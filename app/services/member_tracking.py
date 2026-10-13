"""Track verified main-chat members without confusing reception candidates."""
from __future__ import annotations

import asyncio
import logging
import time

import aiosqlite

from app.core.config import MAIN_CHAT_ID
from app.core.db import DB_PATH

logger = logging.getLogger(__name__)


async def observe_main_member(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO main_chat_observed(user_id, last_confirmed_at)
               VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   last_confirmed_at=excluded.last_confirmed_at""",
            (user_id, int(time.time())),
        )
        await db.commit()


def is_current_member(member) -> bool:
    status = getattr(member.status, "value", member.status)
    return status in {"member", "administrator", "creator"} or (
        status == "restricted" and bool(getattr(member, "is_member", False))
    )


async def known_member_ids() -> list[tuple[int, bool]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """WITH known AS (
                SELECT user_id FROM main_chat_observed
                UNION SELECT user_id FROM clan_members
                UNION SELECT user_id FROM profiles
                WHERE COALESCE(status, 'active') = 'active'
                AND archived_at IS NULL AND deleted_at IS NULL
            )
            SELECT k.user_id, m.user_id IS NOT NULL
            FROM known k
            LEFT JOIN main_chat_observed m ON m.user_id=k.user_id
            ORDER BY k.user_id"""
        )
        return [(int(r[0]), bool(r[1])) for r in await cursor.fetchall()]
