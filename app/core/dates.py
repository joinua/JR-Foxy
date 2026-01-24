"""Централізоване форматування дат для повідомлень українською."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:  # pragma: no cover
    _ZoneInfo = None


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


def _ua_tz() -> Optional[object]:
    """Повертає часову зону України (Europe/Kyiv), якщо доступна."""
    if _ZoneInfo is None:
        return None
    return _ZoneInfo("Europe/Kyiv")


def _to_datetime(value: datetime | int) -> datetime:
    """Нормалізує значення дати до datetime у часовій зоні Europe/Kyiv.

    Параметри:
        value: datetime або UNIX-мітка у секундах.

    Повертає:
        datetime у часовій зоні Europe/Kyiv (або без tzinfo, якщо tz недоступна).
    """
    tz = _ua_tz()

    if isinstance(value, datetime):
        if tz is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)

    timestamp = int(value)
    if tz is None:
        return datetime.fromtimestamp(timestamp)
    return datetime.fromtimestamp(timestamp, tz=tz)


def format_ua_date(value: datetime | int) -> str:
    """Форматує дату у вигляд: 30 жовтня 2026 року."""
    dt = _to_datetime(value)
    month = _MONTHS_UA.get(dt.month, "")
    return f"{dt.day} {month} {dt.year} року"
