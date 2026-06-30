"""SQLite access helpers for player profiles."""

from __future__ import annotations

import sqlite3

import aiosqlite

from app.core.db import DB_PATH


class DuplicateUIDError(Exception):
    """The requested CODM UID is already assigned."""


async def _ensure_profile_audit_ignore_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS profile_audit_ignored (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            raw_identifier TEXT NOT NULL,
            ignored_by INTEGER NOT NULL,
            ignored_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_audit_ignored_user_id
            ON profile_audit_ignored (user_id)
            WHERE user_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_audit_ignored_username
            ON profile_audit_ignored (username COLLATE NOCASE)
            WHERE username IS NOT NULL;
        """
    )


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


async def archive_profile(user_id: int, now: str) -> None:
    """Архівує профіль учасника, який вийшов із головного чату."""

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO profiles (
                user_id,
                status,
                first_seen_at,
                last_seen_at,
                created_at,
                updated_at,
                archived_at
            ) VALUES (?, 'archived', ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                status='archived',
                archived_at=excluded.archived_at,
                updated_at=excluded.updated_at
            """,
            (user_id, now, now, now, now, now),
        )
        await db.commit()


async def reactivate_profile(user_id: int, now: str) -> None:
    """Повертає профіль до активного стану після повторного вступу."""

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE profiles
            SET status='active', archived_at=NULL, deleted_at=NULL, updated_at=?
            WHERE user_id=?
            """,
            (now, user_id),
        )
        await db.commit()


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


async def ignore_profile_audit_identifier(
    identifier: str,
    ignored_by: int,
    ignored_at: int,
) -> dict:
    normalized = identifier.strip().lstrip("@")
    user_id: int | None = int(normalized) if normalized.isdigit() else None
    username: str | None = None if normalized.isdigit() else normalized

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_profile_audit_ignore_schema(db)

        if user_id is None and username:
            cursor = await db.execute(
                """
                SELECT user_id
                FROM (
                    SELECT user_id, last_seen AS sort_value
                    FROM call_members
                    WHERE username = ? COLLATE NOCASE
                    UNION ALL
                    SELECT user_id, 0 AS sort_value
                    FROM profiles
                    WHERE telegram_username = ? COLLATE NOCASE
                )
                ORDER BY sort_value DESC
                LIMIT 1
                """,
                (username, username),
            )
            row = await cursor.fetchone()
            if row and row["user_id"] is not None:
                user_id = int(row["user_id"])

        if user_id is not None:
            await db.execute(
                """
                DELETE FROM profile_audit_ignored
                WHERE user_id = ?
                   OR (? IS NOT NULL AND username = ? COLLATE NOCASE)
                """,
                (user_id, username, username),
            )
            await db.execute(
                """
                INSERT INTO profile_audit_ignored (
                    user_id, username, raw_identifier, ignored_by, ignored_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, username, identifier.strip(), ignored_by, ignored_at),
            )
        elif username:
            await db.execute(
                """
                INSERT INTO profile_audit_ignored (
                    user_id, username, raw_identifier, ignored_by, ignored_at
                ) VALUES (NULL, ?, ?, ?, ?)
                ON CONFLICT(username) WHERE username IS NOT NULL DO UPDATE SET
                    raw_identifier=excluded.raw_identifier,
                    ignored_by=excluded.ignored_by,
                    ignored_at=excluded.ignored_at
                """,
                (username, identifier.strip(), ignored_by, ignored_at),
            )
        await db.commit()
        return {"user_id": user_id, "username": username, "raw_identifier": identifier.strip()}


async def list_profiles_for_audit() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_profile_audit_ignore_schema(db)
        cursor = await db.execute(
            """
            WITH known_users AS (
                SELECT user_id FROM clan_members
                UNION
                SELECT user_id
                FROM profiles
                WHERE COALESCE(status, 'active') = 'active'
                  AND archived_at IS NULL
                  AND deleted_at IS NULL
                UNION
                SELECT user_id
                FROM call_members
                WHERE is_enabled = 1
            )
            SELECT
                ku.user_id,
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
            FROM known_users ku
            LEFT JOIN profiles p ON p.user_id=ku.user_id
            LEFT JOIN clan_members cm ON cm.user_id=ku.user_id
            LEFT JOIN call_members c ON c.user_id=ku.user_id
            LEFT JOIN profile_audit_ignored ignored ON
                   (ignored.user_id IS NOT NULL AND ignored.user_id=ku.user_id)
                OR (ignored.username IS NOT NULL AND c.username = ignored.username COLLATE NOCASE)
                OR (ignored.username IS NOT NULL AND p.telegram_username = ignored.username COLLATE NOCASE)
            WHERE ignored.id IS NULL
              AND (
                    p.user_id IS NULL
                    OR (
                        COALESCE(p.status, 'active') = 'active'
                        AND p.archived_at IS NULL
                        AND p.deleted_at IS NULL
                    )
              )
            ORDER BY COALESCE(p.game_nickname, p.telegram_username, c.username, p.telegram_full_name, c.first_name, ku.user_id) COLLATE NOCASE
            """
        )
        return [dict(row) for row in await cursor.fetchall()]
