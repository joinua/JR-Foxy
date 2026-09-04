"""Transactional persistence for public event responses."""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from app.core import db as core_db
from app.core.event_types import MAX_EVENT_PARTICIPANTS


@dataclass(frozen=True)
class ResponseDecision:
    code: str
    changed: bool = False


async def _audit(
    db: aiosqlite.Connection,
    *,
    event_id: int,
    user_id: int,
    action: str,
    old_status: str | None,
    new_status: str | None,
    now: int,
) -> None:
    await db.execute(
        """
        INSERT INTO event_audit_log (
            event_id, actor_id, action, old_value_json, new_value_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            user_id,
            action,
            json.dumps({"status": old_status}, separators=(",", ":")),
            json.dumps({"status": new_status}, separators=(",", ":")),
            now,
        ),
    )


async def _set_response(
    db: aiosqlite.Connection,
    *,
    event_id: int,
    user_id: int,
    status: str,
    nickname: str | None,
    telegram_name: str,
    joined_at: int | None,
    now: int,
) -> None:
    await db.execute(
        """
        INSERT INTO event_responses (
            event_id, user_id, status, nickname_snapshot,
            telegram_name_snapshot, joined_at, responded_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id, user_id) DO UPDATE SET
            status=excluded.status,
            nickname_snapshot=COALESCE(
                excluded.nickname_snapshot,
                event_responses.nickname_snapshot
            ),
            telegram_name_snapshot=excluded.telegram_name_snapshot,
            joined_at=excluded.joined_at,
            responded_at=excluded.responded_at,
            updated_at=excluded.updated_at
        """,
        (
            event_id,
            user_id,
            status,
            nickname,
            telegram_name,
            joined_at,
            now,
            now,
        ),
    )


async def apply_response(
    event_id: int,
    user_id: int,
    action: str,
    *,
    telegram_name: str,
    now: int,
) -> ResponseDecision:
    """Apply one response with time and capacity checks in one transaction."""

    if action not in {"going", "thinking", "declined"}:
        return ResponseDecision("invalid")

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT id, status, starts_at_utc, safe_until_utc,
                   registration_closes_at_utc
            FROM events WHERE id=?
            """,
            (event_id,),
        )
        event = await cursor.fetchone()
        if not event:
            await db.rollback()
            return ResponseDecision("stale")
        if event["status"] in {"cancelled", "annulled"}:
            await db.rollback()
            return ResponseDecision("cancelled")
        if now >= int(event["starts_at_utc"]):
            await db.rollback()
            return ResponseDecision("started")
        if event["status"] not in {"published", "registration_closed"}:
            await db.rollback()
            return ResponseDecision("stale")

        cursor = await db.execute(
            """
            SELECT status FROM event_responses
            WHERE event_id=? AND user_id=?
            """,
            (event_id, user_id),
        )
        response = await cursor.fetchone()
        old_status = str(response["status"]) if response else None
        safe_until = int(event["safe_until_utc"])
        closes_at = int(event["registration_closes_at_utc"])

        if action == "going":
            if now >= closes_at:
                await db.rollback()
                return ResponseDecision("registration_closed")
            if old_status == "going":
                await db.rollback()
                return ResponseDecision("already")
            cursor = await db.execute(
                """
                SELECT game_nickname FROM profiles
                WHERE user_id=? AND length(trim(COALESCE(game_nickname, '')))>0
                """,
                (user_id,),
            )
            profile = await cursor.fetchone()
            if not profile:
                await db.rollback()
                return ResponseDecision("missing_nickname")
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM event_responses
                WHERE event_id=? AND status='going'
                """,
                (event_id,),
            )
            if int((await cursor.fetchone())[0]) >= MAX_EVENT_PARTICIPANTS:
                await db.rollback()
                return ResponseDecision("limit")
            nickname = str(profile["game_nickname"]).strip()
            await _set_response(
                db,
                event_id=event_id,
                user_id=user_id,
                status="going",
                nickname=nickname,
                telegram_name=telegram_name,
                joined_at=now,
                now=now,
            )
            await db.execute(
                "DELETE FROM event_late_confirmations WHERE event_id=? AND user_id=?",
                (event_id, user_id),
            )
            audit_action = (
                "late_decline_rejoined"
                if old_status == "late_declined"
                else "response_changed"
            )
            await _audit(
                db,
                event_id=event_id,
                user_id=user_id,
                action=audit_action,
                old_status=old_status,
                new_status="going",
                now=now,
            )
            await db.commit()
            return ResponseDecision("going", True)

        if action == "thinking":
            if now >= closes_at:
                await db.rollback()
                return ResponseDecision("registration_closed")
            if old_status == "thinking":
                await db.rollback()
                return ResponseDecision("already")
            if now >= safe_until and old_status in {"going", "late_declined"}:
                await db.rollback()
                return ResponseDecision("thinking_late_blocked")
            await _set_response(
                db,
                event_id=event_id,
                user_id=user_id,
                status="thinking",
                nickname=None,
                telegram_name=telegram_name,
                joined_at=None,
                now=now,
            )
            await db.execute(
                "DELETE FROM event_late_confirmations WHERE event_id=? AND user_id=?",
                (event_id, user_id),
            )
            await _audit(
                db,
                event_id=event_id,
                user_id=user_id,
                action="response_changed",
                old_status=old_status,
                new_status="thinking",
                now=now,
            )
            await db.commit()
            return ResponseDecision("thinking", True)

        if now >= closes_at and old_status != "going":
            await db.rollback()
            return ResponseDecision("registration_closed")
        if old_status in {"declined", "late_declined"}:
            await db.rollback()
            return ResponseDecision("already")

        if old_status == "going" and now >= safe_until:
            cursor = await db.execute(
                """
                SELECT expires_at FROM event_late_confirmations
                WHERE event_id=? AND user_id=?
                """,
                (event_id, user_id),
            )
            confirmation = await cursor.fetchone()
            if confirmation and int(confirmation["expires_at"]) >= now:
                await _set_response(
                    db,
                    event_id=event_id,
                    user_id=user_id,
                    status="late_declined",
                    nickname=None,
                    telegram_name=telegram_name,
                    joined_at=None,
                    now=now,
                )
                await db.execute(
                    "DELETE FROM event_late_confirmations WHERE event_id=? AND user_id=?",
                    (event_id, user_id),
                )
                await _audit(
                    db,
                    event_id=event_id,
                    user_id=user_id,
                    action="late_decline_confirmed",
                    old_status="going",
                    new_status="late_declined",
                    now=now,
                )
                await db.commit()
                return ResponseDecision("late_declined", True)

            expired = confirmation is not None
            await db.execute(
                """
                INSERT INTO event_late_confirmations (
                    event_id, user_id, expires_at, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id, user_id) DO UPDATE SET
                    expires_at=excluded.expires_at,
                    created_at=excluded.created_at
                """,
                (event_id, user_id, now + 60, now),
            )
            await _audit(
                db,
                event_id=event_id,
                user_id=user_id,
                action="late_decline_warning_started",
                old_status="going",
                new_status="going",
                now=now,
            )
            await db.commit()
            return ResponseDecision(
                "late_confirmation_expired" if expired else "late_warning"
            )

        await _set_response(
            db,
            event_id=event_id,
            user_id=user_id,
            status="declined",
            nickname=None,
            telegram_name=telegram_name,
            joined_at=None,
            now=now,
        )
        await db.execute(
            "DELETE FROM event_late_confirmations WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        )
        await _audit(
            db,
            event_id=event_id,
            user_id=user_id,
            action="response_changed",
            old_status=old_status,
            new_status="declined",
            now=now,
        )
        await db.commit()
        return ResponseDecision("declined", True)


async def is_current_publication(
    event_id: int,
    *,
    chat_id: int,
    message_id: int,
) -> bool:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT 1
            FROM events e
            JOIN event_publications ep ON ep.event_id=e.id AND ep.is_current=1
            WHERE e.id=? AND e.publication_missing=0
              AND ep.chat_id=? AND ep.message_id=?
            """,
            (event_id, chat_id, message_id),
        )
        return await cursor.fetchone() is not None
