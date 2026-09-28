"""Persistent TikTok delivery history; no production credentials in records."""

import time

import aiosqlite

from app.core import db as core_db


async def ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS tiktok_feed_state (
            chat_id INTEGER NOT NULL,
            feed_key TEXT NOT NULL,
            initialized_at INTEGER NOT NULL,
            PRIMARY KEY (chat_id, feed_key)
        );
        CREATE TABLE IF NOT EXISTS tiktok_seen_videos (
            chat_id INTEGER NOT NULL,
            video_id TEXT NOT NULL,
            recorded_at INTEGER NOT NULL,
            PRIMARY KEY (chat_id, video_id)
        );
    """)


async def is_initialized(chat_id: int, feed_key: str) -> bool:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM tiktok_feed_state WHERE chat_id=? AND feed_key=?",
            (chat_id, feed_key),
        )
        return await cur.fetchone() is not None


async def initialize_feed(chat_id: int, feed_key: str, baseline: list[str]) -> None:
    """Record old entries and initialization atomically, preserving existing history."""
    now = int(time.time())
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "INSERT OR IGNORE INTO tiktok_feed_state VALUES (?, ?, ?)",
            (chat_id, feed_key, now),
        )
        if cur.rowcount:
            await db.executemany(
                "INSERT OR IGNORE INTO tiktok_seen_videos VALUES (?, ?, ?)",
                [(chat_id, video_id, now) for video_id in baseline],
            )
        await db.commit()


async def seen_ids(chat_id: int, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        placeholders = ",".join("?" for _ in ids)
        cur = await db.execute(
            f"SELECT video_id FROM tiktok_seen_videos WHERE chat_id=? "
            f"AND video_id IN ({placeholders})",
            (chat_id, *ids),
        )
        return {row[0] for row in await cur.fetchall()}


async def mark_seen(chat_id: int, video_id: str) -> None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO tiktok_seen_videos VALUES (?, ?, ?)",
            (chat_id, video_id, int(time.time())),
        )
        await db.commit()


async def next_task() -> tuple[str, int] | None:
    async with aiosqlite.connect(core_db.DB_PATH) as db:
        cur = await db.execute("""SELECT status, run_at FROM scheduled_tasks
               WHERE task_type='tiktok_check' AND status IN ('pending', 'running')
               ORDER BY run_at, id LIMIT 1""")
        row = await cur.fetchone()
        return (str(row[0]), int(row[1])) if row else None
