"""SQLite access layer for event drafts and publication reservations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.core import db as core_db
from app.core.event_types import ACTIVE_START_RESERVATION_STATUSES, EventStatus


class DraftNotFoundError(Exception):
    """The administrator has no active draft."""


class EventConflictError(Exception):
    """Another active event already owns the requested start minute."""

    def __init__(self, title: str):
        super().__init__(title)
        self.title = title


class PublicationStateError(Exception):
    """The event cannot transition from its current publication state."""


class EventVersionError(Exception):
    """The published event changed after an edit draft was opened."""


class EventEditPermissionError(Exception):
    """The administrator level cannot edit this event at this time."""


@dataclass(frozen=True)
class PublicationReservation:
    event_id: int
    status: str
    should_send: bool


@dataclass(frozen=True)
class RepublicationReservation:
    event_id: int
    publication_id: int
    previous_publication_id: int | None
    should_send: bool


def _decode_draft(row: aiosqlite.Row) -> dict[str, Any]:
    draft = dict(row)
    try:
        draft["payload"] = json.loads(draft.pop("payload_json"))
    except (TypeError, ValueError):
        draft["payload"] = {}
    return draft


async def load_draft(
    admin_id: int,
    *,
    now: int,
) -> tuple[dict[str, Any] | None, bool]:
    """Return an active draft and whether an expired one was removed."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT d.*, e.status AS target_event_status,
                   e.starts_at_utc AS target_event_starts_at_utc
            FROM event_drafts d
            LEFT JOIN events e ON e.id=d.target_event_id
            WHERE d.admin_id=?
            """,
            (admin_id,),
        )
        row = await cursor.fetchone()
        if (
            row
            and int(row["expires_at"]) <= now
            and row["target_event_status"]
            not in {
                EventStatus.PUBLISHING.value,
                EventStatus.PUBLICATION_UNKNOWN.value,
            }
        ):
            target_event_id = row["target_event_id"]
            if target_event_id is not None and row["target_event_status"] == "draft":
                await db.execute(
                    "DELETE FROM event_publications WHERE event_id=?",
                    (target_event_id,),
                )
                await db.execute(
                    "DELETE FROM events WHERE id=? AND status='draft'",
                    (target_event_id,),
                )
            await db.execute("DELETE FROM event_drafts WHERE admin_id=?", (admin_id,))
            await db.commit()
            return None, True
        await db.commit()
        return (_decode_draft(row) if row else None), False


async def create_draft(
    admin_id: int,
    *,
    menu_chat_id: int,
    menu_message_id: int | None,
    now: int,
    expires_at: int,
) -> dict[str, Any]:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO event_drafts (
                admin_id, draft_kind, target_event_id, base_version,
                payload_json, menu_chat_id, menu_message_id,
                created_at, updated_at, expires_at
            ) VALUES (?, 'create', NULL, NULL, '{}', ?, ?, ?, ?, ?)
            ON CONFLICT(admin_id) DO UPDATE SET
                draft_kind='create',
                target_event_id=NULL,
                base_version=NULL,
                payload_json='{}',
                menu_chat_id=excluded.menu_chat_id,
                menu_message_id=excluded.menu_message_id,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                expires_at=excluded.expires_at
            """,
            (
                admin_id,
                menu_chat_id,
                menu_message_id,
                now,
                now,
                expires_at,
            ),
        )
        await db.commit()

    draft, _ = await load_draft(admin_id, now=now)
    assert draft is not None
    return draft


async def create_edit_draft(
    admin_id: int,
    event_id: int,
    *,
    base_version: int,
    payload: dict[str, Any],
    menu_chat_id: int,
    menu_message_id: int,
    now: int,
    expires_at: int,
) -> dict[str, Any]:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO event_drafts (
                admin_id, draft_kind, target_event_id, base_version,
                payload_json, menu_chat_id, menu_message_id,
                created_at, updated_at, expires_at
            ) VALUES (?, 'edit', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(admin_id) DO UPDATE SET
                draft_kind='edit',
                target_event_id=excluded.target_event_id,
                base_version=excluded.base_version,
                payload_json=excluded.payload_json,
                menu_chat_id=excluded.menu_chat_id,
                menu_message_id=excluded.menu_message_id,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                expires_at=excluded.expires_at
            """,
            (
                admin_id,
                event_id,
                base_version,
                payload_json,
                menu_chat_id,
                menu_message_id,
                now,
                now,
                expires_at,
            ),
        )
        await db.commit()
    draft, _ = await load_draft(admin_id, now=now)
    if draft is None:
        raise DraftNotFoundError
    return draft


async def save_draft(
    admin_id: int,
    payload: dict[str, Any],
    *,
    menu_chat_id: int,
    menu_message_id: int,
    now: int,
    expires_at: int,
) -> dict[str, Any]:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE event_drafts
            SET payload_json=?, menu_chat_id=?, menu_message_id=?,
                updated_at=?, expires_at=?
            WHERE admin_id=? AND expires_at>?
            """,
            (
                payload_json,
                menu_chat_id,
                menu_message_id,
                now,
                expires_at,
                admin_id,
                now,
            ),
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise DraftNotFoundError

    draft, _ = await load_draft(admin_id, now=now)
    if draft is None:
        raise DraftNotFoundError
    return draft


async def update_draft_menu(
    admin_id: int,
    *,
    menu_chat_id: int,
    menu_message_id: int,
    now: int,
    expires_at: int,
) -> None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute(
            """
            UPDATE event_drafts
            SET menu_chat_id=?, menu_message_id=?, updated_at=?, expires_at=?
            WHERE admin_id=? AND expires_at>?
            """,
            (menu_chat_id, menu_message_id, now, expires_at, admin_id, now),
        )
        await db.commit()


async def delete_draft(admin_id: int) -> bool:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT draft_kind, target_event_id FROM event_drafts WHERE admin_id=?",
            (admin_id,),
        )
        draft = await cursor.fetchone()
        if not draft:
            await db.commit()
            return False
        event_id = draft["target_event_id"]
        if event_id is not None and draft["draft_kind"] == "create":
            cursor = await db.execute(
                "SELECT status FROM events WHERE id=?",
                (event_id,),
            )
            event = await cursor.fetchone()
            if event and event["status"] != EventStatus.DRAFT.value:
                await db.rollback()
                raise PublicationStateError
            await db.execute(
                "DELETE FROM event_publications WHERE event_id=?",
                (event_id,),
            )
            await db.execute(
                "DELETE FROM events WHERE id=? AND status='draft'",
                (event_id,),
            )
        cursor = await db.execute("DELETE FROM event_drafts WHERE admin_id=?", (admin_id,))
        await db.commit()
        return cursor.rowcount > 0


async def cleanup_expired_drafts(*, now: int) -> int:
    """Delete expired draft rows and abandoned, definitely unsent reservations."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT d.draft_kind, d.target_event_id
            FROM event_drafts d
            LEFT JOIN events e ON e.id=d.target_event_id
            WHERE d.expires_at<=?
              AND (
                    d.draft_kind='edit'
                    OR d.target_event_id IS NULL
                    OR e.status='draft'
              )
            """,
            (now,),
        )
        abandoned_event_ids = [
            int(row["target_event_id"])
            for row in await cursor.fetchall()
            if row["draft_kind"] == "create" and row["target_event_id"] is not None
        ]
        deleted = await db.execute(
            """
            DELETE FROM event_drafts
            WHERE expires_at<=?
              AND (
                    draft_kind='edit'
                    OR target_event_id IS NULL
                    OR target_event_id IN (
                        SELECT id FROM events WHERE status='draft'
                    )
              )
            """,
            (now,),
        )
        for event_id in abandoned_event_ids:
            await db.execute(
                "DELETE FROM event_publications WHERE event_id=?",
                (event_id,),
            )
            await db.execute(
                "DELETE FROM events WHERE id=? AND status='draft'",
                (event_id,),
            )
        await db.commit()
        return deleted.rowcount


async def event_menu_counts(now: int) -> dict[str, int]:
    active = ACTIVE_START_RESERVATION_STATUSES
    placeholders = ",".join("?" for _ in active)
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        cursor = await db.execute(
            f"""
            SELECT
                SUM(CASE WHEN status IN ({placeholders}) AND starts_at_utc>=?
                         THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='awaiting_review' THEN 1 ELSE 0 END),
                SUM(CASE WHEN publication_missing=1 AND status IN ({placeholders})
                         THEN 1 ELSE 0 END)
            FROM events
            """,
            (*active, now, *active),
        )
        row = await cursor.fetchone()
    return {
        "upcoming": int(row[0] or 0),
        "review": int(row[1] or 0),
        "missing": int(row[2] or 0),
    }


async def find_start_conflict(
    starts_at_utc: int,
    *,
    exclude_event_id: int | None = None,
) -> dict[str, Any] | None:
    active = ACTIVE_START_RESERVATION_STATUSES
    placeholders = ",".join("?" for _ in active)
    params: list[Any] = [starts_at_utc, *active]
    exclusion = ""
    if exclude_event_id is not None:
        exclusion = " AND id!=?"
        params.append(exclude_event_id)
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, title, starts_at_utc, status
            FROM events
            WHERE starts_at_utc=? AND status IN ({placeholders}){exclusion}
            LIMIT 1
            """,
            params,
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def reserve_draft_publication(
    admin_id: int,
    *,
    title: str,
    event_type: str,
    description: str | None,
    starts_at_utc: int,
    safe_until_utc: int,
    registration_closes_at_utc: int,
    main_chat_id: int,
    now: int,
) -> PublicationReservation:
    """Atomically reserve a start minute before contacting Telegram."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT * FROM event_drafts WHERE admin_id=? AND expires_at>?",
            (admin_id, now),
        )
        draft = await cursor.fetchone()
        if not draft:
            await db.rollback()
            raise DraftNotFoundError

        target_event_id = draft["target_event_id"]
        if target_event_id is not None:
            cursor = await db.execute(
                "SELECT status FROM events WHERE id=?",
                (target_event_id,),
            )
            existing = await cursor.fetchone()
            if existing and existing["status"] != EventStatus.DRAFT.value:
                await db.commit()
                return PublicationReservation(
                    event_id=int(target_event_id),
                    status=str(existing["status"]),
                    should_send=False,
                )

        try:
            if target_event_id is None:
                cursor = await db.execute(
                    """
                    INSERT INTO events (
                        title, event_type, description,
                        starts_at_utc, safe_until_utc,
                        registration_closes_at_utc, status,
                        created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'publishing', ?, ?, ?)
                    """,
                    (
                        title,
                        event_type,
                        description,
                        starts_at_utc,
                        safe_until_utc,
                        registration_closes_at_utc,
                        admin_id,
                        now,
                        now,
                    ),
                )
                event_id = int(cursor.lastrowid)
                await db.execute(
                    """
                    INSERT INTO event_publications (event_id, chat_id, is_current)
                    VALUES (?, ?, 1)
                    """,
                    (event_id, main_chat_id),
                )
                await db.execute(
                    "UPDATE event_drafts SET target_event_id=? WHERE admin_id=?",
                    (event_id, admin_id),
                )
            else:
                event_id = int(target_event_id)
                await db.execute(
                    """
                    UPDATE events
                    SET title=?, event_type=?, description=?, starts_at_utc=?,
                        safe_until_utc=?, registration_closes_at_utc=?,
                        status='publishing', updated_at=?, version=version+1
                    WHERE id=? AND status='draft'
                    """,
                    (
                        title,
                        event_type,
                        description,
                        starts_at_utc,
                        safe_until_utc,
                        registration_closes_at_utc,
                        now,
                        event_id,
                    ),
                )
                await db.execute(
                    """
                    UPDATE event_publications
                    SET chat_id=?, message_id=NULL, published_at=NULL,
                        missing_at=NULL, invalidated_at=NULL, is_current=1
                    WHERE event_id=? AND is_current=1
                    """,
                    (main_chat_id, event_id),
                )
        except sqlite3.IntegrityError as exc:
            await db.rollback()
            conflict = await find_start_conflict(starts_at_utc)
            if conflict:
                raise EventConflictError(str(conflict["title"])) from exc
            raise

        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, new_value_json, created_at
            ) VALUES (?, ?, 'event_publish_reserved', ?, ?)
            """,
            (
                event_id,
                admin_id,
                json.dumps(
                    {
                        "title": title,
                        "event_type": event_type,
                        "starts_at_utc": starts_at_utc,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now,
            ),
        )
        await db.commit()
        return PublicationReservation(
            event_id=event_id,
            status=EventStatus.PUBLISHING.value,
            should_send=True,
        )


async def complete_publication(event_id: int, message_id: int, *, now: int) -> None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            UPDATE events
            SET status='published', publication_missing=0, updated_at=?
            WHERE id=? AND status='publishing'
            """,
            (now, event_id),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            raise PublicationStateError
        publication = await db.execute(
            """
            UPDATE event_publications
            SET message_id=?, published_at=?, missing_at=NULL
            WHERE event_id=? AND is_current=1 AND message_id IS NULL
            """,
            (message_id, now, event_id),
        )
        if publication.rowcount == 0:
            await db.rollback()
            raise PublicationStateError
        await db.execute(
            """
            INSERT INTO event_audit_log (event_id, action, new_value_json, created_at)
            VALUES (?, 'event_published', ?, ?)
            """,
            (event_id, json.dumps({"message_id": message_id}), now),
        )
        await db.execute(
            "DELETE FROM event_drafts WHERE target_event_id=?",
            (event_id,),
        )
        await db.commit()


async def release_failed_publication(
    event_id: int,
    admin_id: int,
    *,
    error: str,
    now: int,
) -> None:
    """Return a definitely unsent publication to its persisted draft."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            UPDATE events SET status='draft', updated_at=?
            WHERE id=? AND status='publishing'
            """,
            (now, event_id),
        )
        await db.execute(
            """
            UPDATE event_publications
            SET message_id=NULL, published_at=NULL
            WHERE event_id=? AND is_current=1
            """,
            (event_id,),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, reason, created_at
            ) VALUES (?, ?, 'event_publish_failed', ?, ?)
            """,
            (event_id, admin_id, error[:1000], now),
        )
        await db.commit()


async def mark_publication_unknown(
    event_id: int,
    *,
    actor_id: int | None,
    error: str,
    now: int,
) -> None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            UPDATE events SET status='publication_unknown', updated_at=?
            WHERE id=? AND status='publishing'
            """,
            (now, event_id),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, reason, created_at
            ) VALUES (?, ?, 'event_publication_unknown', ?, ?)
            """,
            (event_id, actor_id, error[:1000], now),
        )
        await db.commit()


async def reconcile_incomplete_publications(*, now: int) -> int:
    """Mark crash-interrupted sends as unknown without retrying them."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT id FROM events WHERE status='publishing'"
        )
        event_ids = [int(row["id"]) for row in await cursor.fetchall()]
        if event_ids:
            await db.execute(
                """
                UPDATE events SET status='publication_unknown', updated_at=?
                WHERE status='publishing'
                """,
                (now,),
            )
            await db.executemany(
                """
                INSERT INTO event_audit_log (
                    event_id, action, reason, created_at
                ) VALUES (?, 'event_publication_unknown',
                          'startup reconciliation', ?)
                """,
                [(event_id, now) for event_id in event_ids],
            )
        await db.commit()
        return len(event_ids)


async def get_event_card(event_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM events WHERE id=?", (event_id,))
        event = await cursor.fetchone()
        if not event:
            return None
        result = dict(event)

        cursor = await db.execute(
            """
            SELECT
                er.user_id,
                COALESCE(NULLIF(trim(p.game_nickname), ''), er.nickname_snapshot)
                    AS nickname
            FROM event_responses er
            LEFT JOIN profiles p ON p.user_id=er.user_id
            WHERE er.event_id=? AND er.status='going'
            ORDER BY er.joined_at ASC, er.user_id ASC
            """,
            (event_id,),
        )
        result["participants"] = [dict(row) for row in await cursor.fetchall()]

        cursor = await db.execute(
            """
            SELECT status, COUNT(*) AS amount
            FROM event_responses
            WHERE event_id=?
            GROUP BY status
            """,
            (event_id,),
        )
        counts = {str(row["status"]): int(row["amount"]) for row in await cursor.fetchall()}
        result["thinking_count"] = counts.get("thinking", 0)
        result["declined_count"] = counts.get("declined", 0) + counts.get(
            "late_declined", 0
        )

        cursor = await db.execute(
            """
            SELECT chat_id, message_id, published_at
            FROM event_publications
            WHERE event_id=? AND is_current=1
            """,
            (event_id,),
        )
        publication = await cursor.fetchone()
        result["publication"] = dict(publication) if publication else None
        return result


async def list_refreshable_event_ids() -> list[int]:
    statuses = (
        EventStatus.PUBLISHED.value,
        EventStatus.REGISTRATION_CLOSED.value,
        EventStatus.STARTED.value,
        EventStatus.AWAITING_REVIEW.value,
    )
    placeholders = ",".join("?" for _ in statuses)
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        cursor = await db.execute(
            f"""
            SELECT e.id
            FROM events e
            JOIN event_publications ep
              ON ep.event_id=e.id AND ep.is_current=1
            WHERE e.status IN ({placeholders})
              AND e.publication_missing=0
              AND ep.message_id IS NOT NULL
            ORDER BY e.starts_at_utc ASC
            """,
            statuses,
        )
        return [int(row[0]) for row in await cursor.fetchall()]


async def mark_publication_missing(event_id: int, *, now: int) -> bool:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            UPDATE events
            SET publication_missing=1, updated_at=?
            WHERE id=? AND publication_missing=0
              AND status IN ('published', 'registration_closed', 'started', 'awaiting_review')
            """,
            (now, event_id),
        )
        if cursor.rowcount:
            await db.execute(
                """
                UPDATE event_publications SET missing_at=?
                WHERE event_id=? AND is_current=1
                """,
                (now, event_id),
            )
            await db.execute(
                """
                INSERT INTO event_audit_log (event_id, action, created_at)
                VALUES (?, 'publication_missing', ?)
                """,
                (event_id, now),
            )
        await db.commit()
        return cursor.rowcount > 0


async def list_missing_events(limit: int = 10) -> list[dict[str, Any]]:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, title, starts_at_utc, status
            FROM events
            WHERE publication_missing=1
              AND status IN ('published', 'registration_closed', 'started', 'awaiting_review')
            ORDER BY starts_at_utc ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def list_editable_events(
    *,
    now: int,
    include_started: bool,
    limit: int = 10,
) -> list[dict[str, Any]]:
    time_filter = "" if include_started else " AND starts_at_utc>?"
    params: tuple[int, ...] = (limit,) if include_started else (now, limit)
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, title, starts_at_utc, status, version
            FROM events
            WHERE status IN (
                'published', 'registration_closed', 'started', 'awaiting_review'
            ){time_filter}
            ORDER BY starts_at_utc ASC, id ASC
            LIMIT ?
            """,
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]


async def apply_edit_draft(
    admin_id: int,
    *,
    admin_level: int,
    title: str,
    event_type: str,
    description: str | None,
    starts_at_utc: int,
    safe_until_utc: int,
    registration_closes_at_utc: int,
    now: int,
) -> dict[str, Any]:
    """Commit every field in an edit draft and reset responses on reschedule."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT d.target_event_id, d.base_version, e.*
            FROM event_drafts d
            JOIN events e ON e.id=d.target_event_id
            WHERE d.admin_id=? AND d.draft_kind='edit' AND d.expires_at>?
            """,
            (admin_id, now),
        )
        event = await cursor.fetchone()
        if not event:
            await db.rollback()
            raise DraftNotFoundError
        event_id = int(event["target_event_id"])
        if int(event["version"]) != int(event["base_version"]):
            await db.rollback()
            raise EventVersionError
        status = str(event["status"])
        if status not in {
            "published",
            "registration_closed",
            "started",
            "awaiting_review",
        }:
            await db.rollback()
            raise EventEditPermissionError
        if admin_level < 4 and (
            now >= int(event["starts_at_utc"])
            or status not in {"published", "registration_closed"}
        ):
            await db.rollback()
            raise EventEditPermissionError

        rescheduled = starts_at_utc != int(event["starts_at_utc"])
        respondents: list[dict[str, Any]] = []
        old_reminder_message_id: int | None = None
        if rescheduled:
            cursor = await db.execute(
                """
                SELECT er.user_id,
                       COALESCE(NULLIF(trim(p.game_nickname), ''),
                                er.telegram_name_snapshot,
                                NULLIF(trim(p.telegram_full_name), ''),
                                CAST(er.user_id AS TEXT)) AS display_name
                FROM event_responses er
                LEFT JOIN profiles p ON p.user_id=er.user_id
                WHERE er.event_id=?
                ORDER BY er.responded_at ASC, er.user_id ASC
                """,
                (event_id,),
            )
            respondents = [dict(row) for row in await cursor.fetchall()]
            cursor = await db.execute(
                """
                SELECT telegram_message_id
                FROM event_notifications
                WHERE event_id=? AND kind='auto_reminder' AND status='sent'
                  AND telegram_message_id IS NOT NULL
                ORDER BY sent_at DESC, id DESC
                LIMIT 1
                """,
                (event_id,),
            )
            reminder = await cursor.fetchone()
            if reminder:
                old_reminder_message_id = int(reminder["telegram_message_id"])

        old_value = {
            "title": event["title"],
            "event_type": event["event_type"],
            "description": event["description"],
            "starts_at_utc": event["starts_at_utc"],
        }
        new_value = {
            "title": title,
            "event_type": event_type,
            "description": description,
            "starts_at_utc": starts_at_utc,
        }
        next_status = "published" if rescheduled else status
        try:
            updated = await db.execute(
                """
                UPDATE events
                SET title=?, event_type=?, description=?, starts_at_utc=?,
                    safe_until_utc=?, registration_closes_at_utc=?, status=?,
                    updated_at=?, version=version+1
                WHERE id=? AND version=?
                """,
                (
                    title,
                    event_type,
                    description,
                    starts_at_utc,
                    safe_until_utc,
                    registration_closes_at_utc,
                    next_status,
                    now,
                    event_id,
                    event["base_version"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            await db.rollback()
            conflict = await find_start_conflict(
                starts_at_utc,
                exclude_event_id=event_id,
            )
            if conflict:
                raise EventConflictError(str(conflict["title"])) from exc
            raise
        if updated.rowcount == 0:
            await db.rollback()
            raise EventVersionError

        if rescheduled:
            await db.execute(
                """
                UPDATE event_notifications
                SET dedupe_key=dedupe_key || ':stale:' || id || ':' || ?,
                    status=CASE WHEN status='sent' THEN status ELSE 'skipped' END,
                    skipped_at=CASE WHEN status='sent' THEN skipped_at ELSE ? END,
                    error=CASE
                        WHEN status='sent' THEN error
                        ELSE 'event rescheduled'
                    END
                WHERE event_id=? AND kind='auto_reminder'
                """,
                (now, now, event_id),
            )
            await db.execute("DELETE FROM event_results WHERE event_id=?", (event_id,))
            await db.execute(
                "DELETE FROM event_late_confirmations WHERE event_id=?",
                (event_id,),
            )
            await db.execute("DELETE FROM event_responses WHERE event_id=?", (event_id,))
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, old_value_json,
                new_value_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                admin_id,
                "event_rescheduled" if rescheduled else "event_edited",
                json.dumps(old_value, ensure_ascii=False, separators=(",", ":")),
                json.dumps(new_value, ensure_ascii=False, separators=(",", ":")),
                now,
            ),
        )
        await db.execute("DELETE FROM event_drafts WHERE admin_id=?", (admin_id,))
        await db.commit()
        return {
            "event_id": event_id,
            "title": title,
            "rescheduled": rescheduled,
            "respondents": respondents,
            "starts_at_utc": starts_at_utc,
            "registration_closes_at_utc": registration_closes_at_utc,
            "old_reminder_message_id": old_reminder_message_id,
            "version": int(event["base_version"]) + 1,
        }


async def reserve_republication(
    event_id: int,
    actor_id: int,
    *,
    now: int,
) -> RepublicationReservation:
    """Reserve one replacement message and reject concurrent repeat sends."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT status, publication_missing
            FROM events WHERE id=?
            """,
            (event_id,),
        )
        event = await cursor.fetchone()
        if (
            not event
            or not event["publication_missing"]
            or event["status"]
            not in {"published", "registration_closed", "started", "awaiting_review"}
        ):
            await db.rollback()
            raise PublicationStateError

        cursor = await db.execute(
            """
            SELECT id, chat_id, message_id
            FROM event_publications
            WHERE event_id=? AND is_current=1
            """,
            (event_id,),
        )
        current = await cursor.fetchone()
        if not current:
            await db.rollback()
            raise PublicationStateError
        if current["message_id"] is None:
            await db.commit()
            return RepublicationReservation(
                event_id=event_id,
                publication_id=int(current["id"]),
                previous_publication_id=None,
                should_send=False,
            )

        previous_id = int(current["id"])
        await db.execute(
            """
            UPDATE event_publications
            SET is_current=0, invalidated_at=?
            WHERE id=? AND is_current=1
            """,
            (now, previous_id),
        )
        cursor = await db.execute(
            """
            INSERT INTO event_publications (event_id, chat_id, is_current)
            VALUES (?, ?, 1)
            """,
            (event_id, int(current["chat_id"])),
        )
        publication_id = int(cursor.lastrowid)
        await db.execute(
            """
            INSERT INTO event_audit_log (event_id, actor_id, action, created_at)
            VALUES (?, ?, 'event_republish_reserved', ?)
            """,
            (event_id, actor_id, now),
        )
        await db.commit()
        return RepublicationReservation(
            event_id=event_id,
            publication_id=publication_id,
            previous_publication_id=previous_id,
            should_send=True,
        )


async def complete_republication(
    reservation: RepublicationReservation,
    message_id: int,
    *,
    actor_id: int,
    now: int,
) -> None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            UPDATE event_publications
            SET message_id=?, published_at=?, missing_at=NULL
            WHERE id=? AND event_id=? AND is_current=1 AND message_id IS NULL
            """,
            (
                message_id,
                now,
                reservation.publication_id,
                reservation.event_id,
            ),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            raise PublicationStateError
        await db.execute(
            """
            UPDATE events SET publication_missing=0, updated_at=? WHERE id=?
            """,
            (now, reservation.event_id),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, new_value_json, created_at
            ) VALUES (?, ?, 'event_republished', ?, ?)
            """,
            (
                reservation.event_id,
                actor_id,
                json.dumps({"message_id": message_id}),
                now,
            ),
        )
        await db.commit()


async def release_failed_republication(
    reservation: RepublicationReservation,
    *,
    actor_id: int,
    error: str,
    now: int,
) -> None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        deleted = await db.execute(
            """
            DELETE FROM event_publications
            WHERE id=? AND event_id=? AND is_current=1 AND message_id IS NULL
            """,
            (reservation.publication_id, reservation.event_id),
        )
        if deleted.rowcount == 0:
            await db.rollback()
            raise PublicationStateError
        if reservation.previous_publication_id is not None:
            await db.execute(
                """
                UPDATE event_publications
                SET is_current=1, invalidated_at=NULL
                WHERE id=? AND event_id=?
                """,
                (reservation.previous_publication_id, reservation.event_id),
            )
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, reason, created_at
            ) VALUES (?, ?, 'event_republish_failed', ?, ?)
            """,
            (reservation.event_id, actor_id, error[:1000], now),
        )
        await db.commit()


async def mark_republication_unknown(
    reservation: RepublicationReservation,
    *,
    actor_id: int,
    error: str,
    now: int,
) -> None:
    """Record an uncertain replacement delivery without scheduling a retry."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, reason, new_value_json, created_at
            ) VALUES (?, ?, 'event_republication_unknown', ?, ?, ?)
            """,
            (
                reservation.event_id,
                actor_id,
                error[:1000],
                json.dumps({"publication_id": reservation.publication_id}),
                now,
            ),
        )
        await db.commit()
