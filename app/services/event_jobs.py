"""Scheduled maintenance and lifecycle jobs for the event domain."""

import json
import time

from app.core.db import cancel_pending_tasks, schedule_task
from app.dao import events as events_dao
from app.dao import event_lifecycle as lifecycle_dao
from app.dao import event_reviews as reviews_dao
from app.services import event_service
from app.services.event_notifications import send_auto_reminder


EVENT_DRAFT_CLEANUP_TASK = "event_draft_cleanup"
EVENT_DRAFT_CLEANUP_INTERVAL_SECONDS = 60 * 60
EVENT_AUTO_REMINDER_TASK = "event_auto_reminder"
EVENT_REGISTRATION_CLOSE_TASK = "event_registration_close"
EVENT_START_TASK = "event_start"
EVENT_REVIEW_CREATE_TASK = "event_review_create"
EVENT_REVIEW_REMINDER_TASK = "event_review_reminder"


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
        (EVENT_REVIEW_CREATE_TASK, starts_at_utc + 60 * 60),
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


async def rebuild_review_jobs(*, now: int | None = None) -> int:
    current = int(time.time()) if now is None else now
    schedules = await reviews_dao.list_review_schedules()
    for event in schedules:
        event_id = int(event["id"])
        starts_at = int(event["starts_at_utc"])
        if event["status"] != "awaiting_review":
            await schedule_task(
                task_type=EVENT_REVIEW_CREATE_TASK,
                run_at=max(current, starts_at + 3600),
                user_id=event_id,
                payload_json=_payload(event_id, starts_at + 3600),
                dedupe_key=(
                    f"event:{event_id}:{EVENT_REVIEW_CREATE_TASK}:"
                    f"{starts_at + 3600}:v{int(event['version'])}"
                ),
            )
        if event["status"] == "awaiting_review" and event["review_created_at"]:
            review_created_at = int(event["review_created_at"])
            last_reminder = event.get("last_review_reminder_at")
            reminder_at = (
                int(last_reminder) + 24 * 3600
                if last_reminder is not None
                else review_created_at + 6 * 3600
            )
            await schedule_task(
                task_type=EVENT_REVIEW_REMINDER_TASK,
                run_at=max(current, reminder_at),
                user_id=event_id,
                payload_json=_payload(event_id, reminder_at),
                dedupe_key=f"event:{event_id}:review_reminder:{reminder_at}",
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
        if transition.status == "cancelled":
            from app.services.event_reviews import publish_cancellation

            await publish_cancellation(bot, event_id)
        else:
            await event_service.refresh_event_card(
                bot,
                event_id,
                reply_markup_factory=public_event_keyboard,
            )


async def run_event_review_create(bot, task: dict) -> None:
    from app.services.event_reviews import create_and_send_review

    event_id, expected_at = parse_event_task(task)
    await create_and_send_review(
        bot,
        event_id,
        expected_at=expected_at,
        now=int(time.time()),
    )


async def run_event_review_reminder(bot, task: dict) -> None:
    from app.services.event_reviews import send_review_reminder

    event_id, expected_at = parse_event_task(task)
    await send_review_reminder(
        bot,
        event_id,
        expected_at=expected_at,
        now=int(time.time()),
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
