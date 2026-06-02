"""SQLite access helpers for player profiles."""

from __future__ import annotations

import sqlite3

import aiosqlite

from app.core.db import DB_PATH


class DuplicateUIDError(Exception):
    """The requested CODM UID is already assigned."""


async def _fetch_profile(db: aiosqlite.Connection, user_id: int) -> dict | None:
    cursor = await db.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_profile(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await _fetch_profile(db, user_id)


async def find_profile_by_username(username: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM profiles
            WHERE telegram_username = ? COLLATE NOCASE
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (username,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def upsert_telegram_snapshot(
    *,
    user_id: int,
    telegram_username: str | None,
    telegram_full_name: str,
    now: str,
    create: bool,
) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if create:
            await db.execute(
                """
                INSERT INTO profiles (
                    user_id, telegram_username, telegram_full_name,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    telegram_username=excluded.telegram_username,
                    telegram_full_name=excluded.telegram_full_name,
                    first_seen_at=COALESCE(profiles.first_seen_at, excluded.first_seen_at),
                    last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at
                """,
                (
                    user_id,
                    telegram_username,
                    telegram_full_name,
                    now,
                    now,
                    now,
                    now,
                ),
            )
        else:
            await db.execute(
                """
                UPDATE profiles SET
                    telegram_username=?,
                    telegram_full_name=?,
                    first_seen_at=COALESCE(first_seen_at, ?),
                    last_seen_at=?,
                    updated_at=?
                WHERE user_id=?
                """,
                (telegram_username, telegram_full_name, now, now, now, user_id),
            )
        await db.commit()
        return await _fetch_profile(db, user_id)


async def update_nickname(user_id: int, nickname: str, now: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE profiles
            SET game_nickname=?, nickname_updated_at=?, updated_at=?
            WHERE user_id=? AND COALESCE(game_nickname, '') != ?
            """,
            (nickname, now, now, user_id, nickname),
        )
        await db.commit()


async def update_uid(user_id: int, uid: str, now: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """
                UPDATE profiles
                SET codm_uid=?, uid_edit_count=uid_edit_count + 1, updated_at=?
                WHERE user_id=? AND COALESCE(codm_uid, '') != ?
                """,
                (uid, now, user_id, uid),
            )
            await db.commit()
        except sqlite3.IntegrityError as exc:
            raise DuplicateUIDError from exc


async def update_birthday(user_id: int, birthday: str, now: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE profiles
            SET birthday=?, birthday_edit_count=birthday_edit_count + 1, updated_at=?
            WHERE user_id=? AND COALESCE(birthday, '') != ?
            """,
            (birthday, now, user_id, birthday),
        )
        await db.commit()
