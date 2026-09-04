"""Read-only reliability history queries."""

from __future__ import annotations

from typing import Any

import aiosqlite

from app.core import db as core_db


async def get_rating_rows(user_id: int, *, limit: int = 12) -> list[dict[str, Any]]:
    """Return rating-bearing results; neutral and terminally void events do not count."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT e.id AS event_id, e.title, e.starts_at_utc,
                   r.result, r.nickname_snapshot
            FROM event_results r
            JOIN events e ON e.id=r.event_id
            WHERE r.user_id=?
              AND r.finalized_at IS NOT NULL
              AND r.result IN ('present', 'no_show', 'late_decline')
              AND e.status='completed'
            ORDER BY e.starts_at_utc DESC, e.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_recent_rows(user_id: int, *, limit: int = 5) -> list[dict[str, Any]]:
    """Return recent finalized history, including neutral results and their reason."""

    async with aiosqlite.connect(core_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT e.id AS event_id, e.title, e.starts_at_utc,
                   r.result, r.exclusion_reason, r.nickname_snapshot,
                   r.correction_reason, r.corrected_at
            FROM event_results r
            JOIN events e ON e.id=r.event_id
            WHERE r.user_id=?
              AND r.finalized_at IS NOT NULL
              AND e.status='completed'
            ORDER BY e.starts_at_utc DESC, e.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]
