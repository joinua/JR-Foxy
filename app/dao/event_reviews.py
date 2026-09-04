"""Transactional attendance review, correction and cancellation operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite

from app.core import db as core_db


@dataclass(frozen=True)
class ReviewTransition:
    code: str
    event_id: int
    title: str = ""
    total: int = 0
    finalized_at: int | None = None


def _valid_reason(reason: str) -> str:
    value = reason.strip()
    if not 3 <= len(value) <= 300:
        raise ValueError("reason")
    return value


async def list_review_schedules() -> list[dict[str, Any]]:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT e.id, e.starts_at_utc, e.status, e.version, e.review_created_at,
                   (SELECT MAX(n.scheduled_at) FROM event_notifications n
                    WHERE n.event_id=e.id AND n.kind='review_reminder')
                       AS last_review_reminder_at
            FROM events e
            WHERE status IN (
                'published', 'registration_closed', 'started', 'awaiting_review'
            )
            ORDER BY e.starts_at_utc ASC
            """
        )
        return [dict(row) for row in await cursor.fetchall()]


async def create_review(
    event_id: int,
    *,
    expected_at: int,
    now: int,
) -> ReviewTransition:
    """Create an overdue review once and prefill confirmed late declines."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT title, starts_at_utc, status, review_created_at FROM events WHERE id=?",
            (event_id,),
        )
        event = await cursor.fetchone()
        if not event:
            await db.rollback()
            return ReviewTransition("missing", event_id)
        title = str(event["title"])
        if int(event["starts_at_utc"]) + 3600 != expected_at:
            await db.rollback()
            return ReviewTransition("stale", event_id, title)
        if now < expected_at:
            await db.rollback()
            return ReviewTransition("early", event_id, title)
        if event["status"] == "awaiting_review":
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM event_responses
                WHERE event_id=? AND status IN ('going', 'late_declined')
                """,
                (event_id,),
            )
            total = int((await cursor.fetchone())[0])
            await db.rollback()
            return ReviewTransition(
                "already", event_id, title, total, event["review_created_at"]
            )
        if event["status"] != "started":
            await db.rollback()
            return ReviewTransition(str(event["status"]), event_id, title)

        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM event_responses
            WHERE event_id=? AND status IN ('going', 'late_declined')
            """,
            (event_id,),
        )
        total = int((await cursor.fetchone())[0])
        if total == 0:
            await db.execute(
                """
                UPDATE events
                SET status='cancelled',
                    cancel_reason='Подію автоматично скасовано: немає зареєстрованих учасників.',
                    cancelled_at=?, updated_at=?
                WHERE id=? AND status='started'
                """,
                (now, now, event_id),
            )
            await db.execute(
                """
                INSERT INTO event_audit_log (event_id, action, reason, created_at)
                VALUES (?, 'event_auto_cancelled',
                        'Подію автоматично скасовано: немає зареєстрованих учасників.', ?)
                """,
                (event_id, now),
            )
            await db.commit()
            return ReviewTransition("cancelled", event_id, title)

        await db.execute(
            """
            INSERT INTO event_results (
                event_id, user_id, result, nickname_snapshot, source,
                marked_at
            )
            SELECT er.event_id, er.user_id, 'late_decline',
                   COALESCE(NULLIF(trim(p.game_nickname), ''), er.nickname_snapshot),
                   'automatic', ?
            FROM event_responses er
            LEFT JOIN profiles p ON p.user_id=er.user_id
            WHERE er.event_id=? AND er.status='late_declined'
            ON CONFLICT(event_id, user_id) DO NOTHING
            """,
            (now, event_id),
        )
        await db.execute(
            """
            UPDATE events
            SET status='awaiting_review', review_created_at=?, updated_at=?,
                version=version+1
            WHERE id=? AND status='started'
            """,
            (now, now, event_id),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, action, new_value_json, created_at
            ) VALUES (?, 'review_created', ?, ?)
            """,
            (event_id, json.dumps({"participants": total}), now),
        )
        await db.commit()
        return ReviewTransition("created", event_id, title, total, now)


async def get_review(
    event_id: int,
    *,
    page: int = 0,
    page_size: int = 10,
) -> dict[str, Any] | None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM events WHERE id=?", (event_id,))
        event = await cursor.fetchone()
        if not event:
            return None
        data = dict(event)
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM event_responses
            WHERE event_id=? AND status IN ('going', 'late_declined')
            """,
            (event_id,),
        )
        total = int((await cursor.fetchone())[0])
        max_page = max(0, (total - 1) // page_size)
        page = min(max(page, 0), max_page)
        cursor = await db.execute(
            """
            SELECT er.user_id, er.status AS response_status,
                   COALESCE(
                            CASE WHEN ?='completed'
                                 THEN NULLIF(trim(r.nickname_snapshot), '') END,
                            NULLIF(trim(p.game_nickname), ''),
                            er.nickname_snapshot,
                            NULLIF(trim(p.telegram_full_name), ''),
                            er.telegram_name_snapshot,
                            CAST(er.user_id AS TEXT)) AS nickname,
                   r.result, r.exclusion_reason, r.source,
                   r.marked_by, r.corrected_by, r.correction_reason
            FROM event_responses er
            LEFT JOIN profiles p ON p.user_id=er.user_id
            LEFT JOIN event_results r
              ON r.event_id=er.event_id AND r.user_id=er.user_id
            WHERE er.event_id=? AND er.status IN ('going', 'late_declined')
            ORDER BY CASE er.status WHEN 'going' THEN 0 ELSE 1 END,
                     er.joined_at ASC, er.responded_at ASC, er.user_id ASC
            LIMIT ? OFFSET ?
            """,
            (str(event["status"]), event_id, page_size, page * page_size),
        )
        data["players"] = [dict(row) for row in await cursor.fetchall()]
        cursor = await db.execute(
            """
            SELECT result, COUNT(*) AS amount
            FROM event_results WHERE event_id=? GROUP BY result
            """,
            (event_id,),
        )
        counts = {str(row["result"]): int(row["amount"]) for row in await cursor.fetchall()}
        data.update(
            total=total,
            done=sum(counts.values()),
            counts=counts,
            page=page,
            pages=max_page + 1,
        )
        return data


async def list_reviews(
    *,
    include_completed: bool = True,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    statuses = "('awaiting_review', 'completed')" if include_completed else "('awaiting_review')"
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, title, starts_at_utc, status, finalized_at
            FROM events WHERE status IN {statuses}
            ORDER BY CASE status WHEN 'awaiting_review' THEN 0 ELSE 1 END,
                     starts_at_utc DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def count_reviews(*, include_completed: bool = True) -> int:
    statuses = "('awaiting_review', 'completed')" if include_completed else "('awaiting_review')"
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM events WHERE status IN {statuses}"
        )
        return int((await cursor.fetchone())[0])


async def set_result(
    event_id: int,
    user_id: int,
    result: str,
    *,
    actor_id: int,
    now: int,
    reason: str | None = None,
) -> str:
    if result not in {"present", "no_show", "late_decline", "excluded"}:
        raise ValueError("result")
    exclusion_reason = _valid_reason(reason or "") if result == "excluded" else None
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT status FROM events WHERE id=?", (event_id,))
        event = await cursor.fetchone()
        if not event or event["status"] != "awaiting_review":
            await db.rollback()
            return "unavailable"
        cursor = await db.execute(
            """
            SELECT er.user_id,
                   COALESCE(NULLIF(trim(p.game_nickname), ''), er.nickname_snapshot)
            FROM event_responses er
            LEFT JOIN profiles p ON p.user_id=er.user_id
            WHERE er.event_id=? AND er.user_id=?
              AND er.status IN ('going', 'late_declined')
            """,
            (event_id, user_id),
        )
        candidate = await cursor.fetchone()
        if not candidate:
            await db.rollback()
            return "missing"
        cursor = await db.execute(
            "SELECT result, exclusion_reason FROM event_results WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        )
        old = await cursor.fetchone()
        old_json = (
            json.dumps(dict(old), ensure_ascii=False, separators=(",", ":"))
            if old else None
        )
        await db.execute(
            """
            INSERT INTO event_results (
                event_id, user_id, result, exclusion_reason,
                nickname_snapshot, source, marked_by, marked_at
            ) VALUES (?, ?, ?, ?, ?, 'admin', ?, ?)
            ON CONFLICT(event_id, user_id) DO UPDATE SET
                result=excluded.result,
                exclusion_reason=excluded.exclusion_reason,
                source='admin', marked_by=excluded.marked_by,
                marked_at=excluded.marked_at
            """,
            (
                event_id, user_id, result, exclusion_reason,
                candidate[1], actor_id, now,
            ),
        )
        await db.execute(
            "UPDATE events SET updated_at=?, version=version+1 WHERE id=?",
            (now, event_id),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, old_value_json,
                new_value_json, reason, created_at
            ) VALUES (?, ?, 'review_result_set', ?, ?, ?, ?)
            """,
            (
                event_id, actor_id, old_json,
                json.dumps({"user_id": user_id, "result": result}, separators=(",", ":")),
                exclusion_reason, now,
            ),
        )
        await db.commit()
        return "updated"


async def request_or_finalize(
    event_id: int,
    *,
    actor_id: int,
    now: int,
) -> ReviewTransition:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT title, status, version, finalized_at FROM events WHERE id=?",
            (event_id,),
        )
        event = await cursor.fetchone()
        if not event:
            await db.rollback()
            return ReviewTransition("missing", event_id)
        if event["status"] == "completed":
            await db.rollback()
            return ReviewTransition(
                "already", event_id, str(event["title"]), finalized_at=event["finalized_at"]
            )
        if event["status"] != "awaiting_review":
            await db.rollback()
            return ReviewTransition("unavailable", event_id, str(event["title"]))
        cursor = await db.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM event_responses
               WHERE event_id=? AND status IN ('going', 'late_declined')),
              (SELECT COUNT(*) FROM event_results WHERE event_id=?)
            """,
            (event_id, event_id),
        )
        total, done = map(int, await cursor.fetchone())
        if done < total:
            await db.rollback()
            return ReviewTransition("incomplete", event_id, str(event["title"]), total - done)
        cursor = await db.execute(
            """
            SELECT actor_id, new_value_json FROM event_audit_log
            WHERE event_id=? AND action='review_finalize_requested'
            ORDER BY id DESC LIMIT 1
            """,
            (event_id,),
        )
        request = await cursor.fetchone()
        requested_version = None
        if request:
            try:
                requested_version = int(json.loads(request["new_value_json"])["version"])
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                requested_version = None
        if not request or int(request["actor_id"] or 0) != actor_id or requested_version != int(event["version"]):
            await db.execute(
                """
                INSERT INTO event_audit_log (
                    event_id, actor_id, action, new_value_json, created_at
                ) VALUES (?, ?, 'review_finalize_requested', ?, ?)
                """,
                (event_id, actor_id, json.dumps({"version": int(event["version"])}), now),
            )
            await db.commit()
            return ReviewTransition("confirm", event_id, str(event["title"]), total)

        await db.execute(
            """
            UPDATE events SET status='completed', finalized_at=?, updated_at=?,
                              version=version+1
            WHERE id=? AND status='awaiting_review'
            """,
            (now, now, event_id),
        )
        await db.execute(
            """
            UPDATE event_results
            SET nickname_snapshot=COALESCE(
                (SELECT NULLIF(trim(p.game_nickname), '')
                 FROM profiles p WHERE p.user_id=event_results.user_id),
                (SELECT er.nickname_snapshot FROM event_responses er
                 WHERE er.event_id=event_results.event_id
                   AND er.user_id=event_results.user_id),
                nickname_snapshot
            )
            WHERE event_id=?
            """,
            (event_id,),
        )
        await db.execute(
            "UPDATE event_results SET finalized_at=? WHERE event_id=?",
            (now, event_id),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (event_id, actor_id, action, created_at)
            VALUES (?, ?, 'review_finalized', ?)
            """,
            (event_id, actor_id, now),
        )
        await db.commit()
        return ReviewTransition("finalized", event_id, str(event["title"]), total, now)


async def correct_result(
    event_id: int,
    user_id: int,
    result: str,
    *,
    actor_id: int,
    admin_level: int,
    reason: str,
    now: int,
) -> str:
    correction_reason = _valid_reason(reason)
    if result not in {"present", "no_show", "late_decline", "excluded"}:
        raise ValueError("result")
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT status, finalized_at FROM events WHERE id=?", (event_id,)
        )
        event = await cursor.fetchone()
        if not event or event["status"] != "completed":
            await db.rollback()
            return "unavailable"
        if admin_level < 4 and (
            admin_level < 3
            or event["finalized_at"] is None
            or now > int(event["finalized_at"]) + 24 * 3600
        ):
            await db.rollback()
            return "expired"
        cursor = await db.execute(
            "SELECT result, exclusion_reason FROM event_results WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        )
        old = await cursor.fetchone()
        if not old:
            await db.rollback()
            return "missing"
        await db.execute(
            """
            UPDATE event_results
            SET result=?, exclusion_reason=?, corrected_by=?, corrected_at=?,
                correction_reason=?
            WHERE event_id=? AND user_id=?
            """,
            (
                result,
                correction_reason if result == "excluded" else None,
                actor_id, now, correction_reason, event_id, user_id,
            ),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (
                event_id, actor_id, action, old_value_json,
                new_value_json, reason, created_at
            ) VALUES (?, ?, 'review_result_corrected', ?, ?, ?, ?)
            """,
            (
                event_id, actor_id,
                json.dumps({"user_id": user_id, **dict(old)}, ensure_ascii=False),
                json.dumps({"user_id": user_id, "result": result}, ensure_ascii=False),
                correction_reason, now,
            ),
        )
        await db.commit()
        return "updated"


async def request_or_cancel(
    event_id: int,
    *,
    actor_id: int,
    reason: str,
    now: int,
) -> ReviewTransition:
    cancel_reason = _valid_reason(reason)
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT title, status, version FROM events WHERE id=?", (event_id,)
        )
        event = await cursor.fetchone()
        if not event:
            await db.rollback()
            return ReviewTransition("missing", event_id)
        if event["status"] == "cancelled":
            await db.rollback()
            return ReviewTransition("already", event_id, str(event["title"]))
        if event["status"] not in {
            "publishing", "publication_unknown", "published",
            "registration_closed", "started", "awaiting_review",
        }:
            await db.rollback()
            return ReviewTransition("unavailable", event_id, str(event["title"]))
        cursor = await db.execute(
            """
            SELECT actor_id, reason, new_value_json FROM event_audit_log
            WHERE event_id=? AND action='event_cancel_requested'
            ORDER BY id DESC LIMIT 1
            """,
            (event_id,),
        )
        request = await cursor.fetchone()
        version = int(event["version"])
        matches = False
        if request and int(request["actor_id"] or 0) == actor_id and request["reason"] == cancel_reason:
            try:
                matches = int(json.loads(request["new_value_json"])["version"]) == version
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                pass
        if not matches:
            await db.execute(
                """
                INSERT INTO event_audit_log (
                    event_id, actor_id, action, new_value_json, reason, created_at
                ) VALUES (?, ?, 'event_cancel_requested', ?, ?, ?)
                """,
                (event_id, actor_id, json.dumps({"version": version}), cancel_reason, now),
            )
            await db.commit()
            return ReviewTransition("confirm", event_id, str(event["title"]))
        await db.execute(
            """
            UPDATE events
            SET status='cancelled', cancel_reason=?, cancelled_by=?, cancelled_at=?,
                updated_at=?, version=version+1
            WHERE id=?
            """,
            (cancel_reason, actor_id, now, now, event_id),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (event_id, actor_id, action, reason, created_at)
            VALUES (?, ?, 'event_cancelled', ?, ?)
            """,
            (event_id, actor_id, cancel_reason, now),
        )
        await db.commit()
        return ReviewTransition("cancelled", event_id, str(event["title"]))


async def annul_event(
    event_id: int,
    *,
    actor_id: int,
    reason: str,
    now: int,
) -> ReviewTransition:
    annul_reason = _valid_reason(reason)
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT title, status FROM events WHERE id=?", (event_id,))
        event = await cursor.fetchone()
        if not event:
            await db.rollback()
            return ReviewTransition("missing", event_id)
        if event["status"] == "annulled":
            await db.rollback()
            return ReviewTransition("already", event_id, str(event["title"]))
        if event["status"] != "completed":
            await db.rollback()
            return ReviewTransition("unavailable", event_id, str(event["title"]))
        await db.execute(
            """
            UPDATE events
            SET status='annulled', annul_reason=?, annulled_by=?, annulled_at=?,
                updated_at=?, version=version+1
            WHERE id=? AND status='completed'
            """,
            (annul_reason, actor_id, now, now, event_id),
        )
        await db.execute(
            """
            INSERT INTO event_audit_log (event_id, actor_id, action, reason, created_at)
            VALUES (?, ?, 'event_annulled', ?, ?)
            """,
            (event_id, actor_id, annul_reason, now),
        )
        await db.commit()
        return ReviewTransition("annulled", event_id, str(event["title"]))


async def list_cancellable_events(limit: int = 20) -> list[dict[str, Any]]:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, title, starts_at_utc, status FROM events
            WHERE status IN (
                'publishing', 'publication_unknown', 'published',
                'registration_closed', 'started', 'awaiting_review'
            )
            ORDER BY starts_at_utc ASC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def reserve_admin_delivery(
    event_id: int,
    *,
    kind: str,
    scheduled_at: int,
    now: int,
) -> bool:
    """Reserve a one-shot Telegram delivery; a reserved send is never retried."""

    dedupe_key = f"event:{event_id}:{kind}:{scheduled_at}"
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO event_notifications (
                event_id, kind, audience, dedupe_key, scheduled_at,
                reserved_at, status
            ) VALUES (?, ?, 'administration', ?, ?, ?, 'reserved')
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (event_id, kind, dedupe_key, scheduled_at, now),
        )
        await db.commit()
        return cursor.rowcount > 0


async def finish_admin_delivery(
    event_id: int,
    *,
    kind: str,
    scheduled_at: int,
    now: int,
    message_id: int | None = None,
    error: str | None = None,
) -> None:
    status = "sent" if message_id is not None else "failed"
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute(
            """
            UPDATE event_notifications
            SET status=?, sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END,
                telegram_message_id=?, error=?
            WHERE event_id=? AND kind=? AND scheduled_at=? AND status='reserved'
            """,
            (
                status, status, now, message_id, (error or "")[:1000] or None,
                event_id, kind, scheduled_at,
            ),
        )
        await db.commit()
