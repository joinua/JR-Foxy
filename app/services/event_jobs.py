"""Scheduled maintenance and lifecycle jobs for the event domain."""

import json
import time

from app.core.db import cancel_pending_tasks, schedule_task
from app.dao import events as events_dao
from app.dao import event_lifecycle as lifecycle_dao
from app.services import event_service
from app.services.event_notifications import send_auto_reminder


EVENT_DRAFT_CLEANUP_TASK = "event_draft_cleanup"
EVENT_DRAFT_CLEANUP_INTERVAL_SECONDS = 60 * 60
EVENT_AUTO_REMINDER_TASK = "event_auto_reminder"
EVENT_REGISTRATION_CLOSE_TASK = "event_registration_close"
EVENT_START_TASK = "event_start"


def _payload(event_id: int, expected_at: int) -> str:
    return json.dumps(
        {"event_id": event_id, "expected_at": expected_at},
        separators=(",", ":"),
    )


def parse_event_task(task: dict) -> tuple[int, int]:
    payload = json.loads(str(task.get("payload_json") or "{}"))
    return int(payload["event_id"]), int(payload["expected_at"])


async def schedule_event_jobs(
    event_id: int,
    *,
    starts_at_utc: int,
    registration_closes_at_utc: int,
    version: int,
    now: int | None = None,
) -> None:
    current = int(time.time()) if now is None else now
    reminder_at = starts_at_utc - 3 * 60 * 60
    jobs = (
        (EVENT_AUTO_REMINDER_TASK, reminder_at),
        (EVENT_REGISTRATION_CLOSE_TASK, registration_closes_at_utc),
        (EVENT_START_TASK, starts_at_utc),
    )
    for task_type, expected_at in jobs:
        await schedule_task(
            task_type=task_type,
            run_at=max(current, expected_at),
            user_id=event_id,
            payload_json=_payload(event_id, expected_at),
            dedupe_key=f"event:{event_id}:{task_type}:{expected_at}:v{version}",
        )


async def rebuild_event_jobs(*, now: int | None = None) -> int:
    current = int(time.time()) if now is None else now
    schedules = await lifecycle_dao.list_active_schedules()
    for event in schedules:
        await schedule_event_jobs(
            int(event["id"]),
            starts_at_utc=int(event["starts_at_utc"]),
            registration_closes_at_utc=int(event["registration_closes_at_utc"]),
            version=int(event["version"]),
            now=current,
        )
    return len(schedules)


async def run_event_auto_reminder(bot, task: dict) -> None:
    event_id, expected_at = parse_event_task(task)
    await send_auto_reminder(
        bot,
        event_id,
        expected_at=expected_at,
        now=int(time.time()),
    )


async def run_event_registration_close(bot, task: dict) -> None:
    from app.handlers.events.keyboards import public_event_keyboard

    event_id, expected_at = parse_event_task(task)
    transition = await lifecycle_dao.close_registration(
        event_id,
        expected_at=expected_at,
        now=int(time.time()),
    )
    if transition.changed:
        await event_service.refresh_event_card(
            bot,
            event_id,
            reply_markup_factory=public_event_keyboard,
        )


async def run_event_start(bot, task: dict) -> None:
    from app.handlers.events.keyboards import public_event_keyboard

    event_id, expected_at = parse_event_task(task)
    transition = await lifecycle_dao.start_event(
        event_id,
        expected_at=expected_at,
        now=int(time.time()),
    )
    if transition.changed:
        await event_service.refresh_event_card(
            bot,
            event_id,
            reply_markup_factory=public_event_keyboard,
        )


async def register_event_draft_cleanup_task() -> None:
    await cancel_pending_tasks(EVENT_DRAFT_CLEANUP_TASK)
    await schedule_task(
        EVENT_DRAFT_CLEANUP_TASK,
        int(time.time()) + EVENT_DRAFT_CLEANUP_INTERVAL_SECONDS,
    )


async def run_event_draft_cleanup() -> None:
    await events_dao.cleanup_expired_drafts(now=int(time.time()))
    await schedule_task(
        EVENT_DRAFT_CLEANUP_TASK,
        int(time.time()) + EVENT_DRAFT_CLEANUP_INTERVAL_SECONDS,
    )
