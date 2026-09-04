"""Business rules and Telegram feedback for public event responses."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.core.config import MAIN_CHAT_ID
from app.dao import event_responses as responses_dao
from app.services import event_service


@dataclass(frozen=True)
class ResponseFeedback:
    text: str
    show_alert: bool


FEEDBACK = {
    "going": ResponseFeedback("✅ Твій статус: «Я — учасник».", False),
    "thinking": ResponseFeedback("🤔 Твій статус: «Думаю».", False),
    "declined": ResponseFeedback("❌ Твій статус: «Відмовляюся».", False),
    "late_declined": ResponseFeedback("⚠️ Пізню відмову підтверджено.", False),
    "already": ResponseFeedback("Цей статус уже встановлено.", False),
    "missing_nickname": ResponseFeedback(
        "Неможливо підтвердити участь. Заповніть спершу свій профіль /profile.",
        True,
    ),
    "not_member": ResponseFeedback(
        "Підтвердити участь можуть лише учасники головного чату JokerRecon.",
        True,
    ),
    "membership_check_failed": ResponseFeedback(
        "Не вдалося перевірити участь у головному чаті. Спробуйте ще раз трохи "
        "пізніше.",
        True,
    ),
    "limit": ResponseFeedback(
        "У події вже зареєстровано максимальну кількість учасників — 50.",
        True,
    ),
    "registration_closed": ResponseFeedback(
        "Реєстрацію на подію вже завершено. Змінити статус неможливо.",
        True,
    ),
    "started": ResponseFeedback(
        "Подія вже розпочалася. Змінювати відповідь більше не можна.",
        True,
    ),
    "thinking_late_blocked": ResponseFeedback(
        "Після завершення безпечного терміну статус «Думаю» недоступний. "
        "Якщо ви не зможете бути присутні — скористайтеся кнопкою "
        "«Відмовляюся».",
        True,
    ),
    "late_warning": ResponseFeedback(
        "Увага. Безпечний термін минув. Відмова буде зарахована як половина "
        "пропуску. Натисніть «Відмовляюся» ще раз протягом 60 секунд для "
        "підтвердження.",
        True,
    ),
    "late_confirmation_expired": ResponseFeedback(
        "Час підтвердження минув. Натисніть «Відмовляюся» ще раз, якщо хочете "
        "продовжити.",
        True,
    ),
    "cancelled": ResponseFeedback(
        "Подію скасовано. Відповіді більше не приймаються.",
        True,
    ),
    "stale": ResponseFeedback(
        "Стан події вже змінився. Відкрийте актуальну картку.",
        True,
    ),
    "invalid": ResponseFeedback("Некоректна дія.", True),
}

_card_locks: dict[int, asyncio.Lock] = {}
logger = logging.getLogger(__name__)


def _telegram_name(user) -> str:
    full_name = " ".join(
        part for part in (user.first_name or "", user.last_name or "") if part
    ).strip()
    return full_name or user.username or str(user.id)


async def _is_main_chat_member(bot: Bot, user_id: int) -> bool | None:
    try:
        member = await bot.get_chat_member(MAIN_CHAT_ID, user_id)
    except TelegramBadRequest:
        return False
    except TelegramForbiddenError:
        logger.exception("failed to verify main chat membership", extra={"user_id": user_id})
        return None
    except Exception:
        logger.exception("failed to verify main chat membership", extra={"user_id": user_id})
        return None
    status = getattr(member.status, "value", member.status)
    if status in {"left", "kicked"}:
        return False
    if status == "restricted" and not getattr(member, "is_member", False):
        return False
    return True


async def apply_public_response(
    bot: Bot,
    *,
    event_id: int,
    action: str,
    user,
    now: int | None = None,
) -> ResponseFeedback:
    if action == "going":
        membership = await _is_main_chat_member(bot, user.id)
        if membership is None:
            return FEEDBACK["membership_check_failed"]
        if not membership:
            return FEEDBACK["not_member"]

    current = event_service.now_timestamp() if now is None else now
    decision = await responses_dao.apply_response(
        event_id,
        user.id,
        action,
        telegram_name=_telegram_name(user),
        now=current,
    )
    if decision.changed:
        from app.handlers.events.keyboards import public_event_keyboard

        lock = _card_locks.setdefault(event_id, asyncio.Lock())
        async with lock:
            await event_service.refresh_event_card(
                bot,
                event_id,
                reply_markup_factory=public_event_keyboard,
            )
    return FEEDBACK.get(decision.code, FEEDBACK["stale"])
