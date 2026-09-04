"""Event reminder delivery with DB-first no-duplicate reservations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.core.dates import KYIV_TZ
from app.dao import event_lifecycle as lifecycle_dao
from app.dao import events as events_dao


logger = logging.getLogger(__name__)


def _reply_target_missing(exc: TelegramBadRequest) -> bool:
    error = str(exc).casefold()
    return any(
        marker in error
        for marker in (
            "message to be replied not found",
            "reply message not found",
            "message to reply not found",
        )
    )


async def _record_definite_reminder_failure(
    event_id: int,
    *,
    kind: str,
    scheduled_at: int,
    exc: TelegramBadRequest | TelegramForbiddenError,
    now: int,
) -> None:
    await lifecycle_dao.notification_finished_without_send(
        event_id,
        kind=kind,
        scheduled_at=scheduled_at,
        status="failed",
        reason=str(exc),
        now=now,
    )
    if isinstance(exc, TelegramBadRequest) and _reply_target_missing(exc):
        await events_dao.mark_publication_missing(event_id, now=now)


@dataclass(frozen=True)
class ManualReminderResult:
    code: str
    title: str = ""
    retry_after: int = 0


def _mention(row: dict) -> str:
    return (
        f'<a href="tg://user?id={int(row["user_id"])}">'
        f'{escape(str(row["display_name"]))}</a>'
    )


def render_auto_reminder(context: dict) -> str:
    starts = datetime.fromtimestamp(int(context["starts_at_utc"]), tz=KYIV_TZ)
    closes = datetime.fromtimestamp(
        int(context["registration_closes_at_utc"]),
        tz=KYIV_TZ,
    )
    lines = [
        f'🔔 <b>Нагадування про подію «{escape(str(context["title"]))}»</b>',
        "",
        f"Початок сьогодні о {starts:%H:%M} — залишилося 3 години.",
    ]
    if context["going"]:
        lines.extend(("", "Підтвердили участь:"))
        lines.extend(_mention(row) for row in context["going"])
    if context["thinking"]:
        lines.extend(("", "Ще думають:"))
        lines.extend(_mention(row) for row in context["thinking"])
    lines.extend(
        (
            "",
            "До завершення безпечного терміну залишилася 1 година.",
            f"Реєстрація закриється о {closes:%H:%M}.",
        )
    )
    return "\n".join(lines)


def render_manual_reminder(context: dict, audience: str) -> str:
    starts = datetime.fromtimestamp(int(context["starts_at_utc"]), tz=KYIV_TZ)
    lines = [
        f'🔔 <b>Нагадування про подію «{escape(str(context["title"]))}»</b>',
        "",
        f"Початок {starts:%d.%m.%Y} о {starts:%H:%M}.",
    ]
    if audience in {"going", "both"} and context["going"]:
        lines.extend(("", "Підтвердили участь:"))
        lines.extend(_mention(row) for row in context["going"])
    if audience in {"thinking", "both"} and context["thinking"]:
        lines.extend(("", "Ще думають:"))
        lines.extend(_mention(row) for row in context["thinking"])
    return "\n".join(lines)


async def send_auto_reminder(
    bot: Bot,
    event_id: int,
    *,
    expected_at: int,
    now: int,
) -> str:
    context = await lifecycle_dao.reminder_context(event_id)
    if not context or int(context["starts_at_utc"]) - 3 * 60 * 60 != expected_at:
        return "stale"
    if not await lifecycle_dao.reserve_auto_reminder(
        event_id,
        scheduled_at=expected_at,
        now=now,
    ):
        return "duplicate"
    if (
        context["status"] not in {"published", "registration_closed"}
        or now >= int(context["registration_closes_at_utc"])
    ):
        await lifecycle_dao.notification_finished_without_send(
            event_id,
            kind="auto_reminder",
            scheduled_at=expected_at,
            status="skipped",
            reason="registration is already closed",
            now=now,
        )
        return "skipped"
    if not context["going"] and not context["thinking"]:
        await lifecycle_dao.notification_finished_without_send(
            event_id,
            kind="auto_reminder",
            scheduled_at=expected_at,
            status="skipped",
            reason="no reminder recipients",
            now=now,
        )
        return "skipped"

    try:
        sent = await bot.send_message(
            int(context["chat_id"]),
            render_auto_reminder(context),
            parse_mode="HTML",
            reply_to_message_id=int(context["message_id"]),
            disable_web_page_preview=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await _record_definite_reminder_failure(
            event_id,
            kind="auto_reminder",
            scheduled_at=expected_at,
            exc=exc,
            now=now,
        )
        raise
    except Exception:
        logger.exception(
            "event auto reminder result is unknown",
            extra={"event_id": event_id},
        )
        return "unknown"

    await lifecycle_dao.notification_sent(
        event_id,
        kind="auto_reminder",
        scheduled_at=expected_at,
        message_id=sent.message_id,
        now=now,
    )
    return "sent"


async def send_manual_reminder(
    bot: Bot,
    event_id: int,
    *,
    audience: str,
    actor_id: int,
    now: int,
) -> ManualReminderResult:
    context = await lifecycle_dao.reminder_context(event_id)
    if not context or now >= int(context["starts_at_utc"]):
        return ManualReminderResult("unavailable")
    if now >= int(context["registration_closes_at_utc"]):
        context["thinking"] = []
    recipients = []
    if audience in {"going", "both"}:
        recipients.extend(context["going"])
    if audience in {"thinking", "both"}:
        recipients.extend(context["thinking"])
    if not recipients:
        return ManualReminderResult("empty", str(context["title"]))

    reservation = await lifecycle_dao.reserve_manual_reminder(
        event_id,
        audience=audience,
        actor_id=actor_id,
        now=now,
    )
    if not reservation.allowed:
        code = "cooldown" if reservation.retry_after else "unavailable"
        return ManualReminderResult(
            code,
            str(context["title"]),
            reservation.retry_after,
        )
    try:
        sent = await bot.send_message(
            int(context["chat_id"]),
            render_manual_reminder(context, audience),
            parse_mode="HTML",
            reply_to_message_id=int(context["message_id"]),
            disable_web_page_preview=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await _record_definite_reminder_failure(
            event_id,
            kind="manual_reminder",
            scheduled_at=reservation.scheduled_at,
            exc=exc,
            now=now,
        )
        raise
    except Exception:
        logger.exception(
            "event manual reminder result is unknown",
            extra={"event_id": event_id},
        )
        return ManualReminderResult("unknown", str(context["title"]))
    await lifecycle_dao.notification_sent(
        event_id,
        kind="manual_reminder",
        scheduled_at=reservation.scheduled_at,
        message_id=sent.message_id,
        now=now,
    )
    return ManualReminderResult("sent", str(context["title"]))
