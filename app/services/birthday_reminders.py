"""Birthday reminder storage and scheduling helpers."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import aiosqlite
from aiogram import Bot

from app.core.config import ADMIN_LOG_CHAT_ID
from app.core.db import DB_PATH, cancel_pending_tasks, schedule_task
from app.handlers.profile.utils import age_on
from app.services import profile_service

BIRTHDAY_DAILY_TASK = "birthday_daily"
BIRTHDAY_REMIND_TASK = "birthday_remind"


def _next_0800_timestamp(now: datetime | None = None) -> int:
    now = now or datetime.now()
    run_at = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)
    return int(run_at.timestamp())


async def register_birthday_daily_task() -> None:
    await cancel_pending_tasks(BIRTHDAY_DAILY_TASK)
    await schedule_task(BIRTHDAY_DAILY_TASK, _next_0800_timestamp())


async def ensure_birthday_notification(user_id: int, birthday_date: str) -> int | None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO birthday_notifications (user_id, birthday_date, status, remind_at, created_at)
            VALUES (?, ?, 'pending', NULL, ?)
            """,
            (user_id, birthday_date, now),
        )
        cur = await db.execute(
            """
            SELECT id, status FROM birthday_notifications
            WHERE user_id=? AND birthday_date=?
            """,
            (user_id, birthday_date),
        )
        row = await cur.fetchone()
        await db.commit()
    if not row or row[1] == "completed":
        return None
    return int(row[0])


async def complete_birthday_notification(notification_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE birthday_notifications SET status='completed', remind_at=NULL WHERE id=?", (notification_id,))
        await db.commit()


async def postpone_birthday_notification(notification_id: int) -> None:
    remind_at = int(time.time()) + 6 * 60 * 60
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE birthday_notifications SET status='pending', remind_at=? WHERE id=? AND status!='completed'", (remind_at, notification_id))
        cur = await db.execute("SELECT user_id FROM birthday_notifications WHERE id=? AND status='pending'", (notification_id,))
        row = await cur.fetchone()
        await db.commit()
    if row:
        await cancel_pending_tasks(BIRTHDAY_REMIND_TASK, user_id=int(row[0]))
        await schedule_task(BIRTHDAY_REMIND_TASK, remind_at, ADMIN_LOG_CHAT_ID, int(row[0]), str(notification_id))


async def _send_notification(bot: Bot, notification_id: int, profile: dict) -> None:
    from app.handlers.profile.profile_admin import birthday_reminder_keyboard

    birthday = date.fromisoformat(profile["birthday"])
    nickname = profile.get("game_nickname") or profile.get("telegram_full_name") or profile.get("telegram_username") or profile["user_id"]
    text = f"🎉 Сьогодні день народження!\n\n{nickname}\n\nВік: {age_on(birthday)}"
    await bot.send_message(ADMIN_LOG_CHAT_ID, text, reply_markup=birthday_reminder_keyboard(notification_id))


async def send_daily_birthday_reminders(bot: Bot) -> None:
    today = date.today()
    today_key = today.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM profiles
            WHERE birthday IS NOT NULL AND birthday != ''
              AND strftime('%m-%d', birthday)=?
              AND COALESCE(status, 'active')='active'
              AND archived_at IS NULL AND deleted_at IS NULL
            """,
            (today.strftime("%m-%d"),),
        )
        profiles = [dict(row) for row in await cur.fetchall()]
    for profile in profiles:
        notification_id = await ensure_birthday_notification(int(profile["user_id"]), today_key)
        if notification_id:
            await _send_notification(bot, notification_id, profile)
    await register_birthday_daily_task()


async def send_postponed_birthday_reminder(bot: Bot, notification_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, status FROM birthday_notifications WHERE id=?", (notification_id,))
        row = await cur.fetchone()
    if not row or row[1] == "completed":
        return
    profile = await profile_service.get_profile(int(row[0]))
    if profile:
        await _send_notification(bot, notification_id, profile)
