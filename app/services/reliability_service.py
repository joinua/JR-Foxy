"""Reliability calculation and presentation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from typing import Any

from app.core.dates import KYIV_TZ
from app.core.event_types import RELIABILITY_MIN_EVENTS, RELIABILITY_WINDOW_SIZE
from app.dao import reliability as reliability_dao


ZONE_GRAY = "gray"
ZONE_GREEN = "green"
ZONE_YELLOW = "yellow"
ZONE_RED = "red"

ZONE_ICONS = {
    ZONE_GRAY: "⚪",
    ZONE_GREEN: "🟢",
    ZONE_YELLOW: "🟡",
    ZONE_RED: "🔴",
}

RESULT_ICONS = {
    "present": "✅",
    "no_show": "❌",
    "late_decline": "🕒",
    "excluded": "➖",
}

MONTHS_GENITIVE_UA = {
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


@dataclass(frozen=True)
class ReliabilitySummary:
    user_id: int
    evaluated_count: int
    present: int
    no_show: int
    late_decline: int
    percentage: int | None
    zone: str
    recent: tuple[dict[str, Any], ...] = ()

    @property
    def qualified(self) -> bool:
        return self.evaluated_count >= RELIABILITY_MIN_EVENTS

    @property
    def icon(self) -> str:
        return ZONE_ICONS[self.zone]


def calculate_summary(
    user_id: int,
    rating_rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]] | None = None,
) -> ReliabilitySummary:
    """Apply A/(A+P+0.5L) with exact decimal half-up rounding."""

    window = rating_rows[:RELIABILITY_WINDOW_SIZE]
    present = sum(row["result"] == "present" for row in window)
    no_show = sum(row["result"] == "no_show" for row in window)
    late_decline = sum(row["result"] == "late_decline" for row in window)
    evaluated_count = len(window)
    denominator = Decimal(present + no_show) + Decimal(late_decline) / 2
    percentage = None
    if denominator:
        percentage = int(
            (Decimal(present) * 100 / denominator).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )

    if evaluated_count < RELIABILITY_MIN_EVENTS:
        zone = ZONE_GRAY
    elif (percentage or 0) >= 70:
        zone = ZONE_GREEN
    elif (percentage or 0) >= 40:
        zone = ZONE_YELLOW
    else:
        zone = ZONE_RED
    return ReliabilitySummary(
        user_id=user_id,
        evaluated_count=evaluated_count,
        present=present,
        no_show=no_show,
        late_decline=late_decline,
        percentage=percentage,
        zone=zone,
        recent=tuple(recent_rows or ()),
    )


async def get_summary(user_id: int, *, include_recent: bool = False) -> ReliabilitySummary:
    rating_rows = await reliability_dao.get_rating_rows(
        user_id,
        limit=RELIABILITY_WINDOW_SIZE,
    )
    recent_rows = (
        await reliability_dao.get_recent_rows(user_id, limit=5)
        if include_recent
        else []
    )
    return calculate_summary(user_id, rating_rows, recent_rows)


def render_profile_line(summary: ReliabilitySummary) -> str:
    if not summary.qualified:
        return (
            "⚪ Надійність: недостатньо даних · "
            f"{summary.evaluated_count} із {RELIABILITY_MIN_EVENTS} подій"
        )
    return (
        f"{summary.icon} Надійність: {summary.percentage}% · "
        f"враховано {summary.evaluated_count} подій"
    )


def _short_event_date(starts_at_utc: int) -> str:
    value = datetime.fromtimestamp(starts_at_utc, tz=KYIV_TZ)
    return f"{value.day} {MONTHS_GENITIVE_UA[value.month]}"


def _basis_text(count: int) -> str:
    if count == RELIABILITY_WINDOW_SIZE:
        return f"останні {RELIABILITY_WINDOW_SIZE} оцінених подій"
    if count == 0:
        return "оцінених подій ще немає"
    if count == 1:
        return "1 оцінена подія"
    if count in {2, 3, 4}:
        return f"усі {count} оцінені події"
    return f"усі {count} оцінених подій"


def render_details(summary: ReliabilitySummary, nickname: str) -> str:
    if summary.qualified:
        rating = f"{summary.icon} {summary.percentage}%"
    else:
        rating = (
            "⚪ недостатньо даних · "
            f"{summary.evaluated_count} із {RELIABILITY_MIN_EVENTS} подій"
        )
    basis = _basis_text(summary.evaluated_count)
    lines = [
        "📊 <b>Статистика надійності</b>",
        "",
        f"Гравець: {escape(nickname)}",
        f"Рейтинг: {rating}",
        f"Основа: {basis}",
        "",
        f"✅ Присутність: {summary.present}",
        f"🕒 Пізні відмови: {summary.late_decline}",
        f"❌ Неявки: {summary.no_show}",
        "",
        "Останні 5 подій:",
    ]
    if not summary.recent:
        lines.append("Ще немає завершених оцінок.")
    for row in summary.recent:
        icon = RESULT_ICONS.get(str(row["result"]), "•")
        lines.append(
            f'{icon} {_short_event_date(int(row["starts_at_utc"]))} — '
            f'«{escape(str(row["title"]))}»'
        )
        if row["result"] == "excluded" and row.get("exclusion_reason"):
            lines.append(f'   Причина: {escape(str(row["exclusion_reason"]))}')
    return "\n".join(lines)
