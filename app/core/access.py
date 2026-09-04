"""Centralized authorization helpers shared by administrative features."""

from __future__ import annotations

from app.core.config import ADMIN_LOG_CHAT_ID, ALLOWED_CHATS, BOT_OWNER_ID
from app.core.db import get_admin_level
from app.core.event_types import EVENT_ADMIN_MIN_LEVEL


def _chat_ids_containing(fragment: str) -> frozenset[int]:
    normalized = fragment.casefold()
    return frozenset(
        chat_id
        for chat_id, name in ALLOWED_CHATS.items()
        if normalized in name.casefold()
    )


ADMIN_CHAT_IDS = _chat_ids_containing("адміністрац") | {ADMIN_LOG_CHAT_ID}
OFFICER_CHAT_IDS = _chat_ids_containing("офіц")
ADMIN_SAFE_CHAT_IDS = ADMIN_CHAT_IDS | OFFICER_CHAT_IDS


async def get_effective_admin_level(user_id: int) -> int:
    """Return level 4 for the owner and the stored level for everyone else."""

    if int(user_id) == BOT_OWNER_ID:
        return 4
    return await get_admin_level(int(user_id))


async def has_admin_level(user_id: int, minimum: int) -> bool:
    return await get_effective_admin_level(user_id) >= minimum


def is_admin_chat(chat_id: int) -> bool:
    return int(chat_id) in ADMIN_CHAT_IDS


def is_admin_safe_chat(chat_id: int) -> bool:
    return int(chat_id) in ADMIN_SAFE_CHAT_IDS


async def can_manage_events(user_id: int, chat_id: int) -> bool:
    """Events are manageable only by level 3–4 inside the admin chat."""

    return is_admin_chat(chat_id) and await has_admin_level(
        user_id,
        EVENT_ADMIN_MIN_LEVEL,
    )
