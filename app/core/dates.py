"""Централізоване форматування дат для повідомлень українською."""

from __future__ import annotations

from datetime import datetime, timezone


_MONTHS_UA = {
    1: "січня",
    2: "лютого",
    3: "березня",
    4: "квітня",
    5: "травня",
    6: "червня",
    7: "липня",
    8: "серпня",
    9: "вересня",
    10: "жовтня",
    11: "листопада",
    12: "грудня",
}


def _to_datetime(value: datetime | int) -> datetime:
    """Нормалізує значення дати до `datetime` у UTC.

    Параметри:
        value: `datetime` або UNIX-мітка у секундах.

    Повертає:
        Об'єкт `datetime` у часовій зоні UTC.
    """

    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def format_ua_date(value: datetime | int) -> str:
    """Форматує дату у вигляд «30 жовтня 2026 року».

    Параметри:
        value: `datetime` або UNIX-мітка у секундах.

    Повертає:
        Людинозрозумілий рядок дати українською.
    """

    dt = _to_datetime(value)
    month = _MONTHS_UA.get(dt.month, "")
    return f"{dt.day} {month} {dt.year} року"
