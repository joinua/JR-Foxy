"""Admin commands for TikTok notifications."""

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core import config as settings
from app.core.db import (
    ensure_periodic_task,
    get_admin_level,
    get_chat_setting,
    set_chat_setting,
)
from app.dao.tiktok import next_task
from app.services.tiktok_watcher import (
    CheckResult,
    FeedError,
    TIKTOK_CHECK_STATE_KEY,
    TIKTOK_NOTIFY_ENABLED_KEY,
    TIKTOK_THREAD_ID_KEY,
    force_check,
    load_settings,
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
    await ensure_periodic_task("tiktok_check", int(time.time()))
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

    if not await require_level(message, 4):
        return

    result = await force_check(message.bot)
    text = result_text(result)
    if not result.enabled:
        text += "\nАвтопублікація вимкнена; це була лише ручна перевірка."
    await message.answer(text)


def result_text(result: CheckResult) -> str:
    messages = {
        "disabled": "Автопублікація вимкнена.",
        "rss_missing": "❌ TikTok RSS URL не налаштовано.",
        "empty_feed": "ℹ️ RSS доступний, але не містить публікацій.",
        "initialized": "ℹ️ Початковий список збережено. Історичні відео не розсилалися; наступні нові записи будуть оброблені.",
        "no_updates": "ℹ️ RSS перевірено. Нових необроблених відео немає.",
        "busy": "ℹ️ Інша перевірка TikTok уже виконується.",
        "telegram_error": "❌ Telegram не підтвердив надсилання. Перевірте права бота й тему TikTok; можливий також збій мережі.",
        "storage_error": "❌ Помилка збереження стану TikTok у базі даних.",
        "internal_error": "❌ Внутрішня помилка перевірки TikTok. Потрібна перевірка журналу.",
    }
    if result.status == "posted":
        text = f"✅ Опубліковано відео: {result.posted}."
    elif result.status == "feed_error":
        reasons = {
            "timeout": "RSS не відповів у відведений час.",
            "network_error": "Не вдалося підключитися до RSS.",
            "invalid_feed": "Сервіс повернув некоректний RSS/Atom.",
            "invalid_thread": "Некоректний ID теми TikTok. Використайте /tiktok_set_thread у потрібній темі.",
            "invalid_rss_url": "Некоректне посилання RSS.",
            "invalid_video_url": "У RSS є некоректне посилання на відео.",
            "feed_too_large": "RSS перевищує допустимий розмір.",
            "too_many_entries": "У RSS забагато записів для однієї перевірки.",
        }
        reason = reasons.get(result.reason, "Не вдалося прочитати RSS.")
        if result.reason.startswith("http_") and result.reason[5:].isdigit():
            reason = f"RSS повернув HTTP {result.reason[5:]}."
        text = "❌ " + reason
    else:
        text = messages.get(result.status, "Стан ще не визначено.")
    if result.posted and result.status != "posted":
        text += f"\nДо помилки опубліковано: {result.posted}."
    if result.remaining:
        text += f"\nЗалишилося необроблених: {result.remaining}."
    return text


def _format_time(value: int | None) -> str:
    if value is None:
        return "немає даних"
    return datetime.fromtimestamp(value, ZoneInfo("Europe/Kyiv")).strftime(
        "%d.%m.%Y %H:%M:%S"
    )


@router.message(Command("tiktok_status"))
async def tiktok_status_handler(message: Message) -> None:
    """Read status without fetching RSS or posting to the clan."""
    if not (is_private(message) or message.chat.id == settings.ADMIN_LOG_CHAT_ID):
        await message.answer("Команда доступна в приваті або в адмін-чаті.")
        return
    if not await require_level(message, 2):
        return
    try:
        cfg = await load_settings()
    except FeedError as exc:
        await message.answer(result_text(CheckResult("feed_error", reason=str(exc))))
        return
    raw = await get_chat_setting(settings.MAIN_CHAT_ID, TIKTOK_CHECK_STATE_KEY)
    try:
        state = json.loads(raw or "{}")
        if not isinstance(state, dict):
            state = {}
        last = _format_time(state.get("checked_at"))
        newest = _format_time(state.get("latest_published_at"))
        posted = max(0, int(state.get("posted", 0)))
        remaining = max(0, int(state.get("remaining", 0)))
    except (ValueError, TypeError, OverflowError, OSError):
        state, last, newest = {}, "немає даних", "немає даних"
        posted, remaining = 0, 0
    task = await next_task()
    queue = f"{task[0]} — {_format_time(task[1])}" if task else "немає активної задачі"
    status = result_text(
        CheckResult(
            str(state.get("status", "unknown")),
            posted=posted,
            remaining=remaining,
            reason=str(state.get("reason", "")),
        )
    )
    await message.answer(
        f"TikTok: {'увімкнено' if cfg.enabled else 'вимкнено'}\n"
        f"Інтервал: {settings.TIKTOK_CHECK_INTERVAL_SECONDS} с\n"
        f"RSS: {'налаштовано' if cfg.rss_url else 'не налаштовано'}\n"
        f"Тема: {cfg.thread_id if cfg.thread_id is not None else 'загальний чат'}\n"
        f"Остання перевірка: {last}\n{status}\n"
        f"Найновіша дата публікації у перевіреному RSS: {newest}\n"
        f"Задача: {queue}\nЧас — за Києвом."
    )
