"""Transactional lifecycle transitions and notification reservations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.core import db as core_db


@dataclass(frozen=True)
class LifecycleTransition:
    event_id: int
    changed: bool
    status: str
    thinking_expired: int = 0


@dataclass(frozen=True)
class ManualReminderReservation:
    allowed: bool
    scheduled_at: int
    retry_after: int = 0


async def list_active_schedules() -> list[dict[str, Any]]:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, starts_at_utc, registration_closes_at_utc, status, version
            FROM events
            WHERE status IN ('published', 'registration_closed')
            ORDER BY starts_at_utc ASC
            """
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_schedule(event_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, title, starts_at_utc, safe_until_utc,
                   registration_closes_at_utc, status, publication_missing, version
            FROM events WHERE id=?
            """,
            (event_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def close_registration(
    event_id: int,
    *,
    expected_at: int,
    now: int,
) -> LifecycleTransition:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT status, registration_closes_at_utc, starts_at_utc
            FROM events WHERE id=?
            """,
            (event_id,),
        )
        event = await cursor.fetchone()
        if not event:
            await db.rollback()
            return LifecycleTransition(event_id, False, "missing")
        if int(event["registration_closes_at_utc"]) != expected_at:
            await db.rollback()
            return LifecycleTransition(event_id, False, "stale")
        if now < expected_at:
            await db.rollback()
            return LifecycleTransition(event_id, False, str(event["status"]))
        if event["status"] not in {"published", "registration_closed"}:
            await db.rollback()
            return LifecycleTransition(event_id, False, str(event["status"]))

        expired = await db.execute(
            """
            UPDATE event_responses
            SET status='thinking_expired', updated_at=?
            WHERE event_id=? AND status='thinking'
            """,
            (now, event_id),
        )
        changed = await db.execute(
            """
            UPDATE events SET status='registration_closed', updated_at=?
            WHERE id=? AND status='published'
            """,
            (now, event_id),
        )
        if expired.rowcount:
            await db.execute(
                """
                INSERT INTO event_audit_log (
                    event_id, action, new_value_json, created_at
                ) VALUES (?, 'thinking_expired', ?, ?)
                """,
                (event_id, json.dumps({"count": expired.rowcount}), now),
            )
        await db.commit()
        return LifecycleTransition(
            event_id,
            bool(changed.rowcount or expired.rowcount),
            "registration_closed",
            expired.rowcount,
        )


async def start_event(
    event_id: int,
    *,
    expected_at: int,
    now: int,
) -> LifecycleTransition:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT status, starts_at_utc FROM events WHERE id=?",
            (event_id,),
        )
        event = await cursor.fetchone()
        if not event:
            await db.rollback()
            return LifecycleTransition(event_id, False, "missing")
        if int(event["starts_at_utc"]) != expected_at:
            await db.rollback()
            return LifecycleTransition(event_id, False, "stale")
        if now < expected_at or event["status"] not in {
            "published",
            "registration_closed",
        }:
            await db.rollback()
            return LifecycleTransition(event_id, False, str(event["status"]))

        expired = await db.execute(
            """
            UPDATE event_responses
            SET status='thinking_expired', updated_at=?
            WHERE event_id=? AND status='thinking'
            """,
            (now, event_id),
        )
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM event_responses
            WHERE event_id=? AND status IN ('going', 'late_declined')
            """,
            (event_id,),
        )
        committed = int((await cursor.fetchone())[0])
        if committed == 0:
            status = "cancelled"
            reason = (
                "Подію автоматично скасовано: немає зареєстрованих учасників."
            )
            await db.execute(
                """
                UPDATE events
                SET status='cancelled', cancel_reason=?, cancelled_at=?, updated_at=?
                WHERE id=?
                """,
                (reason, now, now, event_id),
            )
            await db.execute(
                """
                INSERT INTO event_audit_log (event_id, action, reason, created_at)
                VALUES (?, 'event_auto_cancelled', ?, ?)
                """,
                (event_id, reason, now),
            )
        else:
            status = "started"
            await db.execute(
                "UPDATE events SET status='started', updated_at=? WHERE id=?",
                (now, event_id),
            )
            await db.execute(
                """
                INSERT INTO event_audit_log (event_id, action, created_at)
                VALUES (?, 'event_started', ?)
                """,
                (event_id, now),
            )
        await db.execute(
            "DELETE FROM event_late_confirmations WHERE event_id=?",
            (event_id,),
        )
        await db.commit()
        return LifecycleTransition(event_id, True, status, expired.rowcount)


async def reserve_auto_reminder(
    event_id: int,
    *,
    scheduled_at: int,
    now: int,
) -> bool:
    dedupe_key = f"event:{event_id}:auto_reminder:{scheduled_at}"
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            INSERT INTO event_notifications (
                event_id, kind, audience, dedupe_key, scheduled_at,
                reserved_at, status
            ) VALUES (?, 'auto_reminder', 'both', ?, ?, ?, 'reserved')
            ON CONFLICT(dedupe_key) DO UPDATE SET
                reserved_at=excluded.reserved_at,
                status='reserved',
                error=NULL
            WHERE event_notifications.status='failed'
            """,
            (event_id, dedupe_key, scheduled_at, now),
        )
        await db.commit()
        return cursor.rowcount > 0


async def notification_sent(
    event_id: int,
    *,
    kind: str,
    scheduled_at: int,
    message_id: int,
    now: int,
) -> None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute(
            """
            UPDATE event_notifications
            SET status='sent', sent_at=?, telegram_message_id=?
            WHERE event_id=? AND kind=? AND scheduled_at=? AND status='reserved'
            """,
            (now, message_id, event_id, kind, scheduled_at),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, action, new_value_json, created_at
            ) VALUES (?, 'reminder_sent', ?, ?)
            """,
            (event_id, json.dumps({"kind": kind, "message_id": message_id}), now),
        )
        await db.commit()


async def notification_finished_without_send(
    event_id: int,
    *,
    kind: str,
    scheduled_at: int,
    status: str,
    reason: str,
    now: int,
) -> None:
    if status not in {"skipped", "failed"}:
        raise ValueError("unsupported notification status")
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute(
            """
            UPDATE event_notifications
            SET status=?, skipped_at=CASE WHEN ?='skipped' THEN ? ELSE skipped_at END,
                error=?
            WHERE event_id=? AND kind=? AND scheduled_at=? AND status='reserved'
            """,
            (status, status, now, reason[:1000], event_id, kind, scheduled_at),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (event_id, action, reason, created_at)
            VALUES (?, 'reminder_skipped', ?, ?)
            """,
            (event_id, reason[:1000], now),
        )
        await db.commit()


async def reminder_context(event_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT e.id, e.title, e.starts_at_utc, e.safe_until_utc,
                   e.registration_closes_at_utc, e.status,
                   ep.chat_id, ep.message_id
            FROM events e
            JOIN event_publications ep ON ep.event_id=e.id AND ep.is_current=1
            WHERE e.id=? AND e.publication_missing=0
            """,
            (event_id,),
        )
        event = await cursor.fetchone()
        if not event:
            return None
        result = dict(event)
        cursor = await db.execute(
            """
            SELECT er.user_id, er.status,
                   COALESCE(NULLIF(trim(p.game_nickname), ''),
                            CASE WHEN er.status='going'
                                 THEN NULLIF(trim(er.nickname_snapshot), '') END,
                            er.telegram_name_snapshot,
                            NULLIF(trim(p.telegram_full_name), ''),
                            CAST(er.user_id AS TEXT)) AS display_name
            FROM event_responses er
            LEFT JOIN profiles p ON p.user_id=er.user_id
            WHERE er.event_id=? AND er.status IN ('going', 'thinking')
            ORDER BY CASE er.status WHEN 'going' THEN 0 ELSE 1 END,
                     er.joined_at ASC, er.responded_at ASC, er.user_id ASC
            """,
            (event_id,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        result["going"] = [row for row in rows if row["status"] == "going"]
        result["thinking"] = [row for row in rows if row["status"] == "thinking"]
        return result


async def list_reminder_events(now: int, limit: int = 10) -> list[dict[str, Any]]:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT e.id, e.title, e.starts_at_utc
            FROM events e
            JOIN event_publications ep ON ep.event_id=e.id AND ep.is_current=1
            WHERE e.status IN ('published', 'registration_closed')
              AND e.starts_at_utc>?
              AND e.publication_missing=0
              AND ep.message_id IS NOT NULL
            ORDER BY e.starts_at_utc ASC, e.id ASC
            LIMIT ?
            """,
            (now, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def reserve_manual_reminder(
    event_id: int,
    *,
    audience: str,
    actor_id: int,
    now: int,
) -> ManualReminderReservation:
    if audience not in {"going", "thinking", "both"}:
        raise ValueError("unsupported reminder audience")
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT starts_at_utc, status FROM events WHERE id=?
            """,
            (event_id,),
        )
        event = await cursor.fetchone()
        if (
            not event
            or event["status"] not in {"published", "registration_closed"}
            or now >= int(event["starts_at_utc"])
        ):
            await db.rollback()
            return ManualReminderReservation(False, now, 0)
        cursor = await db.execute(
            """
            SELECT MAX(COALESCE(sent_at, reserved_at))
            FROM event_notifications
            WHERE event_id=? AND kind='manual_reminder'
              AND status IN ('reserved', 'sent')
            """,
            (event_id,),
        )
        last_at = (await cursor.fetchone())[0]
        if last_at is not None and now - int(last_at) < 3600:
            await db.rollback()
            return ManualReminderReservation(
                False,
                now,
                3600 - (now - int(last_at)),
            )
        dedupe_key = f"event:{event_id}:manual:{now}:{actor_id}"
        await db.execute(
            """
            INSERT INTO event_notifications (
                event_id, kind, audience, dedupe_key, scheduled_at,
                reserved_at, status
            ) VALUES (?, 'manual_reminder', ?, ?, ?, ?, 'reserved')
            ON CONFLICT(dedupe_key) DO UPDATE SET
                audience=excluded.audience,
                reserved_at=excluded.reserved_at,
                status='reserved',
                error=NULL
            WHERE event_notifications.status='failed'
            """,
            (event_id, audience, dedupe_key, now, now),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, new_value_json, created_at
            ) VALUES (?, ?, 'reminder_reserved', ?, ?)
            """,
            (event_id, actor_id, json.dumps({"audience": audience}), now),
        )
        await db.commit()
        return ManualReminderReservation(True, now)
