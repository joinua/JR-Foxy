from pathlib import Path

import aiosqlite

DB_PATH = Path("data") / "jrfoxy.db"


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
        CREATE TABLE IF NOT EXISTS call_members (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            last_name   TEXT,
            is_enabled  INTEGER NOT NULL DEFAULT 1,
            last_seen   INTEGER NOT NULL
        )
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            level INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
        )
        await db.commit()


async def upsert_call_member(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    last_seen: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO call_members (user_id, username, first_name, last_name, is_enabled, last_seen)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_seen=excluded.last_seen
            """,
            (user_id, username, first_name, last_name, last_seen),
        )
        await db.commit()


async def get_call_members(limit: int | None = None) -> list[tuple[int, str | None]]:
    async with aiosqlite.connect(DB_PATH) as db:
        query = """
        SELECT user_id, username
        FROM call_members
        WHERE is_enabled = 1
        ORDER BY last_seen DESC
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor = await db.execute(query)
        return await cursor.fetchall()



import time
from typing import Optional

# 1) гарантуємо таблицю (якщо в тебе нема централізованого init)
async def ensure_admins_table() -> None:
    await db_execute("""
    CREATE TABLE IF NOT EXISTS admins (
      user_id INTEGER PRIMARY KEY,
      first_name TEXT,
      last_name TEXT,
      username TEXT,
      level INTEGER NOT NULL DEFAULT 1,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    """)

async def add_admin(user_id: int, first_name: str = "", last_name: str = "", username: str = "") -> None:
    now = int(time.time())
    await db_execute(
        """
        INSERT INTO admins (user_id, first_name, last_name, username, level, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            username=excluded.username,
            updated_at=excluded.updated_at
        """,
        (user_id, first_name, last_name, username, now, now),
    )

async def set_admin_level(user_id: int, level: int) -> bool:
    # повертає True якщо юзер існує
    level = int(level)
    if level < 1:
        level = 1
    if level > 4:
        level = 4

    now = int(time.time())
    cur = await db_execute(
        "UPDATE admins SET level=?, updated_at=? WHERE user_id=?",
        (level, now, user_id),
        return_cursor=True
    )
    return cur.rowcount > 0

async def delete_admin(user_id: int) -> bool:
    cur = await db_execute(
        "DELETE FROM admins WHERE user_id=?",
        (user_id,),
        return_cursor=True
    )
    return cur.rowcount > 0

async def get_admin_level(user_id: int) -> int:
    row = await db_fetchone("SELECT level FROM admins WHERE user_id=?", (user_id,))
    return int(row[0]) if row else 0

async def list_admins() -> list[tuple[int, str, str, str, int]]:
    # user_id, first_name, last_name, username, level
    rows = await db_fetchall(
        "SELECT user_id, COALESCE(first_name,''), COALESCE(last_name,''), COALESCE(username,''), level FROM admins ORDER BY level DESC, user_id ASC",
        ()
    )
    return [(int(r[0]), r[1], r[2], r[3], int(r[4])) for r in rows]

