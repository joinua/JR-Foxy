import aiosqlite
from pathlib import Path

DB_PATH = Path("data") / "jrfoxy.db"

async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS call_members (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            last_name   TEXT,
            is_enabled  INTEGER NOT NULL DEFAULT 1,
            last_seen   INTEGER NOT NULL
        )
        """)
        await db.commit()

async def upsert_call_member(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    last_seen: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO call_members (user_id, username, first_name, last_name, is_enabled, last_seen)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=excluded.last_seen
        """, (user_id, username, first_name, last_name, last_seen))
        await db.commit()

async def get_call_members(limit: int | None = None):
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
        rows = await cursor.fetchall()
        return rows
