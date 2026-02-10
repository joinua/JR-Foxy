"""Щоденна загальнонаціональна хвилина мовчання."""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import ChatPermissions

from app.core.config import ALLOWED_CHATS, MAIN_CHAT_ID
from app.core.db import get_chat_setting, set_chat_setting

logger = logging.getLogger(__name__)

SILENCE_KEY = "silence_enabled"
SILENCE_START_TEXT = (
    "🕯️ Загальнонаціональна хвилина мовчання.\n"
    "Зупинись на 60 секунд. Вшануй пам’ять наших воїнів і цивільних, які загинули через війну.\n"
    "Схили голову. Подякуй. Пам’ятай."
)
SILENCE_END_TEXT = "Дякую! Слава Україні!"
SILENCE_TZ = ZoneInfo("Europe/Kyiv")


def _seconds_to_next_silence(now: datetime) -> float:
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 0.0)


async def _is_silence_enabled() -> bool:
    value = await get_chat_setting(MAIN_CHAT_ID, SILENCE_KEY)
    if value is None:
        return True
    return value == "1"


async def _delete_message_later(bot: Bot, chat_id: int, message_id: int) -> None:
    await asyncio.sleep(15 * 60)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as exc:
        logger.warning("silence: failed to delete message", extra={"chat_id": chat_id, "message_id": message_id, "error": str(exc)})


def _schedule_delete_message(bot: Bot, chat_id: int, message_id: int) -> None:
    asyncio.create_task(_delete_message_later(bot, chat_id, message_id))


async def _start_silence(bot: Bot, chat_id: int) -> None:
    try:
        sent = await bot.send_message(chat_id, SILENCE_START_TEXT)
        _schedule_delete_message(bot, chat_id, sent.message_id)
    except Exception as exc:
        logger.warning("silence: failed to send start message", extra={"chat_id": chat_id, "error": str(exc)})

    try:
        await bot.set_chat_permissions(
            chat_id,
            ChatPermissions(can_send_messages=False),
        )
    except Exception as exc:
        logger.warning("silence: failed to mute chat", extra={"chat_id": chat_id, "error": str(exc)})


async def _end_silence(bot: Bot, chat_id: int) -> None:
    try:
        await bot.set_chat_permissions(
            chat_id,
            ChatPermissions(
                can_send_messages=True,
                can_send_other_messages=True,
                can_send_polls=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_add_web_page_previews=True,
            ),
        )
    except Exception as exc:
        logger.warning("silence: failed to unmute chat", extra={"chat_id": chat_id, "error": str(exc)})

    try:
        sent = await bot.send_message(chat_id, SILENCE_END_TEXT)
        _schedule_delete_message(bot, chat_id, sent.message_id)
    except Exception as exc:
        logger.warning("silence: failed to send end message", extra={"chat_id": chat_id, "error": str(exc)})


async def run_silence_scheduler(bot: Bot) -> None:
    while True:
        now = datetime.now(SILENCE_TZ)
        wait_seconds = _seconds_to_next_silence(now)
        await asyncio.sleep(wait_seconds)

        run_now = datetime.now(SILENCE_TZ)
        today = run_now.date().isoformat()

        if not await _is_silence_enabled():
            logger.info("silence: skipped (disabled)")
            continue

        last_date = await get_chat_setting(MAIN_CHAT_ID, "silence_last_date")
        if last_date == today:
            logger.info("silence: skipped (already executed today)", extra={"date": today})
            continue

        chat_ids = list(ALLOWED_CHATS.keys())
        logger.info("silence: start", extra={"chat_count": len(chat_ids), "date": today})
        await set_chat_setting(MAIN_CHAT_ID, "silence_last_date", today)

        for chat_id in chat_ids:
            await _start_silence(bot, chat_id)

        await asyncio.sleep(60)

        for chat_id in chat_ids:
            await _end_silence(bot, chat_id)

        logger.info("silence: finished", extra={"chat_count": len(chat_ids), "date": today})
