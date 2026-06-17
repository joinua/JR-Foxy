"""Validation and display helpers for profile handlers."""

from __future__ import annotations

import calendar
from datetime import date, datetime
from html import escape

EMPTY_VALUE = "Не внесено"


def parse_user_date(value: str) -> date:
    parsed = datetime.strptime(value, "%d.%m.%Y").date()
    if parsed.strftime("%d.%m.%Y") != value:
        raise ValueError("date must use DD.MM.YYYY format")
    return parsed


def format_user_date(value: str | None) -> str:
    return date.fromisoformat(value).strftime("%d.%m.%Y") if value else EMPTY_VALUE


def pluralize(value: int, one: str, few: str, many: str) -> str:
    if value % 10 == 1 and value % 100 != 11:
        word = one
    elif value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        word = few
    else:
        word = many
    return f"{value} {word}"


def age_on(birthday: date, today: date | None = None) -> int:
    today = today or date.today()
    return today.year - birthday.year - (
        (today.month, today.day) < (birthday.month, birthday.day)
    )


def _birthday_in_year(birthday: date, year: int) -> date:
    day = min(birthday.day, calendar.monthrange(year, birthday.month)[1])
    return date(year, birthday.month, day)


def days_until_birthday(birthday: date, today: date | None = None) -> int:
    today = today or date.today()
    upcoming = _birthday_in_year(birthday, today.year)
    if upcoming < today:
        upcoming = _birthday_in_year(birthday, today.year + 1)
    return (upcoming - today).days


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def format_duration(start: date, end: date | None = None) -> str:
    end = end or date.today()
    if start > end:
        return EMPTY_VALUE

    months = (end.year - start.year) * 12 + end.month - start.month
    while months and _add_months(start, months) > end:
        months -= 1
    cursor = _add_months(start, months)
    years, remaining_months = divmod(months, 12)
    days = (end - cursor).days

    parts = []
    if years:
        parts.append(pluralize(years, "рік", "роки", "років"))
    if remaining_months:
        parts.append(pluralize(remaining_months, "місяць", "місяці", "місяців"))
    if days or not parts:
        parts.append(pluralize(days, "день", "дні", "днів"))
    return " ".join(parts)


def html_user_mention(user_id: int, display_name: str) -> str:
    return f'<a href="tg://user?id={int(user_id)}">{escape(display_name)}</a>'


def profile_owner_mention(profile: dict) -> str:
    display_name = (
        profile.get("telegram_full_name")
        or profile.get("telegram_username")
        or str(profile["user_id"])
    )
    return html_user_mention(profile["user_id"], display_name)


def is_bot_owner(user_id: int) -> bool:
    from app.core.config import BOT_OWNER_ID

    return user_id == BOT_OWNER_ID


def render_profile(profile: dict) -> str:
    birthday = date.fromisoformat(profile["birthday"]) if profile["birthday"] else None
    nickname = escape(profile["game_nickname"] or EMPTY_VALUE)
    uid = escape(profile["codm_uid"]) if profile["codm_uid"] else EMPTY_VALUE
    uid_html = f"<code>{uid}</code>" if profile["codm_uid"] else uid
    age = pluralize(age_on(birthday), "рік", "роки", "років") if birthday else EMPTY_VALUE
    until_birthday = (
        pluralize(days_until_birthday(birthday), "день", "дні", "днів")
        if birthday
        else EMPTY_VALUE
    )
    clan_duration = (
        format_duration(date.fromisoformat(profile["join_date"]))
        if profile["join_date"]
        else EMPTY_VALUE
    )
    role_value = "Лідер" if is_bot_owner(profile["user_id"]) else profile["role"]
    role = escape(role_value or EMPTY_VALUE)
    owner_mention = profile_owner_mention(profile)
    divider = "━━━━━━━━━━━━"

    return (
        f"Профіль гравця клану {owner_mention}\n"
        f"{divider}\n"
        f"🎮 Ігровий нік: {nickname}\n"
        f"🆔 UID: {uid_html}\n"
        f"{divider}\n"
        f"👤 Вік: {age}\n"
        f"🎂 Дата народження: {format_user_date(profile['birthday'])}\n"
        f"⏳ До дня народження: {until_birthday}\n"
        f"{divider}\n"
        f"📥 Дата вступу в клан: {format_user_date(profile['join_date'])}\n"
        f"🛡 Стаж у клані: {clan_duration}\n"
        f"{divider}\n"
        f"🏷 Роль у клані: {role}"
    )
