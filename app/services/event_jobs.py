"""Scheduled maintenance jobs for the event domain."""

import time

from app.core.db import cancel_pending_tasks, schedule_task
from app.dao import events as events_dao


EVENT_DRAFT_CLEANUP_TASK = "event_draft_cleanup"
EVENT_DRAFT_CLEANUP_INTERVAL_SECONDS = 60 * 60


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
