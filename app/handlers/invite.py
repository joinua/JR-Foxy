"""Логіка прийому кандидатів у чаті Приймальні JR."""

import logging
import time
from html import escape

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
    User,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.core.config import ADMIN_LOG_CHAT_ID, INVITE_CHAT_ID, MAIN_CHAT_ID
from app.core.db import (
    cancel_pending_tasks,
    answer_candidate_rules,
    finish_candidate_rules_send,
    get_admin_level,
    get_candidate,
    get_candidate_in_any_chat,
    get_candidate_invite_message,
    postpone_candidate_review,
    release_candidate_rules_reservation,
    reserve_candidate_rules,
    reset_candidate_rules,
    schedule_task,
    set_candidate_buttons_message,
    set_candidate_invite_message,
    set_candidate_rules_block_message,
    update_candidate_status,
    upsert_candidate_on_join,
)

router = Router()
logger = logging.getLogger(__name__)

INVITE_WELCOME_TEXT = (
    "Привіт! Цей чат - місце нашого першого знайомства з адміністрацією клану. "
    "А за лаштунками все готується до твого прийняття в клан. Як тільки хтось "
    "з адміністрації звільниться - ви поспілкуєтеся, а поки напиши нам: звідки "
    "ти, скільки років, в якому клані був до і як дізнався про нас. Буде класно, "
    'коли ми найдемо наш "конект".'
)

ADMIN_LOG_NEW_CANDIDATE_TEXT = (
    "Долучився новий кандидат в чат Приймальні. Через 3 год з’являться кнопки дії "
    "або /candidate у реплай, щоб скоріше прийняти (якщо відповідає умовам). "
    "Кнопка почекати - дасть ще 36 годин очікування на виконання умов кандидатом. "
    "Поспілкуйтеся з ним"
)

REVIEW_BUTTONS_TEXT = (
    "Настав час адміністрації прийняти рішення щодо кандидата. Натисніть  на одну з трьох "
    "кнопок: Прийняти - якщо кандидат відповідає всім вимогам, почекати - дати додатково "
    "36 годин на виконання умов, Відмовити, якщо кандидат не відповідає вимогам клану."
)

WAIT_DONE_TEXT = (
    "Рішення щодо кандидата відкладено на 36 годин. За цей час кандидат повинен "
    "виконати вимоги, поставлені адміністрацією."
)

LEFT_RECEPTION_TEXT = "Не дочекавшись свого зіркового часу - прибульці полетіли далі"
ACTIVE_CANDIDATE_STATUSES = {"candidate", "wait", "invited"}
RULES_TRIGGER_STATUSES = {"candidate", "wait"}

RULES_TEXT = """🦊 Почекаймо на адміністрацію клану разом!

А поки хочу озвучити кілька важливих умов, без яких приєднатися до клану неможливо:

1️⃣ У грі обов’язково мати кланову приставку в ніку. Скопіювати її можна буде в Головному чаті після прийняття до клану.

2️⃣ Обов’язкова присутність у Головному чаті клану. Вихід із чату = вихід із клану.

3️⃣ Не грати з під@р@сами (москалями). Тут без коментарів — навіть слону зрозуміло.

❓ То як, погоджуєшся з цими правилами?"""

BLOCKED_ACCEPT_TEXT = "⚠️ Кандидат ще не погодився з обов’язковими правилами клану."


def _build_rules_keyboard(candidate_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Так", callback_data=f"rules:yes:{candidate_user_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Ні", callback_data=f"rules:no:{candidate_user_id}"
                ),
            ]
        ]
    )


class CandidateFirstMessageFilter(BaseFilter):
    """Пропускає лише звичайне перше повідомлення активного кандидата."""

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        if not user or user.is_bot or (message.text or "").startswith("/"):
            return False
        if message.content_type not in {
            "text",
            "animation",
            "audio",
            "document",
            "live_photo",
            "paid_media",
            "photo",
            "sticker",
            "story",
            "video",
            "video_note",
            "voice",
            "checklist",
            "contact",
            "dice",
            "game",
            "poll",
            "venue",
            "location",
            "users_shared",
            "chat_shared",
            "user_shared",
            "gift",
            "unique_gift",
        }:
            return False
        candidate = await get_candidate(user.id, INVITE_CHAT_ID)
        return bool(
            candidate
            and candidate["status"] in RULES_TRIGGER_STATUSES
            and candidate["rules_status"] == "not_sent"
        )


def _build_review_keyboard(candidate_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Прийняти", callback_data=f"inv:accept:{candidate_user_id}"
                ),
                InlineKeyboardButton(
                    text="Чекати", callback_data=f"inv:wait:{candidate_user_id}"
                ),
                InlineKeyboardButton(
                    text="Відмовити", callback_data=f"inv:reject:{candidate_user_id}"
                ),
            ]
        ]
    )


def _user_mention(user: User) -> str:
    return user.mention_html(user.full_name)


def _candidate_label(user: User) -> str:
    if user.username:
        return f"@{escape(user.username)}"
    name = (user.full_name or "кандидат").strip()
    if name:
        return escape(name)
    return str(user.id)


async def show_candidate_buttons(message: Message, candidate_user_id: int) -> None:
    sent = await message.answer(
        REVIEW_BUTTONS_TEXT,
        reply_markup=_build_review_keyboard(candidate_user_id),
    )
    await set_candidate_buttons_message(
        candidate_user_id, INVITE_CHAT_ID, sent.message_id
    )


@router.message(F.chat.id == INVITE_CHAT_ID, F.new_chat_members)
async def on_candidate_join_reception(message: Message) -> None:
    now = int(time.time())
    review_due_at = now + 3 * 60 * 60

    for user in message.new_chat_members:
        await upsert_candidate_on_join(
            user_id=user.id,
            reception_chat_id=INVITE_CHAT_ID,
            review_due_at=review_due_at,
        )
        await cancel_pending_tasks(
            "invite_review_due", chat_id=INVITE_CHAT_ID, user_id=user.id
        )
        await schedule_task(
            task_type="invite_review_due",
            run_at=review_due_at,
            chat_id=INVITE_CHAT_ID,
            user_id=user.id,
        )

        await message.answer(INVITE_WELCOME_TEXT)
        await message.bot.send_message(
            ADMIN_LOG_CHAT_ID,
            ADMIN_LOG_NEW_CANDIDATE_TEXT,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Відкрити Приймальню", url="https://t.me/invite_jr"
                        )
                    ]
                ]
            ),
        )


@router.message(F.chat.id == INVITE_CHAT_ID, Command("candidate"))
async def force_candidate_review(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if await get_admin_level(admin_id) < 2:
        await message.answer("Слухаюся лише адміністраторів")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("Надішліть команду у відповідь на повідомлення кандидата")
        return

    candidate_user = message.reply_to_message.from_user
    candidate = await get_candidate(candidate_user.id, INVITE_CHAT_ID)
    if not candidate:
        await message.answer(
            "Використайте цю команду на кандидата, а не простого перехожого"
        )
        return

    if candidate["status"] not in ACTIVE_CANDIDATE_STATUSES:
        await message.answer("Немає тіла, немає діла! Кандидат уже не кандидат.")
        return

    await cancel_pending_tasks(
        "invite_review_due", chat_id=INVITE_CHAT_ID, user_id=candidate_user.id
    )
    await show_candidate_buttons(message, candidate_user.id)
    return


@router.message(F.chat.id == INVITE_CHAT_ID, CandidateFirstMessageFilter())
async def send_rules_after_first_candidate_message(message: Message) -> None:
    """Надсилає договір один раз після першого звичайного повідомлення."""
    user = message.from_user
    if not user:
        return
    reserved = await reserve_candidate_rules(
        user.id, INVITE_CHAT_ID, message.message_id
    )
    if not reserved:
        return
    try:
        sent = await message.reply(
            RULES_TEXT,
            parse_mode="HTML",
            reply_markup=_build_rules_keyboard(user.id),
        )
    except Exception:
        await release_candidate_rules_reservation(user.id, INVITE_CHAT_ID)
        logger.exception(
            "Failed to send first candidate rules", extra={"user_id": user.id}
        )
        return
    await finish_candidate_rules_send(user.id, INVITE_CHAT_ID, sent.message_id)
    logger.info("First candidate rules sent", extra={"user_id": user.id})


@router.callback_query(F.data.startswith("rules:"))
async def on_rules_callback(query: CallbackQuery) -> None:
    try:
        _, action, raw_user_id = (query.data or "").split(":", 2)
        candidate_user_id = int(raw_user_id)
    except (TypeError, ValueError):
        await query.answer()
        return

    if action == "resend":
        await _resend_candidate_rules(query, candidate_user_id)
        return
    if action not in {"yes", "no"} or not query.message:
        await query.answer()
        return
    if query.from_user.id != candidate_user_id:
        await query.answer("Ці кнопки призначені кандидату.", show_alert=True)
        return

    candidate = await get_candidate(candidate_user_id, INVITE_CHAT_ID)
    if (
        not candidate
        or candidate["status"] not in RULES_TRIGGER_STATUSES
        or candidate["rules_status"] != "pending"
        or candidate["rules_message_id"] != query.message.message_id
    ):
        await query.answer()
        return

    answer = "accepted" if action == "yes" else "declined"
    changed = await answer_candidate_rules(
        candidate_user_id, INVITE_CHAT_ID, answer, int(time.time())
    )
    if not changed:
        await query.answer()
        return

    mention = (
        f'<a href="tg://user?id={candidate_user_id}">'
        f"{escape(query.from_user.full_name or 'кандидат')}</a>"
    )
    if answer == "accepted":
        text = (
            f"✅ {mention} погоджується з обов’язковими правилами клану й очікує "
            "на адміністрацію, щоб пройти співбесіду.\n\n"
            "🦊 Я також чекаю на адміністрацію — нехай уже скажуть мені, що робити далі."
        )
        logger.info("Candidate accepted rules", extra={"user_id": candidate_user_id})
    else:
        text = (
            f"❌ {mention} не погоджується з обов’язковими правилами клану.\n\n"
            "💬 Розкажи, будь ласка, з яким саме пунктом ти не погоджуєшся і чому. "
            "Без виконання цих умов приєднатися до клану неможливо."
        )
        logger.info("Candidate declined rules", extra={"user_id": candidate_user_id})
    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=None)
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning(
            "Failed to edit candidate rules response",
            extra={"user_id": candidate_user_id},
        )
    await query.answer()


async def _resend_candidate_rules(query: CallbackQuery, candidate_user_id: int) -> None:
    if await get_admin_level(query.from_user.id) < 2:
        await query.answer("Слухаюся лише адміністраторів.", show_alert=True)
        return
    if not query.message or query.message.chat.id != INVITE_CHAT_ID:
        await query.answer()
        return
    candidate = await get_candidate(candidate_user_id, INVITE_CHAT_ID)
    if not candidate or candidate["status"] not in RULES_TRIGGER_STATUSES:
        await query.answer(
            "Немає тіла, немає діла! Кандидат уже не кандидат.", show_alert=True
        )
        return
    if not await reset_candidate_rules(candidate_user_id, INVITE_CHAT_ID):
        await query.answer()
        return

    kwargs = {
        "chat_id": INVITE_CHAT_ID,
        "text": RULES_TEXT,
        "parse_mode": "HTML",
        "reply_markup": _build_rules_keyboard(candidate_user_id),
    }
    first_message_id = candidate["rules_first_message_id"]
    try:
        sent = (
            await query.bot.send_message(**kwargs, reply_to_message_id=first_message_id)
            if first_message_id
            else await query.bot.send_message(**kwargs)
        )
    except TelegramBadRequest:
        mention = f'<a href="tg://user?id={candidate_user_id}">кандидат</a>\n\n'
        try:
            sent = await query.bot.send_message(
                **{**kwargs, "text": mention + RULES_TEXT}
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.warning(
                "Failed to resend candidate rules", extra={"user_id": candidate_user_id}
            )
            await query.answer(
                "Не вдалося надіслати договір. Спробуйте ще раз.", show_alert=True
            )
            return
    except TelegramForbiddenError:
        logger.warning(
            "Failed to resend candidate rules", extra={"user_id": candidate_user_id}
        )
        await query.answer(
            "Не вдалося надіслати договір. Спробуйте ще раз.", show_alert=True
        )
        return
    await finish_candidate_rules_send(
        candidate_user_id, INVITE_CHAT_ID, sent.message_id
    )
    try:
        await query.message.edit_text(
            "🔁 Договір повторно надіслано кандидату.", reply_markup=None
        )
    except TelegramBadRequest:
        pass
    await set_candidate_rules_block_message(candidate_user_id, INVITE_CHAT_ID, None)
    logger.info("Candidate rules resent", extra={"user_id": candidate_user_id})
    await query.answer()


@router.callback_query(F.data.startswith("inv:"))
async def on_invite_callback(query: CallbackQuery) -> None:
    if not query.message or query.message.chat.id != INVITE_CHAT_ID:
        await query.answer()
        return

    admin_id = query.from_user.id if query.from_user else 0
    if await get_admin_level(admin_id) < 2:
        await query.answer("Слухаюся лише адміністраторів", show_alert=True)
        return

    try:
        _, action, raw_user_id = (query.data or "").split(":", 2)
        candidate_user_id = int(raw_user_id)
    except (ValueError, TypeError):
        await query.answer()
        return
    candidate = await get_candidate(candidate_user_id, INVITE_CHAT_ID)
    if not candidate or candidate["status"] not in ACTIVE_CANDIDATE_STATUSES:
        await query.answer(
            "Немає тіла, немає діла! Кандидат уже не кандидат.", show_alert=True
        )
        return

    reviewed_at = int(time.time())

    if action == "accept":
        if candidate["rules_status"] != "accepted":
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔁 Надіслати договір ще раз",
                            callback_data=f"rules:resend:{candidate_user_id}",
                        )
                    ]
                ]
            )
            block_message_id = candidate["rules_block_message_id"]
            if block_message_id:
                try:
                    await query.bot.edit_message_text(
                        BLOCKED_ACCEPT_TEXT,
                        chat_id=INVITE_CHAT_ID,
                        message_id=block_message_id,
                        reply_markup=keyboard,
                    )
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
                    if "message is not modified" not in str(exc).lower():
                        block_message_id = None
            if not block_message_id:
                blocked = await query.message.answer(
                    BLOCKED_ACCEPT_TEXT, reply_markup=keyboard
                )
                await set_candidate_rules_block_message(
                    candidate_user_id, INVITE_CHAT_ID, blocked.message_id
                )
            logger.info(
                "Candidate accept blocked by rules",
                extra={"user_id": candidate_user_id},
            )
            await query.answer()
            return
        try:
            invite = await query.bot.create_chat_invite_link(
                chat_id=MAIN_CHAT_ID,
                expire_date=reviewed_at + 86400,
                member_limit=1,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            await query.message.answer(
                "Не маю права створити інвайт-лінк у головний чат. Допоможіть!"
            )
            await query.answer()
            return

        await update_candidate_status(
            user_id=candidate_user_id,
            reception_chat_id=INVITE_CHAT_ID,
            status="invited",
            reviewed_by=admin_id,
            reviewed_at=reviewed_at,
            invite_link=invite.invite_link,
        )

        admin_mention = _user_mention(query.from_user)
        await query.message.edit_text(
            (
                "Кандидат офіційно стає учасником клану! "
                f"Адміністратор {admin_mention} прийняв кандидата. Посилання на чат готове!"
            ),
            parse_mode="HTML",
            reply_markup=None,
        )

        try:
            chat_member = await query.bot.get_chat_member(
                INVITE_CHAT_ID, candidate_user_id
            )
            candidate_user = chat_member.user
        except Exception:
            candidate_user = None

        mention = f'<a href="tg://user?id={candidate_user_id}">кандидат</a>'
        if candidate_user:
            mention = _user_mention(candidate_user)

        invite_message = await query.message.answer(
            f"{mention}, ось твоє посилання на наш офіційний чат.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Посилання в чат", url=invite.invite_link
                        )
                    ]
                ]
            ),
        )
        await set_candidate_invite_message(
            candidate_user_id,
            INVITE_CHAT_ID,
            invite_message.message_id,
        )

        label = str(candidate_user_id)
        if candidate_user:
            label = _candidate_label(candidate_user)

        await query.bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f"Адміністратор {admin_mention} прийняв в клан {label}",
            parse_mode="HTML",
        )

    elif action == "reject":
        try:
            await query.bot.ban_chat_member(INVITE_CHAT_ID, candidate_user_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            await query.message.answer(
                "Не можу кікати людей. Виправіть додатковими дозволами"
            )
            await query.answer()
            return

        await update_candidate_status(
            user_id=candidate_user_id,
            reception_chat_id=INVITE_CHAT_ID,
            status="kicked",
            reviewed_by=admin_id,
            reviewed_at=reviewed_at,
        )

        admin_mention = _user_mention(query.from_user)
        await query.bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f"Адміністратор {admin_mention} відмовив кандидату та кікнув {candidate_user_id}",
            parse_mode="HTML",
        )

        try:
            await query.message.delete()
        except Exception:
            await query.message.edit_reply_markup(reply_markup=None)

    elif action == "wait":
        new_due = int(time.time()) + 36 * 60 * 60
        await postpone_candidate_review(candidate_user_id, INVITE_CHAT_ID, new_due)
        await cancel_pending_tasks(
            "invite_review_due", chat_id=INVITE_CHAT_ID, user_id=candidate_user_id
        )
        await schedule_task(
            task_type="invite_review_due",
            run_at=new_due,
            chat_id=INVITE_CHAT_ID,
            user_id=candidate_user_id,
        )
        await query.message.edit_text(WAIT_DONE_TEXT, reply_markup=None)
    else:
        await query.answer()
        return

    await query.answer()


async def cleanup_candidate_after_main_join(message: Message) -> None:
    """Очищає кандидата після його входу в основний чат."""
    logger.info(
        "Detected new_chat_members in main chat %s: %s",
        MAIN_CHAT_ID,
        [user.id for user in message.new_chat_members],
    )

    for user in message.new_chat_members:
        candidate = await get_candidate_in_any_chat(user.id)
        if candidate:
            logger.info(
                "Candidate found for user_id=%s in reception_chat_id=%s with status=%s",
                user.id,
                candidate["reception_chat_id"],
                candidate["status"],
            )
        else:
            logger.info("Candidate not found for user_id=%s", user.id)
        if not candidate:
            continue

        if candidate["status"] not in ACTIVE_CANDIDATE_STATUSES:
            continue

        await update_candidate_status(
            user.id, candidate["reception_chat_id"], "accepted"
        )

        invite_message_id = await get_candidate_invite_message(
            user.id,
            candidate["reception_chat_id"],
        )
        if invite_message_id is not None:
            logger.info(
                "Invite message found for user_id=%s: invite_message_id=%s",
                user.id,
                invite_message_id,
            )
        else:
            logger.info("Invite message missing for user_id=%s", user.id)
        if invite_message_id is not None:
            try:
                await message.bot.delete_message(INVITE_CHAT_ID, invite_message_id)
                await set_candidate_invite_message(
                    user.id,
                    candidate["reception_chat_id"],
                    None,
                )
                logger.info(
                    "Invite message deleted for user_id=%s: invite_message_id=%s",
                    user.id,
                    invite_message_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to delete invite message for user_id=%s invite_message_id=%s: %s",
                    user.id,
                    invite_message_id,
                    exc,
                )

        try:
            await message.bot.ban_chat_member(INVITE_CHAT_ID, user.id)
            logger.info("User kicked from invite chat: user_id=%s", user.id)
        except Exception as exc:
            logger.warning(
                "Failed to kick user_id=%s from invite chat: %s",
                user.id,
                exc,
            )

        await cancel_pending_tasks(
            "invite_review_due",
            chat_id=candidate["reception_chat_id"],
            user_id=user.id,
        )
