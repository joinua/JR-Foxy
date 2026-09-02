"""Централізоване форматування дат для повідомлень українською.

Безпечна реалізація для Windows (fallback, якщо Europe/Kyiv недоступний).
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


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


def _ua_tz() -> tzinfo | None:
    """Повертає ZoneInfo('Europe/Kyiv') або None, якщо зона недоступна."""

    if ZoneInfo is None:
        return None

    try:
        return ZoneInfo("Europe/Kyiv")
    except Exception:
        # Windows без tzdata — працюємо без timezone
        return None


KYIV_TZ = _ua_tz()


def to_kyiv_datetime(value: datetime | None = None) -> datetime:
    """Return an aware Europe/Kyiv datetime when timezone data is available."""

    if value is None:
        return datetime.now(KYIV_TZ) if KYIV_TZ is not None else datetime.now()
    if KYIV_TZ is None:
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=KYIV_TZ)
    return value.astimezone(KYIV_TZ)


def today_kyiv() -> date:
    """Return the current calendar date used by every bot feature."""

    return to_kyiv_datetime().date()


def _to_datetime(value: datetime | int) -> datetime:
    """Нормалізує дату до datetime, БЕЗПЕЧНО для Windows."""

    tz = KYIV_TZ

    if isinstance(value, datetime):
        if tz is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)

    ts = int(value)
    if tz is None:
        return datetime.fromtimestamp(ts)
    return datetime.fromtimestamp(ts, tz=tz)


def format_ua_date(value: datetime | int) -> str:
    """Форматує дату у вигляді: 30 жовтня 2026 року."""

    dt = _to_datetime(value)
    month = _MONTHS_UA.get(dt.month, "")
    return f"{dt.day} {month} {dt.year} року"
