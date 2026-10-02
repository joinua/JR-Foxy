"""DB-backed scheduler для відкладених задач."""

import asyncio
import logging
import time
import json

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.config import INVITE_CHAT_ID, TIKTOK_CHECK_INTERVAL_SECONDS
from app.core.db import (
    fetch_due_tasks,
    get_candidate,
    mark_task_done,
    mark_task_failed,
    mark_task_running,
    recover_stale_running_tasks,
    set_candidate_buttons_message,
    ensure_periodic_task,
)

from app.services.tiktok_watcher import check_and_notify
from app.services.birthday_reminders import (
    BIRTHDAY_DAILY_TASK,
    BIRTHDAY_REMIND_TASK,
    send_daily_birthday_reminders,
    send_postponed_birthday_reminder,
)
from app.services.talktop import TALKTOP_DAILY_TASK, send_daily_talktop
from app.services.clan_exit import EXIT_CLEANUP_TASK, EXIT_REMINDER_TASK, run_cleanup, run_reminder
from app.services.event_jobs import (
    EVENT_AUTO_REMINDER_TASK,
    EVENT_DRAFT_CLEANUP_TASK,
    EVENT_REGISTRATION_CLOSE_TASK,
    EVENT_START_TASK,
    EVENT_REVIEW_CREATE_TASK,
    EVENT_REVIEW_REMINDER_TASK,
    run_event_auto_reminder,
    run_event_draft_cleanup,
    run_event_registration_close,
    run_event_start,
    run_event_review_create,
    run_event_review_reminder,
)

logger = logging.getLogger(__name__)
SCHEDULER_LEASE_SECONDS = 5 * 60
SCHEDULER_RECOVERY_INTERVAL_SECONDS = 60

REVIEW_BUTTONS_TEXT = (
    "Настав час адміністрації прийняти рішення щодо кандидата. Натисніть  на одну з трьох "
    "кнопок: Прийняти - якщо кандидат відповідає всім вимогам, почекати - дати додатково "
    "36 годин на виконання умов, Відмовити, якщо кандидат не відповідає вимогам клану."
)

LEFT_RECEPTION_TEXT = "Не дочекавшись свого зіркового часу - прибульці полетіли далі"


async def register_tiktok_task() -> None:
    """Зберігає чинний розклад; відсутню перевірку запускає одразу."""
    await ensure_periodic_task("tiktok_check", int(time.time()))


async def _handle_tiktok_check(bot: Bot) -> None:
    result = await check_and_notify(bot)
    delay = TIKTOK_CHECK_INTERVAL_SECONDS
    if result.status in {"feed_error", "telegram_error", "storage_error", "internal_error"}:
        delay = min(delay, 300)
    elif result.remaining or result.status == "busy":
        delay = min(delay, 60)
    await ensure_periodic_task("tiktok_check", int(time.time()) + delay, include_running=False)


def _review_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Прийняти", callback_data=f"inv:accept:{user_id}"
                ),
                InlineKeyboardButton(
                    text="Чекати", callback_data=f"inv:wait:{user_id}"
                ),
                InlineKeyboardButton(
                    text="Відмовити", callback_data=f"inv:reject:{user_id}"
                ),
            ]
        ]
    )


async def _handle_invite_review_due(bot: Bot, task: dict) -> None:
    user_id = int(task["user_id"])
    chat_id = int(task["chat_id"] or INVITE_CHAT_ID)

    candidate = await get_candidate(user_id, chat_id)
    if not candidate or candidate["status"] != "candidate":
        return

    from app.handlers.invite import _is_main_member, _stop_existing_candidate

    membership = await _is_main_member(bot, user_id)
    if membership is True:
        await _stop_existing_candidate(user_id)
        return
    if membership is None:
        raise RuntimeError(f"Cannot verify main chat membership for candidate {user_id}")

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
    last_recovery = 0.0
    while True:
        try:
            monotonic_now = asyncio.get_running_loop().time()
            if monotonic_now - last_recovery >= SCHEDULER_RECOVERY_INTERVAL_SECONDS:
                recovered = await recover_stale_running_tasks(SCHEDULER_LEASE_SECONDS)
                if recovered["recovered"] or recovered["failed"]:
                    logger.warning(
                        "recovered stale scheduler tasks",
                        extra=recovered,
                    )
                last_recovery = monotonic_now

                # A periodic job must survive retry exhaustion or legacy missing jobs.
                await ensure_periodic_task(
                    "tiktok_check", int(time.time()) + TIKTOK_CHECK_INTERVAL_SECONDS
                )

            tasks = await fetch_due_tasks(limit=30)

            for task in tasks:
                task_id = int(task["id"])
                locked = await mark_task_running(task_id)
                if not locked:
                    continue

                try:
                    if task["task_type"] == "invite_review_due":
                        await _handle_invite_review_due(bot, task)
                    elif task["task_type"] == EXIT_CLEANUP_TASK:
                        await run_cleanup(bot, int(task["payload_json"]))
                    elif task["task_type"] == EXIT_REMINDER_TASK:
                        payload = json.loads(task["payload_json"])
                        await run_reminder(bot, int(payload["id"]), int(payload["number"]))
                    elif task["task_type"] == "tiktok_check":
                        await _handle_tiktok_check(bot)
                    elif task["task_type"] == BIRTHDAY_DAILY_TASK:
                        await send_daily_birthday_reminders(bot)
                    elif task["task_type"] == BIRTHDAY_REMIND_TASK:
                        await send_postponed_birthday_reminder(bot, int(task["payload_json"] or 0))
                    elif task["task_type"] == TALKTOP_DAILY_TASK:
                        await send_daily_talktop(bot)
                    elif task["task_type"] == EVENT_DRAFT_CLEANUP_TASK:
                        await run_event_draft_cleanup()
                    elif task["task_type"] == EVENT_AUTO_REMINDER_TASK:
                        await run_event_auto_reminder(bot, task)
                    elif task["task_type"] == EVENT_REGISTRATION_CLOSE_TASK:
                        await run_event_registration_close(bot, task)
                    elif task["task_type"] == EVENT_START_TASK:
                        await run_event_start(bot, task)
                    elif task["task_type"] == EVENT_REVIEW_CREATE_TASK:
                        await run_event_review_create(bot, task)
                    elif task["task_type"] == EVENT_REVIEW_REMINDER_TASK:
                        await run_event_review_reminder(bot, task)
                    await mark_task_done(task_id)
                except Exception as exc:
                    logger.exception("db scheduler task failed", extra={"task_id": task_id})
                    await mark_task_failed(task_id, str(exc))
        except Exception as exc:
            # DB failures outside a handler must not kill the background worker.
            logger.warning("db scheduler iteration failed: %s", type(exc).__name__)

        await asyncio.sleep(poll_interval)
