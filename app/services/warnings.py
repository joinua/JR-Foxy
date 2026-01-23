"""Warning service layer for audit-friendly discipline actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
import time

import aiosqlite

from app.core.db import DB_PATH


WARN_EXPIRY_DAYS = 30


@dataclass(frozen=True)
class WarningRecord:
    """Immutable warning record snapshot for read operations."""

    id: int
    user_id: int
    chat_id: int
    reason: str
    issued_at: int
    expires_at: int
    issued_by: int
    issued_by_level: int
    is_revoked: bool
    revoked_at: int | None
    revoked_by: int | None


def _row_to_warning(row: tuple) -> WarningRecord:
    return WarningRecord(
        id=int(row[0]),
        user_id=int(row[1]),
        chat_id=int(row[2]),
        reason=str(row[3]),
        issued_at=int(row[4]),
        expires_at=int(row[5]),
        issued_by=int(row[6]),
        issued_by_level=int(row[7]),
        is_revoked=bool(row[8]),
        revoked_at=int(row[9]) if row[9] is not None else None,
        revoked_by=int(row[10]) if row[10] is not None else None,
    )


def build_mention(user_id: int, first_name: str | None, last_name: str | None) -> str:
    """Build a safe HTML mention for a user id.

    We avoid relying on username because it can change or be missing.
    """

    name_parts = [part for part in (first_name or "", last_name or "") if part]
    display_name = " ".join(name_parts).strip() or str(user_id)
    return f'<a href="tg://user?id={user_id}">{html.escape(display_name)}</a>'


def format_uk_date(timestamp: int) -> str:
    """Format timestamp as Ukrainian date string for user-facing messages."""

    months = {
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
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    month = months.get(date.month, "")
    return f"{date.day} {month} {date.year} року"


def _now_ts() -> int:
    return int(time.time())


def _expiry_ts(issued_at: int) -> int:
    return int((datetime.fromtimestamp(issued_at, tz=timezone.utc)
                + timedelta(days=WARN_EXPIRY_DAYS)).timestamp())


async def create_warning(
    user_id: int,
    chat_id: int,
    reason: str,
    issued_by: int,
    issued_by_level: int,
) -> tuple[WarningRecord, int]:
    """Create a warning and return the created record with active count.

    We use a transaction to keep insert + count consistent under concurrency.
    """

    issued_at = _now_ts()
    expires_at = _expiry_ts(issued_at)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            INSERT INTO warnings (
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                is_revoked,
                revoked_at,
                revoked_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
            """,
            (
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
            ),
        )
        cursor = await db.execute("SELECT last_insert_rowid()")
        warning_id = int((await cursor.fetchone())[0])
        active_count = await count_active_warnings(user_id, db=db, now=issued_at)
        await db.commit()

    warning = WarningRecord(
        id=warning_id,
        user_id=user_id,
        chat_id=chat_id,
        reason=reason,
        issued_at=issued_at,
        expires_at=expires_at,
        issued_by=issued_by,
        issued_by_level=issued_by_level,
        is_revoked=False,
        revoked_at=None,
        revoked_by=None,
    )
    return warning, active_count


async def count_active_warnings(
    user_id: int,
    *,
    db: aiosqlite.Connection | None = None,
    now: int | None = None,
) -> int:
    """Count active warnings (not revoked and not expired)."""

    now_ts = now if now is not None else _now_ts()
    if db is None:
        async with aiosqlite.connect(DB_PATH) as db_conn:
            return await count_active_warnings(user_id, db=db_conn, now=now_ts)

    cursor = await db.execute(
        """
        SELECT COUNT(*)
        FROM warnings
        WHERE user_id = ?
          AND is_revoked = 0
          AND expires_at > ?
        """,
        (user_id, now_ts),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def list_active_warnings(user_id: int) -> list[WarningRecord]:
    """Return active warnings ordered from newest to oldest."""

    now_ts = _now_ts()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                is_revoked,
                revoked_at,
                revoked_by
            FROM warnings
            WHERE user_id = ?
              AND is_revoked = 0
              AND expires_at > ?
            ORDER BY issued_at DESC, id DESC
            """,
            (user_id, now_ts),
        )
        rows = await cursor.fetchall()
        return [_row_to_warning(row) for row in rows]


async def list_warning_history(user_id: int) -> list[WarningRecord]:
    """Return full warning history ordered from newest to oldest."""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                is_revoked,
                revoked_at,
                revoked_by
            FROM warnings
            WHERE user_id = ?
            ORDER BY issued_at DESC, id DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_warning(row) for row in rows]


async def revoke_latest_warning(
    user_id: int,
    revoked_by: int,
) -> tuple[WarningRecord | None, int]:
    """Revoke the latest active warning and return it with active count.

    The warning is marked revoked instead of deleted for auditability.
    """

    now_ts = _now_ts()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT
                id,
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                is_revoked,
                revoked_at,
                revoked_by
            FROM warnings
            WHERE user_id = ?
              AND is_revoked = 0
              AND expires_at > ?
            ORDER BY issued_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, now_ts),
        )
        row = await cursor.fetchone()
        if not row:
            active_count = await count_active_warnings(user_id, db=db, now=now_ts)
            await db.commit()
            return None, active_count

        warning = _row_to_warning(row)
        await db.execute(
            """
            UPDATE warnings
            SET is_revoked = 1,
                revoked_at = ?,
                revoked_by = ?
            WHERE id = ?
            """,
            (now_ts, revoked_by, warning.id),
        )
        active_count = await count_active_warnings(user_id, db=db, now=now_ts)
        await db.commit()

    revoked_warning = WarningRecord(
        id=warning.id,
        user_id=warning.user_id,
        chat_id=warning.chat_id,
        reason=warning.reason,
        issued_at=warning.issued_at,
        expires_at=warning.expires_at,
        issued_by=warning.issued_by,
        issued_by_level=warning.issued_by_level,
        is_revoked=True,
        revoked_at=now_ts,
        revoked_by=revoked_by,
    )
    return revoked_warning, active_count


def warning_status_label(warning: WarningRecord, now: int | None = None) -> str:
    """Return a human-readable status for a warning record."""

    now_ts = now if now is not None else _now_ts()
    if warning.is_revoked:
        return "скасовано"
    if warning.expires_at <= now_ts:
        return "прострочено"
    return "активне"
