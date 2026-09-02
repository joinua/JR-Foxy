"""Birthday reminder storage, rendering, and scheduling helpers."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from html import escape

import aiosqlite
from aiogram import Bot

from app.core.config import ADMIN_LOG_CHAT_ID
from app.core.dates import KYIV_TZ, to_kyiv_datetime
from app.core.db import DB_PATH, cancel_pending_tasks, schedule_task
from app.handlers.profile.utils import (
    EMPTY_VALUE,
    age_on,
    format_duration,
    html_user_mention,
    pluralize,
)
from app.services import profile_service

BIRTHDAY_DAILY_TASK = "birthday_daily"
BIRTHDAY_REMIND_TASK = "birthday_remind"

logger = logging.getLogger(__name__)


def _kyiv_datetime(now: datetime | None = None) -> datetime:
    return to_kyiv_datetime(now)


def _next_0800_timestamp(now: datetime | None = None) -> int:
    local_now = _kyiv_datetime(now)
    run_at = local_now.replace(hour=8, minute=0, second=0, microsecond=0)
    if run_at <= local_now:
        run_at += timedelta(days=1)
    return int(run_at.timestamp())


def _profile_display_name(profile: dict) -> str:
    full_name = str(profile.get("telegram_full_name") or "").strip()
    if full_name:
        return full_name
    username = str(profile.get("telegram_username") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    return str(profile.get("game_nickname") or profile["user_id"])


def _profile_mention(profile: dict) -> str:
    return html_user_mention(int(profile["user_id"]), _profile_display_name(profile))


def _nickname_html(profile: dict) -> str:
    nickname = str(profile.get("game_nickname") or EMPTY_VALUE)
    return f"<code>{escape(nickname)}</code>"


def _birthday_age(profile: dict, birthday_date: date) -> int:
    return age_on(date.fromisoformat(str(profile["birthday"])), birthday_date)


def render_birthday_message(profile: dict, birthday_date: date) -> str:
    age = _birthday_age(profile, birthday_date)
    return (
        "🎉 <b>Сьогодні день народження!</b>\n\n"
        f"👤 {_profile_mention(profile)}\n"
        f"🎮 Ігровий нік: {_nickname_html(profile)}\n"
        f"🥳 Святкує: {pluralize(age, 'рік', 'роки', 'років')}"
    )


def render_birthday_pre_message(
    profile: dict,
    birthday_date: date,
    *,
    responsible_user_id: int | None = None,
    responsible_name: str | None = None,
) -> str:
    age = _birthday_age(profile, birthday_date)
    if profile.get("join_date"):
        clan_duration = format_duration(
            date.fromisoformat(str(profile["join_date"])),
            birthday_date - timedelta(days=1),
        )
    else:
        clan_duration = EMPTY_VALUE

    text = (
        f"<b>ЗАВТРА 🎉</b> свої {pluralize(age, 'рік', 'роки', 'років')} "
        f"святкує гравець {_profile_mention(profile)}.\n\n"
        f"🎮 Ігровий нік: {_nickname_html(profile)}\n"
        f"🛡 З нами вже: {escape(clan_duration)}.\n\n"
        "Залишилося запланувати привітання від імені клану 😉"
    )
    if responsible_user_id is not None:
        responsible = html_user_mention(
            responsible_user_id,
            responsible_name or str(responsible_user_id),
        )
        text += f"\n\nВідповідальний за привітання: {responsible}"
    return text


async def register_birthday_daily_task(*, catch_up_today: bool = False) -> None:
    await cancel_pending_tasks(BIRTHDAY_DAILY_TASK)
    local_now = _kyiv_datetime()
    already_after_daily_time = local_now.hour >= 8
    run_at = (
        int(time.time()) + 1
        if catch_up_today and already_after_daily_time
        else _next_0800_timestamp(local_now)
    )
    await schedule_task(BIRTHDAY_DAILY_TASK, run_at)


async def ensure_birthday_notification(
    user_id: int,
    birthday_date: str,
) -> int | None:
    """Atomically reserve one day-of notification for a user and date."""

    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO birthday_notifications (
                user_id, birthday_date, status, remind_at, created_at
            ) VALUES (?, ?, 'pending', NULL, ?)
            """,
            (user_id, birthday_date, now),
        )
        await db.commit()
        return int(cursor.lastrowid) if cursor.rowcount > 0 else None


async def finish_birthday_notification_send(
    notification_id: int,
    message_id: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE birthday_notifications
            SET message_id=?, sent_at=?
            WHERE id=?
            """,
            (message_id, int(time.time()), notification_id),
        )
        await db.commit()


async def release_birthday_notification(notification_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM birthday_notifications
            WHERE id=? AND message_id IS NULL AND status='pending'
            """,
            (notification_id,),
        )
        await db.commit()


async def complete_birthday_notification(notification_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE birthday_notifications
            SET status='completed', remind_at=NULL
            WHERE id=? AND status!='completed'
            """,
            (notification_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def postpone_birthday_notification(notification_id: int) -> bool:
    remind_at = int(time.time()) + 6 * 60 * 60
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE birthday_notifications
            SET status='pending', remind_at=?
            WHERE id=? AND status!='completed'
            """,
            (remind_at, notification_id),
        )
        user_cursor = await db.execute(
            """
            SELECT user_id FROM birthday_notifications
            WHERE id=? AND status='pending'
            """,
            (notification_id,),
        )
        row = await user_cursor.fetchone()
        await db.commit()
    if cursor.rowcount <= 0 or not row:
        return False
    user_id = int(row[0])
    await cancel_pending_tasks(BIRTHDAY_REMIND_TASK, user_id=user_id)
    await schedule_task(
        BIRTHDAY_REMIND_TASK,
        remind_at,
        ADMIN_LOG_CHAT_ID,
        user_id,
        str(notification_id),
    )
    return True


async def ensure_birthday_pre_notification(
    user_id: int,
    birthday_date: str,
) -> int | None:
    """Atomically reserve one day-before notification for a user and birthday."""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO birthday_pre_notifications (
                user_id, birthday_date, status, created_at
            ) VALUES (?, ?, 'pending', ?)
            """,
            (user_id, birthday_date, int(time.time())),
        )
        await db.commit()
        return int(cursor.lastrowid) if cursor.rowcount > 0 else None


async def finish_birthday_pre_notification_send(
    notification_id: int,
    message_id: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE birthday_pre_notifications
            SET message_id=?
            WHERE id=?
            """,
            (message_id, notification_id),
        )
        await db.commit()


async def release_birthday_pre_notification(notification_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM birthday_pre_notifications
            WHERE id=? AND message_id IS NULL AND status='pending'
            """,
            (notification_id,),
        )
        await db.commit()


async def claim_birthday_pre_notification(
    notification_id: int,
    responsible_user_id: int,
    responsible_name: str,
) -> dict | None:
    """Assign the first admin who claims a greeting and reject later claims."""

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            UPDATE birthday_pre_notifications
            SET status='claimed', responsible_user_id=?, responsible_name=?, claimed_at=?
            WHERE id=? AND status='pending' AND responsible_user_id IS NULL
            """,
            (
                responsible_user_id,
                responsible_name,
                int(time.time()),
                notification_id,
            ),
        )
        if cursor.rowcount <= 0:
            await db.commit()
            return None
        row_cursor = await db.execute(
            "SELECT * FROM birthday_pre_notifications WHERE id=?",
            (notification_id,),
        )
        row = await row_cursor.fetchone()
        await db.commit()
        return dict(row) if row else None


async def _load_birthday_profiles(month_day: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM profiles
            WHERE birthday IS NOT NULL AND birthday != ''
              AND strftime('%m-%d', birthday)=?
              AND COALESCE(status, 'active')='active'
              AND archived_at IS NULL AND deleted_at IS NULL
            """,
            (month_day,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def _send_notification(bot: Bot, notification_id: int, profile: dict) -> int:
    from app.handlers.profile.profile_admin import birthday_reminder_keyboard

    birthday_date = _kyiv_datetime().date()
    sent = await bot.send_message(
        ADMIN_LOG_CHAT_ID,
        render_birthday_message(profile, birthday_date),
        parse_mode="HTML",
        reply_markup=birthday_reminder_keyboard(notification_id),
        disable_web_page_preview=True,
    )
    return int(sent.message_id)


async def _send_pre_notification(
    bot: Bot,
    notification_id: int,
    profile: dict,
    birthday_date: date,
) -> int:
    from app.handlers.profile.profile_admin import birthday_pre_reminder_keyboard

    sent = await bot.send_message(
        ADMIN_LOG_CHAT_ID,
        render_birthday_pre_message(profile, birthday_date),
        parse_mode="HTML",
        reply_markup=birthday_pre_reminder_keyboard(notification_id),
        disable_web_page_preview=True,
    )
    return int(sent.message_id)


async def send_daily_birthday_reminders(bot: Bot) -> None:
    """Send today/tomorrow reminders and always preserve the next daily run."""

    today = _kyiv_datetime().date()
    tomorrow = today + timedelta(days=1)
    failures: list[str] = []

    try:
        today_profiles = await _load_birthday_profiles(today.strftime("%m-%d"))
        for profile in today_profiles:
            notification_id = await ensure_birthday_notification(
                int(profile["user_id"]),
                today.isoformat(),
            )
            if notification_id is None:
                continue
            try:
                message_id = await _send_notification(bot, notification_id, profile)
                await finish_birthday_notification_send(notification_id, message_id)
            except Exception as exc:
                await release_birthday_notification(notification_id)
                failures.append(f"today user_id={profile['user_id']}: {exc}")
                logger.exception(
                    "birthday: failed to send day-of notification",
                    extra={"user_id": profile["user_id"]},
                )

        tomorrow_profiles = await _load_birthday_profiles(tomorrow.strftime("%m-%d"))
        for profile in tomorrow_profiles:
            notification_id = await ensure_birthday_pre_notification(
                int(profile["user_id"]),
                tomorrow.isoformat(),
            )
            if notification_id is None:
                continue
            try:
                message_id = await _send_pre_notification(
                    bot,
                    notification_id,
                    profile,
                    tomorrow,
                )
                await finish_birthday_pre_notification_send(
                    notification_id,
                    message_id,
                )
            except Exception as exc:
                await release_birthday_pre_notification(notification_id)
                failures.append(f"tomorrow user_id={profile['user_id']}: {exc}")
                logger.exception(
                    "birthday: failed to send day-before notification",
                    extra={"user_id": profile["user_id"]},
                )
    finally:
        await register_birthday_daily_task()

    if failures:
        raise RuntimeError("; ".join(failures))


async def send_postponed_birthday_reminder(
    bot: Bot,
    notification_id: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT user_id, status FROM birthday_notifications WHERE id=?
            """,
            (notification_id,),
        )
        row = await cursor.fetchone()
    if not row or row[1] == "completed":
        return
    profile = await profile_service.get_profile(int(row[0]))
    if profile:
        message_id = await _send_notification(bot, notification_id, profile)
        await finish_birthday_notification_send(notification_id, message_id)
