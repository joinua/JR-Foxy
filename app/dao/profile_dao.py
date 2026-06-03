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


async def update_role(user_id: int, role: str, now: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE profiles
            SET role=?, updated_at=?
            WHERE user_id=?
            """,
            (role, now, user_id),
        )
        await db.commit()


async def update_join_date(
    user_id: int,
    join_date: str,
    source: str,
    now: str,
    *,
    only_if_empty: bool = False,
) -> bool:
    conditions = "user_id=?"
    if only_if_empty:
        conditions += " AND (join_date IS NULL OR join_date = '')"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"""
            UPDATE profiles
            SET join_date=?, join_date_source=?, updated_at=?
            WHERE {conditions}
            """,
            (join_date, source, now, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_join_date_fallback_candidate(user_id: int) -> tuple[str, int] | None:
    """Return (source, unix timestamp) for the best known join-date fallback."""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT reviewed_at
            FROM candidates
            WHERE user_id=?
              AND status='accepted'
              AND reviewed_at IS NOT NULL
            ORDER BY reviewed_at ASC
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if row and row[0] is not None:
            return "invite", int(row[0])

        cursor = await db.execute(
            """
            SELECT first_joined_at
            FROM clan_members
            WHERE user_id=?
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if row and row[0] is not None:
            return "first_message", int(row[0])

        return None


async def list_profiles_for_audit() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                COALESCE(p.user_id, cm.user_id) AS user_id,
                p.telegram_username,
                p.telegram_full_name,
                p.game_nickname,
                p.codm_uid,
                p.birthday,
                p.join_date,
                p.role,
                cm.first_joined_at,
                c.username AS call_username,
                c.first_name AS call_first_name,
                c.last_name AS call_last_name
            FROM clan_members cm
            LEFT JOIN profiles p ON p.user_id=cm.user_id
            LEFT JOIN call_members c ON c.user_id=cm.user_id
            WHERE p.user_id IS NULL
               OR (
                    COALESCE(p.status, 'active') = 'active'
                    AND p.archived_at IS NULL
                    AND p.deleted_at IS NULL
               )
            ORDER BY COALESCE(p.game_nickname, p.telegram_username, c.username, p.telegram_full_name, c.first_name, cm.user_id) COLLATE NOCASE
            """
        )
        rows = await cursor.fetchall()
        if rows:
            return [dict(row) for row in rows]

        cursor = await db.execute(
            """
            SELECT
                p.user_id,
                p.telegram_username,
                p.telegram_full_name,
                p.game_nickname,
                p.codm_uid,
                p.birthday,
                p.join_date,
                p.role,
                NULL AS first_joined_at,
                c.username AS call_username,
                c.first_name AS call_first_name,
                c.last_name AS call_last_name
            FROM profiles p
            LEFT JOIN call_members c ON c.user_id=p.user_id
            WHERE COALESCE(p.status, 'active') = 'active'
              AND p.archived_at IS NULL
              AND p.deleted_at IS NULL
            ORDER BY COALESCE(p.game_nickname, p.telegram_username, c.username, p.telegram_full_name, c.first_name, p.user_id) COLLATE NOCASE
            """
        )
        return [dict(row) for row in await cursor.fetchall()]
