"""Логіка прийому кандидатів у чаті Приймальні JR."""

import time
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery, User
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.core.config import ADMIN_LOG_CHAT_ID, INVITE_CHAT_ID, MAIN_CHAT_ID
from app.core.db import (
    cancel_pending_tasks,
    get_admin_level,
    get_candidate,
    get_candidate_in_any_chat,
    postpone_candidate_review,
    schedule_task,
    set_candidate_buttons_message,
    update_candidate_status,
    upsert_candidate_on_join,
)

router = Router()

INVITE_WELCOME_TEXT = (
    "Привіт! Цей чат - місце нашого першого знайомства з адміністрацією клану. "
    "А за лаштунками все готується до твого прийняття в клан. Як тільки хтось "
    "з адміністрації звільниться - ви поспілкуєтеся, а поки напиши нам: звідки "
    "ти, скільки років, в якому клані був до і як дізнався про нас. Буде класно, "
    "коли ми найдемо наш \"конект\"."
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


def _build_review_keyboard(candidate_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Прийняти", callback_data=f"inv:accept:{candidate_user_id}"),
                InlineKeyboardButton(text="Чекати", callback_data=f"inv:wait:{candidate_user_id}"),
                InlineKeyboardButton(text="Відмовити", callback_data=f"inv:reject:{candidate_user_id}"),
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
    await set_candidate_buttons_message(candidate_user_id, INVITE_CHAT_ID, sent.message_id)


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
        await cancel_pending_tasks("invite_review_due", chat_id=INVITE_CHAT_ID, user_id=user.id)
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
                inline_keyboard=[[InlineKeyboardButton(text="Відкрити Приймальню", url="https://t.me/invite_jr")]]
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
        await message.answer("Використайте цю команду на кандидата, а не простого перехожого")
        return

    if candidate["status"] != "candidate":
        await message.answer("Немає тіла, немає діла! Кандидат уже не кандидат.")
        return

    await show_candidate_buttons(message, candidate_user.id)
    await cancel_pending_tasks("invite_review_due", chat_id=INVITE_CHAT_ID, user_id=candidate_user.id)


@router.callback_query(F.data.startswith("inv:"))
async def on_invite_callback(query: CallbackQuery) -> None:
    if not query.message or query.message.chat.id != INVITE_CHAT_ID:
        await query.answer()
        return

    admin_id = query.from_user.id if query.from_user else 0
    if await get_admin_level(admin_id) < 2:
        await query.answer("Слухаюся лише адміністраторів", show_alert=True)
        return

    _, action, raw_user_id = (query.data or "").split(":", 2)
    candidate_user_id = int(raw_user_id)
    candidate = await get_candidate(candidate_user_id, INVITE_CHAT_ID)
    if not candidate or candidate["status"] != "candidate":
        await query.answer("Немає тіла, немає діла! Кандидат уже не кандидат.", show_alert=True)
        return

    reviewed_at = int(time.time())

    if action == "accept":
        try:
            invite = await query.bot.create_chat_invite_link(
                chat_id=MAIN_CHAT_ID,
                expire_date=reviewed_at + 86400,
                member_limit=1,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            await query.message.answer("Не маю права створити інвайт-лінк у головний чат. Допоможіть!")
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
            chat_member = await query.bot.get_chat_member(INVITE_CHAT_ID, candidate_user_id)
            candidate_user = chat_member.user
        except Exception:
            candidate_user = None

        mention = f'<a href="tg://user?id={candidate_user_id}">кандидат</a>'
        if candidate_user:
            mention = _user_mention(candidate_user)

        await query.message.answer(
            f"{mention}, ось твоє посилання на наш офіційний чат.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Посилання в чат", url=invite.invite_link)]]
            ),
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
            await query.message.answer("Не можу кікати людей. Виправіть додатковими дозволами")
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
            f"Адміністратор {admin_mention} відмовив кандидату та кікнув користувача {candidate_user_id}",
            parse_mode="HTML",
        )

        try:
            await query.message.delete()
        except Exception:
            await query.message.edit_reply_markup(reply_markup=None)

    elif action == "wait":
        new_due = int(time.time()) + 36 * 60 * 60
        await postpone_candidate_review(candidate_user_id, INVITE_CHAT_ID, new_due)
        await cancel_pending_tasks("invite_review_due", chat_id=INVITE_CHAT_ID, user_id=candidate_user_id)
        await schedule_task(
            task_type="invite_review_due",
            run_at=new_due,
            chat_id=INVITE_CHAT_ID,
            user_id=candidate_user_id,
        )
        await query.message.edit_text(WAIT_DONE_TEXT, reply_markup=None)

    await query.answer()


@router.message(F.chat.id == MAIN_CHAT_ID, F.new_chat_members)
async def on_candidate_join_main_chat(message: Message) -> None:
    for user in message.new_chat_members:
        candidate = await get_candidate_in_any_chat(user.id)
        if not candidate:
            continue

        if candidate["status"] not in {"candidate", "invited"}:
            continue

        await update_candidate_status(user.id, candidate["reception_chat_id"], "accepted")
        await cancel_pending_tasks(
            "invite_review_due",
            chat_id=candidate["reception_chat_id"],
            user_id=user.id,
        )

        try:
            await message.bot.ban_chat_member(candidate["reception_chat_id"], user.id)
        except Exception:
            continue
