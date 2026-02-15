"""Service that checks TikTok RSS and posts new video notifications."""

import asyncio
import logging

import feedparser
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.config import (
    ADMIN_LOG_CHAT_ID,
    MAIN_CHAT_ID,
    TIKTOK_NOTIFY_ENABLED,
    TIKTOK_PROFILE_URL,
    TIKTOK_RSS_URL,
    TIKTOK_THREAD_ID,
)
from app.core.db import get_chat_setting, set_chat_setting

logger = logging.getLogger(__name__)

TIKTOK_NOTIFY_ENABLED_KEY = "tiktok_notify_enabled"
TIKTOK_THREAD_ID_KEY = "tiktok_thread_id"
TIKTOK_LAST_VIDEO_ID_KEY = "tiktok_last_video_id"
TIKTOK_RSS_URL_KEY = "tiktok_rss_url"

NEW_VIDEO_TEXT = (
    "На нашій сторінці в ТікТок з'явилося нове відео. "
    "Очікуємо вашої активності!"
)

_rss_missing_logged = False


async def fetch_latest_from_rss(rss_url: str) -> tuple[str, str] | None:
    """Return (video_id, video_url) for the latest RSS entry, or None."""

    try:
        parsed = await asyncio.to_thread(feedparser.parse, rss_url)
    except Exception:
        logger.exception("tiktok rss parse failed")
        return None

    entries = getattr(parsed, "entries", None)
    if not entries:
        return None

    item = entries[0]
    video_url = str(getattr(item, "link", "") or "").strip()
    if not video_url:
        return None

    raw_id = (
        getattr(item, "id", None)
        or getattr(item, "guid", None)
        or getattr(item, "yt_videoid", None)
        or ""
    )
    video_id = str(raw_id).strip() or video_url.rstrip("/")
    return video_id, video_url


async def _run_check(bot: Bot, force: bool = False) -> str:
    """Run one check cycle and return internal status string."""

    global _rss_missing_logged

    enabled_raw = await get_chat_setting(MAIN_CHAT_ID, TIKTOK_NOTIFY_ENABLED_KEY)
    if enabled_raw is None:
        enabled = bool(TIKTOK_NOTIFY_ENABLED)
    else:
        enabled = enabled_raw == "1"

    if not enabled and not force:
        return "disabled"

    rss_url = await get_chat_setting(MAIN_CHAT_ID, TIKTOK_RSS_URL_KEY)
    rss_url = (rss_url or "").strip() or TIKTOK_RSS_URL
    if not rss_url:
        if not _rss_missing_logged:
            logger.warning(
                "tiktok rss url missing",
                extra={"profile_url": TIKTOK_PROFILE_URL},
            )
            _rss_missing_logged = True
        return "rss_missing"

    _rss_missing_logged = False

    latest = await fetch_latest_from_rss(rss_url)
    if latest is None:
        return "no_updates"

    latest_id, video_url = latest
    last_id = await get_chat_setting(MAIN_CHAT_ID, TIKTOK_LAST_VIDEO_ID_KEY)

    if latest_id == last_id:
        return "no_updates"

    thread_raw = await get_chat_setting(MAIN_CHAT_ID, TIKTOK_THREAD_ID_KEY)
    thread_id = None
    if thread_raw is not None and thread_raw.strip():
        try:
            thread_id = int(thread_raw)
        except ValueError:
            logger.warning("invalid tiktok thread_id in chat_settings")

    if thread_id is None:
        thread_id = TIKTOK_THREAD_ID

    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Відкрити відео", url=video_url)],
        ]
    )

    send_kwargs: dict = {
        "chat_id": MAIN_CHAT_ID,
        "text": NEW_VIDEO_TEXT,
        "reply_markup": reply_markup,
    }
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id

    try:
        await bot.send_message(**send_kwargs)
    except Exception as exc:
        logger.exception("TikTok notify failed: %s", exc)
        return "no_updates"

    await set_chat_setting(MAIN_CHAT_ID, TIKTOK_LAST_VIDEO_ID_KEY, latest_id)

    await bot.send_message(
        ADMIN_LOG_CHAT_ID,
        f"TikTok: опубліковано нове відео: {video_url}",
    )
    logger.info("tiktok new video published", extra={"video_url": video_url})
    return "posted"


async def check_and_notify(bot: Bot) -> bool:
    """Check RSS and post a new video message if found."""

    status = await _run_check(bot, force=False)
    return status == "posted"


async def force_check(bot: Bot) -> str:
    """Forced one-off check for admin command."""

    return await _run_check(bot, force=True)
