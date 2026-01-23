"""Database helpers for the bot."""

from pathlib import Path
import time

import aiosqlite

DB_PATH = Path("data") / "jrfoxy.db"


async def init_db() -> None:
    """Ініціалізує базу даних та створює таблиці, якщо їх ще немає."""

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS call_members (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                is_enabled  INTEGER NOT NULL DEFAULT 1,
                last_seen   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                level INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clan_members (
                user_id INTEGER PRIMARY KEY,
                first_joined_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id     INTEGER NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT NOT NULL,
                updated_at  INTEGER NOT NULL,
                PRIMARY KEY (chat_id, key)
            );
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                issued_by INTEGER NOT NULL,
                issued_by_level INTEGER NOT NULL,
                is_revoked INTEGER NOT NULL DEFAULT 0,
                revoked_at INTEGER,
                revoked_by INTEGER
            );
            """
        )
        await db.commit()


async def ensure_clan_member(user_id: int, joined_at: int) -> None:
    """Гарантує наявність запису про першого вступу користувача до клану."""

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO clan_members (user_id, first_joined_at)
            VALUES (?, ?)
            """,
            (user_id, joined_at),
        )
        await db.commit()


async def upsert_call_member(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    last_seen: int,
) -> None:
    """Додає або оновлює учасника для /call і фіксує час останньої активності."""

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO call_members (
                user_id, username, first_name, last_name, is_enabled, last_seen
                )
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
    """Повертає список активних учасників для /call (за потреби з лімітом)."""

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


async def add_admin(
    user_id: int,
    first_name: str = "",
    last_name: str = "",
    username: str = "",
) -> None:
    """Додає адміністратора або оновлює його профіль із рівнем за замовчуванням."""

    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO admins (
                user_id, first_name, last_name, username, level, created_at, updated_at
                )
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                username=excluded.username,
                updated_at=excluded.updated_at
            """,
            (user_id, first_name, last_name, username, now, now),
        )
        await db.commit()


async def set_admin_level(user_id: int, level: int) -> bool:
    """Змінює рівень адміністратора та повертає True, якщо запис оновлено."""

    now = int(time.time())
    params = (int(level), now, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE admins SET level=?, updated_at=? WHERE user_id=?",
            params,
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_admin(user_id: int) -> bool:
    """Видаляє адміністратора та повертає True, якщо запис існував."""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM admins WHERE user_id=?",
            (user_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_admin_level(user_id: int) -> int:
    """Повертає рівень адміністратора або 0, якщо його немає."""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT level FROM admins WHERE user_id=?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


async def list_admins() -> list[tuple[int, str, str, str, int]]:
    """Повертає список адміністраторів, відсортований за рівнем."""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                user_id,
                COALESCE(first_name, ''),
                COALESCE(last_name, ''),
                COALESCE(username, ''),
                level
            FROM admins
            ORDER BY level DESC, user_id ASC
            """
        )
        rows = await cursor.fetchall()
        return [(int(r[0]), r[1], r[2], r[3], int(r[4])) for r in rows]


async def update_admin_profile(
    user_id: int,
    first_name: str,
    last_name: str,
    username: str,
) -> bool:
    """Оновлює дані профілю адміністратора та повертає True при успіху."""

    now = int(time.time())
    params = (first_name, last_name, username, now, user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE admins
            SET first_name=?, last_name=?, username=?, updated_at=?
            WHERE user_id=?
            """,
            params,
        )
        await db.commit()
        return cursor.rowcount > 0


async def set_chat_setting(
    chat_id: int,
    key: str,
    value: str
    ) -> None:
    """Зберігає або оновлює налаштування чату за ключем."""

    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO chat_settings (chat_id, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (chat_id, key, value, now),
        )
        await db.commit()


async def get_chat_setting(chat_id: int, key: str) -> str | None:
    """Повертає значення налаштування чату або None, якщо ключ відсутній."""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT value FROM chat_settings WHERE chat_id=? AND key=?",
            (chat_id, key),
        )
        row = await cursor.fetchone()
        return str(row[0]) if row else None
