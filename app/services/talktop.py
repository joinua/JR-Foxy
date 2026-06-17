"""Щоденний рейтинг активності чату Родини."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.core.config import FAMILY_CHAT_ID
from app.core.db import (
    cancel_pending_tasks,
    get_chat_setting,
    get_daily_talk_top,
    get_talk_record_before,
    schedule_task,
)

TALKTOP_DAILY_TASK = "daily_talktop"
TALKTOP_ENABLED_KEY = "daily_talktop_enabled"
KYIV_TZ = ZoneInfo("Europe/Kyiv")
_MONTHS_UA = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня", 5: "травня", 6: "червня",
    7: "липня", 8: "серпня", 9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}
_MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]


def today_kyiv() -> date:
    return datetime.now(KYIV_TZ).date()


def _next_2359_timestamp(now: datetime | None = None) -> int:
    now = now.astimezone(KYIV_TZ) if now else datetime.now(KYIV_TZ)
    run_at = now.replace(hour=23, minute=59, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)
    return int(run_at.timestamp())


async def register_daily_talktop_task() -> None:
    await cancel_pending_tasks(TALKTOP_DAILY_TASK, chat_id=FAMILY_CHAT_ID)
    await schedule_task(TALKTOP_DAILY_TASK, _next_2359_timestamp(), FAMILY_CHAT_ID)


def format_short_ua_date(value: date) -> str:
    return f"{value.day} {_MONTHS_UA[value.month]}"


def format_full_ua_date(value: date | str) -> str:
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value.day} {_MONTHS_UA[value.month]} {value.year} року"


def message_word(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "повідомлень"
    if count % 10 == 1:
        return "повідомлення"
    if 2 <= count % 10 <= 4:
        return "повідомлення"
    return "повідомлень"


def mention_html(row: dict) -> str:
    name = (row.get("full_name") or row.get("username") or "гравець").strip()
    if row.get("username") and not row.get("full_name"):
        name = f"@{row['username'].lstrip('@')}"
    return f'<a href="tg://user?id={int(row["user_id"])}">{escape(name)}</a>'


def _record_line(prefix: str, row: dict) -> str:
    count = int(row["message_count"])
    return (
        f"{prefix} {mention_html(row)} — {count} {message_word(count)} "
        f"{format_full_ua_date(row['activity_date'])}."
    )


def render_talktop(top_rows: list[dict], previous_record: dict | None, activity_date: date) -> str:
    title = "🗣️ <b>Топ 7 балакунів клану</b>"
    if not top_rows:
        return (
            f"{title}\n\n"
            "Щось тут тихо 😶\n"
            "Піду, мабуть, пограю в колду 🎮\n"
            "Може і приведу нам в клан балакуна — з ним веселіше буде 😏🦊"
        )

    lines = [
        title,
        "",
        f"Сьогодні, {format_short_ua_date(activity_date)}, я рахувала детально повідомлення кожного і створила список найбільших “балакунів” клану 😏",
        "",
    ]
    for index, row in enumerate(top_rows):
        count = int(row["message_count"])
        lines.append(f"{_MEDALS[index]} — {mention_html(row)}: {count} {message_word(count)}")

    lines.extend(["", "━━━━━━━━━━━━━━", "", "🏆 <b>Рекорд</b>", ""])
    today_record = top_rows[0]
    if previous_record and int(today_record["message_count"]) > int(previous_record["message_count"]):
        lines.extend([
            "🔥 Сьогодні встановлено новий рекорд!",
            "",
            _record_line("Новий рекордсмен:", today_record),
            "",
            _record_line("Попередній рекорд тримав", previous_record),
        ])
    else:
        record = previous_record or today_record
        lines.append(_record_line("Найбільший рекорд зафіксовано в", record))
    return "\n".join(lines)


async def send_daily_talktop(bot: Bot) -> None:
    try:
        enabled = await get_chat_setting(FAMILY_CHAT_ID, TALKTOP_ENABLED_KEY)
        if enabled == "1":
            activity_date = today_kyiv()
            date_key = activity_date.isoformat()
            top_rows = await get_daily_talk_top(FAMILY_CHAT_ID, date_key, limit=7)
            previous_record = await get_talk_record_before(FAMILY_CHAT_ID, before_date=date_key)
            await bot.send_message(
                FAMILY_CHAT_ID,
                render_talktop(top_rows, previous_record, activity_date),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    finally:
        await register_daily_talktop_task()
