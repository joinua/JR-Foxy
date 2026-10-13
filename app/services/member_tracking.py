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


async def reconcile_members(bot) -> None:
    """Detect missed departures only for previously verified main-chat users.

    Telegram errors never count as a confirmed departure. Old reception-only
    identities are probed to seed the roster but cannot trigger exit cards.
    """
    from app.dao import clan_exit as exit_dao
    from app.services import clan_exit, profile_service

    self_id = (await bot.get_me()).id
    for user_id, was_observed in await known_member_ids():
        if user_id == self_id:
            continue
        try:
            member = await asyncio.wait_for(
                bot.get_chat_member(MAIN_CHAT_ID, user_id), timeout=8
            )
        except Exception as exc:
            logger.debug("Cannot confirm membership for %s: %s", user_id, exc)
            continue

        user = member.user
        if user.is_bot:
            continue
        profile = await profile_service.get_profile(user_id)
        status = getattr(member.status, "value", member.status)
        if is_current_member(member):
            await observe_main_member(user_id)
            if profile is None or profile.get("status") != "active" or profile.get("archived_at"):
                if profile is not None:
                    await profile_service.reactivate_profile(user_id)
                await profile_service.sync_telegram_user(user, create=True)
            previous = await exit_dao.get_open_for_user(user_id)
            if previous:
                await clan_exit.mark_returned(bot, previous)
        elif (was_observed and status in {"left", "kicked"} and profile
              and profile.get("status") == "active" and not profile.get("archived_at")):
            logger.warning("Recovering missed exit for confirmed member %s", user_id)
            profile = await profile_service.fill_missing_join_date(user_id) or profile
            await profile_service.archive_profile(user_id)
            await clan_exit.record_exit(
                bot, user, "вихід виявлено під час звірки; спосіб невідомий", profile
            )
