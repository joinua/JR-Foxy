"""Administrative controls for clan exit checks."""

from __future__ import annotations

import json
import logging
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.core.access import has_admin_level
from app.core.config import ADMIN_LOG_CHAT_ID
from app.dao import clan_exit as dao
from app.services import clan_exit as service

router = Router()
logger = logging.getLogger(__name__)
ACTIONS = {"game", "contact", "explain", "unavailable", "refresh", "return", "later", "finish"}


class Explanation(StatesGroup):
    text = State()


async def _answer(query: CallbackQuery, text: str, *, show_alert: bool = False) -> None:
    try:
        await service.telegram_call(query.answer(text, show_alert=show_alert))
    except Exception:
        # An expired callback must not prevent an authorized action from finishing.
        logger.exception("Cannot answer exit callback", extra={"callback_id": query.id})


async def _notify(message: Message, text: str) -> None:
    try:
        await service.telegram_call(message.answer(text, parse_mode=None))
    except Exception:
        logger.exception("Cannot send exit action feedback")


@router.callback_query(F.data.startswith("exit:"))
async def exit_action(query: CallbackQuery, state: FSMContext) -> None:
    if not query.message or query.message.chat.id != ADMIN_LOG_CHAT_ID or not query.data:
        await _answer(query, "Ця дія доступна лише в чаті адміністрації.", show_alert=True)
        return
    try:
        if not await has_admin_level(query.from_user.id, 2):
            await _answer(query, "Потрібна роль адміністратора або вище.", show_alert=True)
            return
        try:
            _, action, raw_id = query.data.split(":", 2)
            check_id = int(raw_id)
        except (ValueError, TypeError):
            await _answer(query, "Некоректна перевірка.", show_alert=True)
            return
        if action not in ACTIONS:
            await _answer(query, "Невідома дія.", show_alert=True)
            return
        case = await dao.get(check_id)
        if (not case or case["message_id"] != query.message.message_id
                or (case["status"] != "open" and action != "refresh")):
            await _answer(query, "Ця картка вже неактивна.", show_alert=True)
            return
        if action == "unavailable" and (
            not case["contact_at"] or time.time() - case["contact_at"] < service.EXPLANATION_WAIT_SECONDS
        ):
            await _answer(query, "Потрібен зафіксований запит і 24 години очікування.", show_alert=True)
            return
    except Exception:
        logger.exception("Cannot load exit case", extra={"callback_data": query.data})
        await _answer(query, "Не вдалося відкрити перевірку. Спробуй ще раз.", show_alert=True)
        return

    # Acknowledge exactly once, before DB writes, FSM changes or Telegram checks.
    prompt = "Надішли короткий виклад пояснення сюди, у чат адміністрації."
    await _answer(
        query,
        prompt if action == "explain" else (
            "Перевіряю…" if action in {"refresh", "return", "finish"} else "Обробляю…"
        ),
        show_alert=action == "explain",
    )
    try:
        await _apply_action(query, state, action, case)
    except service.CardPublishError:
        logger.exception("Cannot update exit card", extra={"case_id": check_id, "action": action})
        await _notify(query.message,
                      f"⚠️ Перевірка #{check_id}: дані збережено, але картку не вдалося оновити. "
                      "Натисни «Перевірити чати», щоб повторити оновлення.")
    except Exception:
        logger.exception("Exit action failed", extra={"case_id": check_id, "action": action})
        await _notify(query.message,
                      f"⚠️ Не вдалося завершити дію для перевірки #{check_id}. Спробуй ще раз.")


async def _apply_action(query: CallbackQuery, state: FSMContext, action: str, case: dict) -> None:
    check_id = case["id"]
    actor = query.from_user.id
    if action == "game":
        await dao.change(check_id, actor, "game_confirmed", game_by=actor, game_at=int(time.time()))
    elif action == "contact":
        await dao.change(check_id, actor, "contact_confirmed", contact_by=actor,
                         contact_at=int(time.time()), explanation_status="requested")
    elif action == "explain":
        await state.set_state(Explanation.text)
        await state.update_data(exit_check_id=check_id)
        return
    elif action == "unavailable":
        await dao.change(check_id, actor, "explanation_unavailable",
                         explanation_status="unavailable", explanation_by=actor,
                         explanation_at=int(time.time()), explanation_text=None)
    elif action == "refresh":
        if case["status"] != "open":
            # A completed action may have persisted while its Telegram edit failed.
            await service.publish(query.bot, case)
            return
        await service.refresh_chats(query.bot, case, remove=True, actor_id=actor)
        return
    elif action == "return":
        if not await service.mark_returned(query.bot, case, actor):
            await _notify(query.message, f"Перевірка #{check_id}: повернення в головний чат не підтверджено.")
        return
    elif action == "later":
        await dao.change(check_id, actor, "postponed", chat_states=case["chat_states"])
    elif action == "finish":
        case = await service.refresh_chats(query.bot, case, remove=False, actor_id=actor)
        if case["status"] != "open":
            return
        states = json.loads(case["chat_states"])
        if (states.get(str(service.MAIN_CHAT_ID)) != "немає" or not case["game_at"]
            or case["explanation_status"] not in {"explained", "unavailable"}
            or not all(states.get(str(chat_id)) == "немає" for chat_id in service.OTHER_CHATS)):
            await _notify(query.message, f"Перевірка #{check_id}: є незавершені кроки. Картку оновлено.")
            return
        await dao.change(check_id, actor, "closed", status="closed",
                         closed_by=actor, closed_at=int(time.time()))
    updated = await dao.get(check_id)
    if updated:
        await service.publish(query.bot, updated)
    if action == "later":
        await _notify(query.message,
                      f"Перевірка #{check_id} лишається відкритою. Нагадування надійде за розкладом.")


@router.message(Explanation.text, F.chat.id == ADMIN_LOG_CHAT_ID)
async def record_explanation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    case = await dao.get(int(data.get("exit_check_id", 0)))
    if not message.from_user or not await has_admin_level(message.from_user.id, 2):
        await state.clear()
        return
    if not case or case["status"] != "open":
        await state.clear()
        await _notify(message, "Перевірка вже закрита.")
        return
    summary = (message.text or "").strip()
    if not summary or summary.startswith("/") or len(summary) > 500:
        await _notify(message, "Надішли короткий текст пояснення (до 500 символів).")
        return
    actor = message.from_user.id
    await dao.change(case["id"], actor, "explanation_recorded",
                     explanation_status="explained", explanation_text=summary,
                     explanation_by=actor, explanation_at=int(time.time()))
    await state.clear()
    try:
        await service.publish(message.bot, await dao.get(case["id"]))
    except service.CardPublishError:
        logger.exception("Cannot publish recorded explanation", extra={"case_id": case["id"]})
        await _notify(message, f"⚠️ Пояснення для перевірки #{case['id']} збережено, "
                      "але картку не вдалося оновити. Натисни «Перевірити чати».")
        return
    await _notify(message, "Пояснення записано в картці перевірки.")
