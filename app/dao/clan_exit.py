"""Durable exit cases and action history."""

from __future__ import annotations

import json
import time

import aiosqlite

from app.core.db import DB_PATH


async def ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS clan_exit_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            departed_at INTEGER NOT NULL,
            departure_kind TEXT NOT NULL,
            nickname TEXT,
            telegram_name TEXT NOT NULL,
            join_date TEXT,
            chat_states TEXT NOT NULL DEFAULT '{}',
            game_by INTEGER,
            game_at INTEGER,
            explanation_status TEXT NOT NULL DEFAULT 'not_requested',
            explanation_text TEXT,
            explanation_by INTEGER,
            explanation_at INTEGER,
            contact_by INTEGER,
            contact_at INTEGER,
            message_id INTEGER,
            closed_by INTEGER,
            closed_at INTEGER,
            updated_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_open_clan_exit_user
            ON clan_exit_checks(user_id) WHERE status='open';
        CREATE TABLE IF NOT EXISTS clan_exit_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_id INTEGER NOT NULL REFERENCES clan_exit_checks(id),
            actor_id INTEGER,
            action TEXT NOT NULL,
            detail TEXT,
            created_at INTEGER NOT NULL
        );
    """)


async def get(check_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM clan_exit_checks WHERE id=?", (check_id,))).fetchone()
        return dict(row) if row else None


async def get_open_for_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM clan_exit_checks WHERE user_id=? AND status='open'", (user_id,)
        )).fetchone()
        return dict(row) if row else None


async def create(user_id: int, kind: str, nickname: str | None, name: str,
                 join_date: str | None) -> tuple[dict, bool]:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT id FROM clan_exit_checks WHERE user_id=? AND status='open'", (user_id,)
        )
        existing = await cursor.fetchone()
        if existing:
            check_id, created = existing[0], False
        else:
            cursor = await db.execute("""
                INSERT INTO clan_exit_checks
                    (user_id, departed_at, departure_kind, nickname, telegram_name, join_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, now, kind, nickname, name, join_date, now))
            check_id, created = cursor.lastrowid, True
            await db.execute("""
                INSERT INTO clan_exit_actions(check_id, action, detail, created_at)
                VALUES (?, 'detected', ?, ?)
            """, (check_id, kind, now))
        await db.commit()
    case = await get(check_id)
    assert case is not None
    return case, created


async def change(check_id: int, actor_id: int | None, action: str, **values) -> bool:
    allowed = {
        "status", "chat_states", "game_by", "game_at", "explanation_status",
        "explanation_text", "explanation_by", "explanation_at", "contact_by",
        "contact_at", "message_id", "closed_by", "closed_at",
    }
    if not values or set(values) - allowed:
        raise ValueError("Unsupported exit case update")
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "UPDATE clan_exit_checks SET " + ", ".join(f"{key}=?" for key in values)
            + ", updated_at=? WHERE id=? AND status='open'",
            (*values.values(), now, check_id),
        )
        if cursor.rowcount:
            await db.execute("""
                INSERT INTO clan_exit_actions(check_id, actor_id, action, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (check_id, actor_id, action, json.dumps(values, ensure_ascii=False), now))
        await db.commit()
        return bool(cursor.rowcount)


async def attach_message(check_id: int, message_id: int) -> None:
    """A notification may be published after a concurrent return/closure."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE clan_exit_checks SET message_id=? WHERE id=? AND message_id IS NULL",
            (message_id, check_id),
        )
        await db.commit()


async def open_cases() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM clan_exit_checks WHERE status='open' ORDER BY departed_at"
        )).fetchall()
        return [dict(row) for row in rows]
