"""Admin commands for TikTok notifications."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core import config as settings
from app.core.db import get_admin_level, set_chat_setting
from app.services.tiktok_watcher import (
    TIKTOK_NOTIFY_ENABLED_KEY,
    TIKTOK_THREAD_ID_KEY,
    force_check,
)

router = Router()


def is_private(message: Message) -> bool:
    """Return True when command is called in private chat."""

    return message.chat.type == "private"


async def require_level(message: Message, min_level: int) -> bool:
    """Ensure user has at least required admin level."""

    if not message.from_user:
        await message.answer("Недостатній рівень.")
        return False

    level = await get_admin_level(message.from_user.id)
    if level < min_level:
        await message.answer("Недостатній рівень.")
        return False

    return True


async def ensure_private(message: Message) -> bool:
    """Ensure command is used in private chat."""

    if is_private(message):
        return True

    await message.answer("Команда доступна лише в приватних повідомленнях.")
    return False


@router.message(Command("tiktok_set_thread"))
async def tiktok_set_thread_handler(message: Message) -> None:
    """Store TikTok forum thread id for MAIN_CHAT_ID."""

    if not await require_level(message, 4):
        return

    if message.chat.id != settings.MAIN_CHAT_ID:
        await message.answer("❌ Команду потрібно виконувати в головному чаті.")
        return

    thread_id = message.message_thread_id
    if thread_id is None:
        await message.answer(
            "❌ Це не форум-тема. Відкрий тему 'Тік-Ток' і повтори команду."
        )
        return

    await set_chat_setting(settings.MAIN_CHAT_ID, TIKTOK_THREAD_ID_KEY, str(thread_id))
    await message.answer(f"✅ TikTok thread_id встановлено: {thread_id}")


@router.message(Command("tiktok_enable"))
async def tiktok_enable_handler(message: Message) -> None:
    """Enable TikTok notifications."""

    if not await ensure_private(message):
        return

    if not await require_level(message, 4):
        return

    await set_chat_setting(settings.MAIN_CHAT_ID, TIKTOK_NOTIFY_ENABLED_KEY, "1")
    await message.answer("✅ TikTok Notify увімкнено.")


@router.message(Command("tiktok_disable"))
async def tiktok_disable_handler(message: Message) -> None:
    """Disable TikTok notifications."""

    if not await ensure_private(message):
        return

    if not await require_level(message, 4):
        return

    await set_chat_setting(settings.MAIN_CHAT_ID, TIKTOK_NOTIFY_ENABLED_KEY, "0")
    await message.answer("✅ TikTok Notify вимкнено.")


@router.message(Command("tiktok_check"))
async def tiktok_check_handler(message: Message) -> None:
    """Force one TikTok RSS check and return short status."""

    in_admin_chat = message.chat.id == settings.ADMIN_LOG_CHAT_ID
    if not (is_private(message) or in_admin_chat):
        await message.answer("Команда доступна в приваті або в адмін-чаті.")
        return

    if not await require_level(message, 3):
        return

    status = await force_check(message.bot)
    if status == "posted":
        await message.answer("✅ Нове відео запощено.")
        return

    if status == "rss_missing":
        await message.answer("❌ TikTok RSS URL не налаштовано.")
        return

    await message.answer("ℹ️ Нових відео немає.")
