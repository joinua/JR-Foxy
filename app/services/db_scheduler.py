"""DB-backed scheduler для відкладених задач."""

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.config import INVITE_CHAT_ID
from app.core.db import (
    fetch_due_tasks,
    get_candidate,
    mark_task_done,
    mark_task_failed,
    mark_task_running,
    set_candidate_buttons_message,
)

logger = logging.getLogger(__name__)

REVIEW_BUTTONS_TEXT = (
    "Настав час адміністрації прийняти рішення щодо кандидата. Натисніть  на одну з трьох "
    "кнопок: Прийняти - якщо кандидат відповідає всім вимогам, почекати - дати додатково "
    "36 годин на виконання умов, Відмовити, якщо кандидат не відповідає вимогам клану."
)

LEFT_RECEPTION_TEXT = "Не дочекавшись свого зіркового часу - прибульці полетіли далі"


def _review_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Прийняти", callback_data=f"inv:accept:{user_id}"),
            InlineKeyboardButton(text="Чекати", callback_data=f"inv:wait:{user_id}"),
            InlineKeyboardButton(text="Відмовити", callback_data=f"inv:reject:{user_id}"),
        ]]
    )


async def _handle_invite_review_due(bot: Bot, task: dict) -> None:
    user_id = int(task["user_id"])
    chat_id = int(task["chat_id"] or INVITE_CHAT_ID)

    candidate = await get_candidate(user_id, chat_id)
    if not candidate or candidate["status"] != "candidate":
        return

    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest:
        member = None

    if not member or member.status in {"left", "kicked"}:
        await bot.send_message(chat_id, LEFT_RECEPTION_TEXT)
        return

    sent = await bot.send_message(
        chat_id,
        REVIEW_BUTTONS_TEXT,
        reply_markup=_review_keyboard(user_id),
    )
    await set_candidate_buttons_message(user_id, chat_id, sent.message_id)


async def run_db_scheduler(bot: Bot, poll_interval: float = 5.0) -> None:
    while True:
        tasks = await fetch_due_tasks(limit=30)

        for task in tasks:
            task_id = int(task["id"])
            locked = await mark_task_running(task_id)
            if not locked:
                continue

            try:
                if task["task_type"] == "invite_review_due":
                    await _handle_invite_review_due(bot, task)
                await mark_task_done(task_id)
            except Exception as exc:
                logger.exception("db scheduler task failed", extra={"task_id": task_id})
                await mark_task_failed(task_id, str(exc))

        await asyncio.sleep(poll_interval)
