"""Timezone-aware Ukrainian date helpers used by every bot feature."""

from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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

_WEEKDAYS_UA = {
    0: "понеділок",
    1: "вівторок",
    2: "середа",
    3: "четвер",
    4: "п’ятниця",
    5: "субота",
    6: "неділя",
}


try:
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError as exc:  # pragma: no cover - deployment guard
    raise RuntimeError(
        "Europe/Kyiv timezone is unavailable. Install the tzdata package."
    ) from exc


def to_kyiv_datetime(value: datetime | None = None) -> datetime:
    """Return an aware Europe/Kyiv datetime.

    Naive values are intentionally interpreted as Kyiv wall-clock values for
    compatibility with existing callers. New persistence code must store UTC.
    """

    if value is None:
        return datetime.now(KYIV_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=KYIV_TZ)
    return value.astimezone(KYIV_TZ)


def today_kyiv() -> date:
    """Return the current calendar date used by every bot feature."""

    return to_kyiv_datetime().date()


def _to_datetime(value: datetime | int) -> datetime:
    """Normalize a datetime or Unix timestamp to Europe/Kyiv."""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=KYIV_TZ)
        return value.astimezone(KYIV_TZ)

    return datetime.fromtimestamp(int(value), tz=KYIV_TZ)


def format_ua_date(value: datetime | int) -> str:
    """Форматує дату у вигляді: 30 жовтня 2026 року."""

    dt = _to_datetime(value)
    month = _MONTHS_UA.get(dt.month, "")
    return f"{dt.day} {month} {dt.year} року"


def format_ua_datetime(value: datetime | int, *, include_weekday: bool = True) -> str:
    """Format a full localized event datetime."""

    dt = _to_datetime(value)
    rendered = f"{format_ua_date(dt)} о {dt:%H:%M}"
    if include_weekday:
        rendered += f" ({_WEEKDAYS_UA[dt.weekday()]})"
    return rendered


def parse_user_time(value: str) -> datetime_time:
    """Parse strict HH:MM input and reject non-canonical values."""

    parsed = datetime.strptime(value, "%H:%M").time()
    if parsed.strftime("%H:%M") != value:
        raise ValueError("time must use HH:MM format")
    return parsed


def localize_kyiv_datetime(value: datetime) -> datetime:
    """Attach Kyiv timezone and reject DST gaps or ambiguous wall times."""

    if value.tzinfo is not None:
        return value.astimezone(KYIV_TZ)

    candidates: list[datetime] = []
    seen_offsets = set()
    for fold in (0, 1):
        candidate = value.replace(tzinfo=KYIV_TZ, fold=fold)
        roundtrip = (
            candidate.astimezone(timezone.utc)
            .astimezone(KYIV_TZ)
            .replace(tzinfo=None)
        )
        if roundtrip != value:
            continue
        offset = candidate.utcoffset()
        if offset not in seen_offsets:
            seen_offsets.add(offset)
            candidates.append(candidate)

    if not candidates:
        raise ValueError("local datetime does not exist in Europe/Kyiv")
    if len(candidates) > 1:
        raise ValueError("local datetime is ambiguous in Europe/Kyiv")
    return candidates[0]


def combine_kyiv_datetime(day: date, clock: datetime_time) -> datetime:
    """Combine calendar and strict time inputs into an aware Kyiv datetime."""

    return localize_kyiv_datetime(datetime.combine(day, clock))


def to_utc_timestamp(value: datetime) -> int:
    """Convert an aware datetime to a whole-second Unix timestamp."""

    if value.tzinfo is None:
        raise ValueError("UTC conversion requires an aware datetime")
    return int(value.astimezone(timezone.utc).timestamp())
