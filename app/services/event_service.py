"""Business rules for event drafts and first publication."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html import escape
from typing import Any, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup

from app.core.config import MAIN_CHAT_ID
from app.core.dates import (
    combine_kyiv_datetime,
    format_ua_datetime,
    parse_user_time,
    to_kyiv_datetime,
    to_utc_timestamp,
)
from app.core.event_types import (
    EVENT_DRAFT_TTL_SECONDS,
    EVENT_MIN_CREATION_LEAD_SECONDS,
    EVENT_REGISTRATION_CLOSE_SECONDS,
    EVENT_SAFE_WINDOW_SECONDS,
    EventStatus,
    EventType,
)
from app.dao import events as events_dao
from app.services.event_render import render_public_card


logger = logging.getLogger(__name__)


def _public_markup(card: dict[str, Any], factory):
    if card.get("status") in {
        EventStatus.STARTED.value,
        EventStatus.AWAITING_REVIEW.value,
        EventStatus.COMPLETED.value,
        EventStatus.CANCELLED.value,
        EventStatus.ANNULLED.value,
    }:
        return None
    return factory(int(card["id"]))


class EventValidationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedDraft:
    title: str
    event_type: str
    description: str | None
    starts_at_utc: int
    safe_until_utc: int
    registration_closes_at_utc: int


@dataclass(frozen=True)
class PublicationResult:
    event_id: int
    status: str
    title: str


@dataclass(frozen=True)
class RepublicationResult:
    event_id: int
    status: str
    title: str


def now_timestamp() -> int:
    return int(time.time())


def normalize_title(value: str) -> str:
    return " ".join(value.split())


def normalize_description(value: str) -> str | None:
    normalized = value.strip()
    return normalized or None


def _draft_expiry(now: int) -> int:
    return now + EVENT_DRAFT_TTL_SECONDS


async def load_draft(
    admin_id: int,
    *,
    now: int | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    current = now_timestamp() if now is None else now
    return await events_dao.load_draft(admin_id, now=current)


async def create_draft(
    admin_id: int,
    *,
    menu_chat_id: int,
    menu_message_id: int,
    now: int | None = None,
) -> dict[str, Any]:
    current = now_timestamp() if now is None else now
    return await events_dao.create_draft(
        admin_id,
        menu_chat_id=menu_chat_id,
        menu_message_id=menu_message_id,
        now=current,
        expires_at=_draft_expiry(current),
    )


async def save_draft_field(
    admin_id: int,
    field: str,
    value: str | None,
    *,
    menu_chat_id: int,
    menu_message_id: int,
    now: int | None = None,
) -> dict[str, Any]:
    current = now_timestamp() if now is None else now
    draft, expired = await events_dao.load_draft(admin_id, now=current)
    if draft is None:
        raise events_dao.DraftNotFoundError("expired" if expired else "missing")
    target_status = draft.get("target_event_status")
    if draft.get("draft_kind") == "create":
        if target_status and target_status != EventStatus.DRAFT.value:
            raise events_dao.PublicationStateError
    elif target_status not in {
        EventStatus.PUBLISHED.value,
        EventStatus.REGISTRATION_CLOSED.value,
        EventStatus.STARTED.value,
        EventStatus.AWAITING_REVIEW.value,
    }:
        raise events_dao.PublicationStateError
    payload = dict(draft["payload"])

    if field == "title":
        normalized = normalize_title(value or "")
        if not 3 <= len(normalized) <= 100:
            raise EventValidationError(
                "title",
                "Назва події повинна містити від 3 до 100 символів.",
            )
        payload[field] = normalized
    elif field == "description":
        normalized_description = normalize_description(value or "")
        if normalized_description is not None and len(normalized_description) > 1000:
            raise EventValidationError(
                "description",
                "Опис події не може перевищувати 1000 символів.",
            )
        if normalized_description is None:
            payload.pop(field, None)
        else:
            payload[field] = normalized_description
    elif field == "time":
        try:
            parsed_time = parse_user_time((value or "").strip())
        except ValueError as exc:
            raise EventValidationError(
                "time",
                "Введіть реальний час у форматі ГГ:ХХ, наприклад 21:00.",
            ) from exc
        payload[field] = parsed_time.strftime("%H:%M")
    elif field == "date":
        try:
            payload[field] = date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise EventValidationError("date", "Оберіть коректну дату.") from exc
    elif field == "event_type":
        try:
            payload[field] = EventType(str(value)).value
        except ValueError as exc:
            raise EventValidationError("event_type", "Оберіть тип події.") from exc
    else:
        raise ValueError(f"unsupported draft field: {field}")

    return await events_dao.save_draft(
        admin_id,
        payload,
        menu_chat_id=menu_chat_id,
        menu_message_id=menu_message_id,
        now=current,
        expires_at=_draft_expiry(current),
    )


async def touch_draft_menu(
    admin_id: int,
    *,
    menu_chat_id: int,
    menu_message_id: int,
    now: int | None = None,
) -> None:
    current = now_timestamp() if now is None else now
    await events_dao.update_draft_menu(
        admin_id,
        menu_chat_id=menu_chat_id,
        menu_message_id=menu_message_id,
        now=current,
        expires_at=_draft_expiry(current),
    )


async def validate_draft(
    draft: dict[str, Any],
    *,
    now: int | None = None,
) -> ValidatedDraft:
    current = now_timestamp() if now is None else now
    payload = draft.get("payload") or {}
    missing = [
        key
        for key in ("title", "event_type", "date", "time")
        if not payload.get(key)
    ]
    if missing:
        raise EventValidationError(
            "missing",
            "Неможливо перейти до публікації. Заповніть назву, тип, дату й час.",
        )

    title = normalize_title(str(payload["title"]))
    if not 3 <= len(title) <= 100:
        raise EventValidationError(
            "title",
            "Назва події повинна містити від 3 до 100 символів.",
        )
    try:
        event_type = EventType(str(payload["event_type"])).value
        event_date = date.fromisoformat(str(payload["date"]))
        event_time = parse_user_time(str(payload["time"]))
        starts_local = combine_kyiv_datetime(event_date, event_time)
    except ValueError as exc:
        raise EventValidationError(
            "datetime",
            "Дата або час події некоректні. Оберіть дату та введіть час повторно.",
        ) from exc

    starts_at = to_utc_timestamp(starts_local)
    earliest = current + EVENT_MIN_CREATION_LEAD_SECONDS
    earliest = ((earliest + 59) // 60) * 60
    original_start = draft.get("target_event_starts_at_utc")
    start_changed = original_start is None or starts_at != int(original_start)
    if start_changed and starts_at < earliest:
        earliest_dt = datetime.fromtimestamp(earliest, tz=timezone.utc)
        raise EventValidationError(
            "lead_time",
            "Подію потрібно запланувати щонайменше за 24 години. "
            f"Найраніший доступний час: {format_ua_datetime(earliest_dt)}.",
        )

    target_event_id = draft.get("target_event_id")
    conflict = await events_dao.find_start_conflict(
        starts_at,
        exclude_event_id=int(target_event_id) if target_event_id is not None else None,
    )
    if conflict:
        raise EventValidationError(
            "conflict",
            f'На цей час уже заплановано подію «{conflict["title"]}». '
            "Виберіть іншу дату або час.",
        )

    description = normalize_description(str(payload.get("description") or ""))
    if description is not None and len(description) > 1000:
        raise EventValidationError(
            "description",
            "Опис події не може перевищувати 1000 символів.",
        )
    return ValidatedDraft(
        title=title,
        event_type=event_type,
        description=description,
        starts_at_utc=starts_at,
        safe_until_utc=starts_at - EVENT_SAFE_WINDOW_SECONDS,
        registration_closes_at_utc=starts_at
        - EVENT_REGISTRATION_CLOSE_SECONDS,
    )


def preview_event(validated: ValidatedDraft) -> dict[str, Any]:
    return {
        "title": validated.title,
        "event_type": validated.event_type,
        "description": validated.description,
        "starts_at_utc": validated.starts_at_utc,
        "safe_until_utc": validated.safe_until_utc,
        "registration_closes_at_utc": validated.registration_closes_at_utc,
        "participants": [],
        "thinking_count": 0,
        "declined_count": 0,
    }


async def create_edit_draft(
    admin_id: int,
    event_id: int,
    *,
    admin_level: int,
    menu_chat_id: int,
    menu_message_id: int,
    now: int | None = None,
) -> dict[str, Any]:
    current = now_timestamp() if now is None else now
    card = await events_dao.get_event_card(event_id)
    if not card or card["status"] not in {
        EventStatus.PUBLISHED.value,
        EventStatus.REGISTRATION_CLOSED.value,
        EventStatus.STARTED.value,
        EventStatus.AWAITING_REVIEW.value,
    }:
        raise events_dao.EventEditPermissionError
    if admin_level < 4 and current >= int(card["starts_at_utc"]):
        raise events_dao.EventEditPermissionError
    starts = to_kyiv_datetime(
        datetime.fromtimestamp(int(card["starts_at_utc"]), tz=timezone.utc)
    )
    payload = {
        "title": str(card["title"]),
        "event_type": str(card["event_type"]),
        "date": starts.date().isoformat(),
        "time": starts.strftime("%H:%M"),
    }
    if card.get("description"):
        payload["description"] = str(card["description"])
    return await events_dao.create_edit_draft(
        admin_id,
        event_id,
        base_version=int(card["version"]),
        payload=payload,
        menu_chat_id=menu_chat_id,
        menu_message_id=menu_message_id,
        now=current,
        expires_at=_draft_expiry(current),
    )


async def apply_edit_draft(
    bot: Bot,
    admin_id: int,
    *,
    admin_level: int,
    reply_markup_factory: Callable[[int], InlineKeyboardMarkup],
    now: int | None = None,
) -> dict[str, Any]:
    current = now_timestamp() if now is None else now
    draft, expired = await events_dao.load_draft(admin_id, now=current)
    if not draft or draft.get("draft_kind") != "edit":
        raise events_dao.DraftNotFoundError("expired" if expired else "missing")
    validated = await validate_draft(draft, now=current)
    try:
        result = await events_dao.apply_edit_draft(
            admin_id,
            admin_level=admin_level,
            title=validated.title,
            event_type=validated.event_type,
            description=validated.description,
            starts_at_utc=validated.starts_at_utc,
            safe_until_utc=validated.safe_until_utc,
            registration_closes_at_utc=validated.registration_closes_at_utc,
            now=current,
        )
    except events_dao.EventConflictError as exc:
        raise EventValidationError(
            "conflict",
            f'На цей час уже заплановано подію «{exc.title}». Виберіть інший час.',
        ) from exc

    if result["rescheduled"]:
        from app.core.db import cancel_pending_tasks
        from app.services.event_jobs import (
            EVENT_AUTO_REMINDER_TASK,
            EVENT_REGISTRATION_CLOSE_TASK,
            EVENT_REVIEW_CREATE_TASK,
            EVENT_REVIEW_REMINDER_TASK,
            EVENT_START_TASK,
            schedule_event_jobs,
        )

        try:
            for task_type in (
                EVENT_AUTO_REMINDER_TASK,
                EVENT_REGISTRATION_CLOSE_TASK,
                EVENT_START_TASK,
                EVENT_REVIEW_CREATE_TASK,
                EVENT_REVIEW_REMINDER_TASK,
            ):
                await cancel_pending_tasks(task_type, user_id=result["event_id"])
            await schedule_event_jobs(
                result["event_id"],
                starts_at_utc=result["starts_at_utc"],
                registration_closes_at_utc=result["registration_closes_at_utc"],
                version=result["version"],
                now=current,
            )
        except Exception:
            logger.exception(
                "event jobs will require startup reconciliation",
                extra={"event_id": result["event_id"]},
            )

        old_reminder_id = result.get("old_reminder_message_id")
        if old_reminder_id:
            try:
                await bot.edit_message_text(
                    "⚠️ Це нагадування більше не актуальне: дату або час події змінено.",
                    chat_id=MAIN_CHAT_ID,
                    message_id=int(old_reminder_id),
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                logger.info(
                    "old event reminder could not be marked stale",
                    extra={"event_id": result["event_id"]},
                )

    await refresh_event_card(
        bot,
        result["event_id"],
        reply_markup_factory=reply_markup_factory,
    )
    if result["rescheduled"] and result["respondents"]:
        card = await events_dao.get_event_card(result["event_id"])
        publication = card.get("publication") if card else None
        if publication and publication.get("message_id"):
            mentions = "\n".join(
                f'<a href="tg://user?id={int(row["user_id"])}">'
                f'{escape(str(row["display_name"]))}</a>'
                for row in result["respondents"]
            )
            try:
                await bot.send_message(
                    int(publication["chat_id"]),
                    f'📅 Дату або час події «{escape(result["title"])}» змінено. '
                    "Попередні відповіді скинуто. Підтвердьте участь повторно:\n\n"
                    f"{mentions}",
                    parse_mode="HTML",
                    reply_to_message_id=int(publication["message_id"]),
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception(
                    "event reschedule notice failed",
                    extra={"event_id": result["event_id"]},
                )
    return result


async def publish_draft(
    bot: Bot,
    admin_id: int,
    *,
    reply_markup_factory: Callable[[int], InlineKeyboardMarkup],
    now: int | None = None,
) -> PublicationResult:
    current = now_timestamp() if now is None else now
    draft, expired = await events_dao.load_draft(admin_id, now=current)
    if draft is None:
        raise events_dao.DraftNotFoundError("expired" if expired else "missing")
    validated = await validate_draft(draft, now=current)

    try:
        reservation = await events_dao.reserve_draft_publication(
            admin_id,
            title=validated.title,
            event_type=validated.event_type,
            description=validated.description,
            starts_at_utc=validated.starts_at_utc,
            safe_until_utc=validated.safe_until_utc,
            registration_closes_at_utc=validated.registration_closes_at_utc,
            main_chat_id=MAIN_CHAT_ID,
            now=current,
        )
    except events_dao.EventConflictError as exc:
        raise EventValidationError(
            "conflict",
            f'На цей час уже заплановано подію «{exc.title}». '
            "Виберіть іншу дату або час.",
        ) from exc

    if not reservation.should_send:
        return PublicationResult(
            event_id=reservation.event_id,
            status=reservation.status,
            title=validated.title,
        )

    card = await events_dao.get_event_card(reservation.event_id)
    assert card is not None
    try:
        sent = await bot.send_message(
            MAIN_CHAT_ID,
            render_public_card(card),
            parse_mode="HTML",
            reply_markup=reply_markup_factory(reservation.event_id),
            disable_web_page_preview=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await events_dao.release_failed_publication(
            reservation.event_id,
            admin_id,
            error=str(exc),
            now=now_timestamp(),
        )
        raise
    except Exception as exc:
        await events_dao.mark_publication_unknown(
            reservation.event_id,
            actor_id=admin_id,
            error=str(exc),
            now=now_timestamp(),
        )
        logger.exception(
            "event publication result is unknown",
            extra={"event_id": reservation.event_id},
        )
        return PublicationResult(
            event_id=reservation.event_id,
            status=EventStatus.PUBLICATION_UNKNOWN.value,
            title=validated.title,
        )

    await events_dao.complete_publication(
        reservation.event_id,
        sent.message_id,
        now=now_timestamp(),
    )
    from app.services.event_jobs import schedule_event_jobs

    try:
        await schedule_event_jobs(
            reservation.event_id,
            starts_at_utc=validated.starts_at_utc,
            registration_closes_at_utc=validated.registration_closes_at_utc,
            version=int(card["version"]),
        )
    except Exception:
        logger.exception(
            "published event jobs will require startup reconciliation",
            extra={"event_id": reservation.event_id},
        )
    return PublicationResult(
        event_id=reservation.event_id,
        status=EventStatus.PUBLISHED.value,
        title=validated.title,
    )


async def reconcile_startup(*, now: int | None = None) -> int:
    current = now_timestamp() if now is None else now
    reconciled = await events_dao.reconcile_incomplete_publications(now=current)
    from app.services.event_jobs import rebuild_event_jobs, rebuild_review_jobs

    await rebuild_event_jobs(now=current)
    await rebuild_review_jobs(now=current)
    return reconciled


def _message_is_missing(exc: TelegramBadRequest) -> bool:
    error = str(exc).casefold()
    return any(
        marker in error
        for marker in (
            "message to edit not found",
            "message not found",
            "message_id_invalid",
        )
    )


async def refresh_public_cards(
    bot: Bot,
    *,
    reply_markup_factory: Callable[[int], InlineKeyboardMarkup],
) -> int:
    """Refresh active cards and flag deleted messages on admin interaction."""

    missing = 0
    for event_id in await events_dao.list_refreshable_event_ids():
        card = await events_dao.get_event_card(event_id)
        if not card or not card.get("publication"):
            continue
        publication = card["publication"]
        try:
            await bot.edit_message_text(
                render_public_card(card),
                chat_id=int(publication["chat_id"]),
                message_id=int(publication["message_id"]),
                parse_mode="HTML",
                reply_markup=_public_markup(card, reply_markup_factory),
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                continue
            if _message_is_missing(exc) and await events_dao.mark_publication_missing(
                event_id,
                now=now_timestamp(),
            ):
                missing += 1
            else:
                logger.warning(
                    "event card refresh failed",
                    extra={"event_id": event_id, "error": str(exc)},
                )
        except TelegramForbiddenError as exc:
            logger.warning(
                "event card refresh forbidden",
                extra={"event_id": event_id, "error": str(exc)},
            )
    return missing


async def refresh_event_card(
    bot: Bot,
    event_id: int,
    *,
    reply_markup_factory: Callable[[int], InlineKeyboardMarkup],
) -> bool:
    """Refresh one committed event card and detect a deleted publication."""

    card = await events_dao.get_event_card(event_id)
    if not card or not card.get("publication"):
        return False
    publication = card["publication"]
    try:
        await bot.edit_message_text(
            render_public_card(card),
            chat_id=int(publication["chat_id"]),
            message_id=int(publication["message_id"]),
            parse_mode="HTML",
            reply_markup=_public_markup(card, reply_markup_factory),
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return True
        if _message_is_missing(exc):
            await events_dao.mark_publication_missing(event_id, now=now_timestamp())
        else:
            logger.warning(
                "event card refresh failed",
                extra={"event_id": event_id, "error": str(exc)},
            )
        return False
    except TelegramForbiddenError as exc:
        logger.warning(
            "event card refresh forbidden",
            extra={"event_id": event_id, "error": str(exc)},
        )
        return False
    return True


async def republish_event(
    bot: Bot,
    event_id: int,
    actor_id: int,
    *,
    reply_markup_factory: Callable[[int], InlineKeyboardMarkup],
) -> RepublicationResult:
    reservation = await events_dao.reserve_republication(
        event_id,
        actor_id,
        now=now_timestamp(),
    )
    card = await events_dao.get_event_card(event_id)
    if not card:
        raise events_dao.PublicationStateError
    if not reservation.should_send:
        return RepublicationResult(event_id, "unknown", str(card["title"]))

    try:
        sent = await bot.send_message(
            MAIN_CHAT_ID,
            render_public_card(card),
            parse_mode="HTML",
            reply_markup=reply_markup_factory(event_id),
            disable_web_page_preview=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await events_dao.release_failed_republication(
            reservation,
            actor_id=actor_id,
            error=str(exc),
            now=now_timestamp(),
        )
        raise
    except Exception as exc:
        await events_dao.mark_republication_unknown(
            reservation,
            actor_id=actor_id,
            error=str(exc),
            now=now_timestamp(),
        )
        logger.exception(
            "event republication result is unknown",
            extra={"event_id": event_id},
        )
        return RepublicationResult(event_id, "unknown", str(card["title"]))

    await events_dao.complete_republication(
        reservation,
        sent.message_id,
        actor_id=actor_id,
        now=now_timestamp(),
    )
    return RepublicationResult(event_id, "published", str(card["title"]))
