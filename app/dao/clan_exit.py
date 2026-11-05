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
            telegram_username TEXT,
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
            deferred_by INTEGER,
            deferred_until INTEGER,
            reminder_count INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER,
            closed_by INTEGER,
            closed_at INTEGER,
            updated_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_open_clan_exit_user
            ON clan_exit_checks(user_id) WHERE status='open';
        CREATE TABLE IF NOT EXISTS clan_exit_actor_names (
            user_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clan_exit_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_id INTEGER NOT NULL REFERENCES clan_exit_checks(id),
            actor_id INTEGER,
            action TEXT NOT NULL,
            detail TEXT,
            created_at INTEGER NOT NULL
        );
    """)
    columns = {row[1] for row in await (await db.execute("PRAGMA table_info(clan_exit_checks)")).fetchall()}
    for name, definition in {
        "telegram_username": "TEXT",
        "deferred_by": "INTEGER",
        "deferred_until": "INTEGER",
        "reminder_count": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE clan_exit_checks ADD COLUMN {name} {definition}")


async def remember_actor(user) -> None:
    """Persist reviewer name even if they lack a player profile."""
    if not user:
        return
    label = (getattr(user, "full_name", None)
             or getattr(user, "username", None)
             or getattr(user, "first_name", None))
    if not label:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO clan_exit_actor_names(user_id, display_name, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "display_name=excluded.display_name, updated_at=excluded.updated_at",
            (int(user.id), str(label), int(time.time())),
        )
        await db.commit()


async def actor_labels(user_ids: set[int]) -> dict[int, str]:
    """Resolve reviewer names from local Telegram snapshots (no Bot API wait)."""
    if not user_ids:
        return {}
    labels = {}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for user_id in user_ids:
            row = await (await db.execute(
                """SELECT p.telegram_full_name, p.telegram_username,
                          a.first_name AS admin_first, a.last_name AS admin_last,
                          a.username AS admin_username,
                          c.first_name AS call_first, c.last_name AS call_last,
                          c.username AS call_username,
                          n.display_name AS recorded_name
                   FROM (SELECT ? AS user_id) v
                   LEFT JOIN profiles p ON p.user_id=v.user_id
                   LEFT JOIN admins a ON a.user_id=v.user_id
                   LEFT JOIN call_members c ON c.user_id=v.user_id
                   LEFT JOIN clan_exit_actor_names n ON n.user_id=v.user_id""",
                (user_id,),
            )).fetchone()
            if not row:
                continue
            name = (
                row["recorded_name"]
                or row["telegram_full_name"]
                or " ".join(x for x in (row["admin_first"], row["admin_last"]) if x)
                or " ".join(x for x in (row["call_first"], row["call_last"]) if x)
                or (f"@{row['telegram_username']}" if row["telegram_username"] else "")
                or (f"@{row['admin_username']}" if row["admin_username"] else "")
                or (f"@{row['call_username']}" if row["call_username"] else "")
            )
            if name:
                labels[user_id] = name
    return labels


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
                 join_date: str | None, telegram_username: str | None = None) -> tuple[dict, bool]:
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
                    (user_id, departed_at, departure_kind, nickname, telegram_name,
                     telegram_username, join_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, now, kind, nickname, name, telegram_username, join_date, now))
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
        "contact_at", "deferred_by", "deferred_until", "reminder_count",
        "message_id", "closed_by", "closed_at",
    }
    if not values or set(values) - allowed:
        raise ValueError("Unsupported exit case update")
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        guards = {
            "game_confirmed": " AND game_at IS NULL",
            "contact_confirmed": " AND explanation_status='not_requested'",
            "explanation_unavailable": " AND explanation_status='requested' AND contact_at IS NOT NULL",
            "explanation_recorded": " AND explanation_status IN ('not_requested', 'requested')",
        }
        cursor = await db.execute(
            "UPDATE clan_exit_checks SET " + ", ".join(f"{key}=?" for key in values)
            + ", updated_at=? WHERE id=? AND status='open'" + guards.get(action, ""),
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


async def list_open(limit: int = 30) -> list[dict]:
    """Open departure cases ordered by oldest departure."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM clan_exit_checks WHERE status='open' ORDER BY departed_at LIMIT ?",
            (limit,),
        )).fetchall()
        return [dict(row) for row in rows]


async def action_history(check_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT actor_id, action, created_at FROM clan_exit_actions "
            "WHERE check_id=? ORDER BY id DESC LIMIT ?", (check_id, limit),
        )).fetchall()
        return [dict(row) for row in rows]
