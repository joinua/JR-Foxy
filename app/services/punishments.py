"""Punishment checks derived from warning state."""

from __future__ import annotations

from aiogram import Bot


async def enforce_warning_ban(bot: Bot, chat_id: int, user_id: int, active_count: int) -> None:
    """Apply or lift a ban depending on active warning count.

    The ban decision is derived from the current warning state to keep history intact.
    """

    if active_count >= 3:
        try:
            await bot.ban_chat_member(chat_id, user_id)
        except Exception:
            # We avoid interrupting moderation flow if Telegram rejects the ban.
            return
        return

    try:
        await bot.unban_chat_member(chat_id, user_id)
    except Exception:
        # If the user is not banned, we can safely ignore the error.
        return
