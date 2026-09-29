"""Bounded RSS checks, durable delivery history and explicit diagnostic results."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp
import feedparser
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core import config as settings
from app.core.db import get_chat_setting, set_chat_setting
from app.dao import tiktok as history

logger = logging.getLogger(__name__)
TIKTOK_NOTIFY_ENABLED_KEY = "tiktok_notify_enabled"
TIKTOK_THREAD_ID_KEY = "tiktok_thread_id"
TIKTOK_LAST_VIDEO_ID_KEY = "tiktok_last_video_id"
TIKTOK_RSS_URL_KEY = "tiktok_rss_url"
TIKTOK_CHECK_STATE_KEY = "tiktok_check_state"
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_ENTRIES = 200
MAX_POSTS_PER_CHECK = 5
_check_lock = asyncio.Lock()

NEW_VIDEO_TEXT = (
    "На нашій сторінці в ТікТок з'явилося нове відео. " "Очікуємо вашої активності!"
)


@dataclass(frozen=True)
class Video:
    video_id: str
    feed_id: str
    url: str
    published_at: int | None


@dataclass(frozen=True)
class TikTokSettings:
    enabled: bool
    rss_url: str
    thread_id: int | None
    rss_source: str


@dataclass(frozen=True)
class CheckResult:
    status: str
    posted: int = 0
    remaining: int = 0
    reason: str = ""
    enabled: bool = True
    latest_published_at: int | None = None


class FeedError(Exception):
    """Only fixed diagnostic codes; never embed feed URLs or response bodies."""


async def load_settings() -> TikTokSettings:
    chat = settings.MAIN_CHAT_ID
    enabled = await get_chat_setting(chat, TIKTOK_NOTIFY_ENABLED_KEY)
    rss = (await get_chat_setting(chat, TIKTOK_RSS_URL_KEY) or "").strip()
    thread = await get_chat_setting(chat, TIKTOK_THREAD_ID_KEY)
    thread_id = settings.TIKTOK_THREAD_ID
    if thread is not None and thread.strip():
        try:
            thread_id = int(thread)
        except ValueError:
            raise FeedError("invalid_thread") from None
    if thread_id is not None and thread_id <= 0:
        raise FeedError("invalid_thread")
    return TikTokSettings(
        settings.TIKTOK_NOTIFY_ENABLED if enabled is None else enabled == "1",
        rss or settings.TIKTOK_RSS_URL,
        thread_id,
        "database" if rss else "environment",
    )


def parse_feed(data: bytes) -> list[Video]:
    parsed = feedparser.parse(data)
    if not parsed.get("version") or parsed.get("bozo"):
        raise FeedError("invalid_feed")
    entries = parsed.get("entries", [])
    if len(entries) > MAX_FEED_ENTRIES:
        raise FeedError("too_many_entries")
    videos: dict[str, Video] = {}
    for entry in entries:
        url = str(entry.get("link") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            raise FeedError("invalid_video_url")
        feed_id = str(entry.get("id") or entry.get("guid") or url).strip()
        match = re.search(r"/video/(\d+)(?:/|$)", parts.path)
        is_tiktok = parts.hostname == "tiktok.com" or parts.hostname.endswith(
            ".tiktok.com"
        )
        video_id = "tiktok:" + match[1] if is_tiktok and match else feed_id
        date = entry.get("published_parsed") or entry.get("updated_parsed")
        published_at = calendar.timegm(date) if date else None
        videos.setdefault(video_id, Video(video_id, feed_id, url, published_at))
    # RSS.app can put pinned or otherwise old posts anywhere in the list.
    return sorted(videos.values(), key=lambda v: (v.published_at or 0, v.video_id))


async def fetch_videos(rss_url: str) -> list[Video]:
    parts = urlsplit(rss_url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise FeedError("invalid_rss_url")
    timeout = aiohttp.ClientTimeout(total=20, connect=10, sock_read=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(rss_url) as response:
                if response.status != 200:
                    raise FeedError(f"http_{response.status}")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_FEED_BYTES:
                        raise FeedError("feed_too_large")
    except TimeoutError:
        raise FeedError("timeout") from None
    except aiohttp.ClientError:
        raise FeedError("network_error") from None
    return await asyncio.to_thread(parse_feed, bytes(data))


async def _check(bot: Bot, cfg: TikTokSettings, *, force: bool) -> CheckResult:
    if not cfg.enabled and not force:
        return CheckResult("disabled", enabled=False)
    if not cfg.rss_url:
        return CheckResult("rss_missing", enabled=cfg.enabled)
    videos = await fetch_videos(cfg.rss_url)
    if not videos:
        return CheckResult("empty_feed", enabled=cfg.enabled)
    newest = max((v.published_at for v in videos if v.published_at), default=None)
    feed_key = hashlib.sha256(cfg.rss_url.encode()).hexdigest()
    initialized = False
    if not await history.is_initialized(settings.MAIN_CHAT_ID, feed_key):
        legacy_id = await get_chat_setting(
            settings.MAIN_CHAT_ID, TIKTOK_LAST_VIDEO_ID_KEY
        )
        checkpoint = next(
            (v for v in videos if v.feed_id == legacy_id or v.video_id == legacy_id),
            None,
        )
        # Migrate a known checkpoint by date, never by feed position. If it cannot
        # be located/dated, baseline the feed rather than flooding old posts.
        baseline = [
            v.video_id
            for v in videos
            if checkpoint is None
            or checkpoint.published_at is None
            or v.published_at is None
            or v.published_at <= checkpoint.published_at
        ]
        await history.initialize_feed(settings.MAIN_CHAT_ID, feed_key, baseline)
        initialized = True
    seen = await history.seen_ids(settings.MAIN_CHAT_ID, [v.video_id for v in videos])
    pending = sorted(
        (v for v in videos if v.video_id not in seen),
        key=lambda v: (v.published_at or 0, v.video_id),
    )
    posted = 0
    for video in pending[:MAX_POSTS_PER_CHECK]:
        try:
            await asyncio.wait_for(
                bot.send_message(
                    chat_id=settings.MAIN_CHAT_ID,
                    text=NEW_VIDEO_TEXT,
                    message_thread_id=cfg.thread_id,
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="Відкрити відео", url=video.url)]
                        ]
                    ),
                    request_timeout=15,
                ),
                timeout=20,
            )
        except Exception as exc:
            logger.warning("tiktok send failed: %s", type(exc).__name__)
            return CheckResult(
                "telegram_error",
                posted,
                len(pending) - posted,
                type(exc).__name__,
                cfg.enabled,
                newest,
            )
        posted += 1
        # Only confirmed sends become seen. Report already delivered messages even
        # if persisting their history fails; a retry can then produce a duplicate.
        try:
            await history.mark_seen(settings.MAIN_CHAT_ID, video.video_id)
            await set_chat_setting(
                settings.MAIN_CHAT_ID, TIKTOK_LAST_VIDEO_ID_KEY, video.feed_id
            )
        except sqlite3.Error as exc:
            logger.warning("tiktok delivery history failed: %s", type(exc).__name__)
            return CheckResult(
                "storage_error",
                posted,
                len(pending) - posted,
                type(exc).__name__,
                cfg.enabled,
                newest,
            )
        try:
            await asyncio.wait_for(
                bot.send_message(
                    settings.ADMIN_LOG_CHAT_ID,
                    f"TikTok: опубліковано нове відео: {video.url}",
                    request_timeout=5,
                ),
                timeout=8,
            )
        except Exception as exc:
            # An audit-chat failure must not invalidate the main-chat delivery.
            logger.warning("tiktok audit send failed: %s", type(exc).__name__)
    status = "posted" if posted else "initialized" if initialized else "no_updates"
    return CheckResult(
        status,
        posted,
        len(pending) - posted,
        enabled=cfg.enabled,
        latest_published_at=newest,
    )


async def _run_check(bot: Bot, *, force: bool = False) -> CheckResult:
    # Manual commands and scheduled checks share this lock in the single bot process.
    if _check_lock.locked():
        return CheckResult("busy")
    async with _check_lock:
        cfg = None
        try:
            cfg = await load_settings()
            result = await _check(bot, cfg, force=force)
        except FeedError as exc:
            result = CheckResult(
                "feed_error", reason=str(exc), enabled=cfg.enabled if cfg else True
            )
        except sqlite3.Error as exc:
            logger.warning("tiktok storage failed: %s", type(exc).__name__)
            result = CheckResult(
                "storage_error",
                reason=type(exc).__name__,
                enabled=cfg.enabled if cfg else True,
            )
        except Exception as exc:
            logger.warning("tiktok check failed: %s", type(exc).__name__)
            result = CheckResult(
                "internal_error",
                reason=type(exc).__name__,
                enabled=cfg.enabled if cfg else True,
            )
        state = dict(
            checked_at=int(time.time()),
            status=result.status,
            posted=result.posted,
            remaining=result.remaining,
            reason=result.reason,
            latest_published_at=result.latest_published_at,
        )
        try:
            await set_chat_setting(
                settings.MAIN_CHAT_ID,
                TIKTOK_CHECK_STATE_KEY,
                json.dumps(state, ensure_ascii=False),
            )
        except sqlite3.Error:
            logger.warning("tiktok check status could not be persisted")
        logger.info(
            "tiktok check: status=%s posted=%s remaining=%s reason=%s",
            result.status,
            result.posted,
            result.remaining,
            result.reason,
        )
        return result


async def check_and_notify(bot: Bot) -> CheckResult:
    return await _run_check(bot)


async def force_check(bot: Bot) -> CheckResult:
    return await _run_check(bot, force=True)
