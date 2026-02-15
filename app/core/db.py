"""Допоміжні функції для роботи з базою даних бота."""

from pathlib import Path
import time

import aiosqlite

DB_PATH = Path("data") / "jrfoxy.db"


async def _ensure_warnings_schema(db: aiosqlite.Connection) -> None:
    """Гарантує актуальну схему таблиці `warnings` без втрати історії.

    Підтримуємо міграції через `ALTER TABLE`, бо попередження є аудит-логом
    і видаляти/пересоздавати таблицю не можна.

    Параметри:
        db: Відкрите з'єднання з SQLite.
    """

    cursor = await db.execute("PRAGMA table_info(warnings)")
    columns = {row[1] for row in await cursor.fetchall()}

    if "user_username_snapshot" not in columns:
        await db.execute(
            "ALTER TABLE warnings ADD COLUMN user_username_snapshot TEXT"
        )

    if "admin_username_snapshot" not in columns:
        await db.execute(
            "ALTER TABLE warnings ADD COLUMN admin_username_snapshot TEXT"
        )

    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_warnings_active_lookup
        ON warnings (user_id, is_revoked, expires_at)
        """
    )


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
                user_username_snapshot TEXT,
                admin_username_snapshot TEXT,
                is_revoked INTEGER NOT NULL DEFAULT 0,
                revoked_at INTEGER,
                revoked_by INTEGER
            );
            CREATE TABLE IF NOT EXISTS candidates (
                user_id INTEGER NOT NULL,
                reception_chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'candidate',
                created_at INTEGER NOT NULL,
                review_due_at INTEGER NOT NULL,
                wait_count INTEGER NOT NULL DEFAULT 0,
                last_buttons_msg_id INTEGER,
                reviewed_by INTEGER,
                reviewed_at INTEGER,
                invite_link TEXT,
                UNIQUE(user_id, reception_chat_id)
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_status_due
                ON candidates (status, review_due_at);
            CREATE INDEX IF NOT EXISTS idx_candidates_user_id
                ON candidates (user_id);
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                run_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                chat_id INTEGER,
                user_id INTEGER,
                payload_json TEXT,
                tries INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status_run_at
                ON scheduled_tasks (status, run_at);
            """
        )
        await _ensure_warnings_schema(db)
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

async def find_user_id_by_username_snapshot(username: str) -> int | None:
    """Шукає user_id за збереженим username у таблиці warnings.

    Працює для випадку, коли Telegram API не може резолвнути @username напряму.
    Повертає останній відомий user_id для цього username, якщо він є в історії варнів.
    """
    username = username.lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_warnings_schema(db)
        cur = await db.execute(
            """
            SELECT user_id
            FROM warnings
            WHERE user_username_snapshot = ? COLLATE NOCASE
            ORDER BY issued_at DESC
            LIMIT 1
            """,
            (username,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else None

async def find_user_by_username(
    username: str,
) -> tuple[
    int,
    str | None,
    str | None,
    str | None,
] | None:
    """Шукає користувача по username в локальній БД.

    Джерела (в пріоритеті):
    1) call_members (бот "чув" в чаті)
    2) admins (якщо ведете профілі адмінів)
    3) warnings.user_username_snapshot (якщо вже були варни)

    Повертає:
        (user_id, first_name, last_name, username) або None
    """

    username = username.lstrip("@")
    if not username:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_warnings_schema(db)

        # 1) call_members — основне джерело
        cur = await db.execute(
            """
            SELECT user_id, first_name, last_name, username
            FROM call_members
            WHERE username = ? COLLATE NOCASE
            ORDER BY last_seen DESC
            LIMIT 1
            """,
            (username,),
        )
        row = await cur.fetchone()
        if row:
            return int(row[0]), row[1], row[2], row[3]

        # 2) admins — якщо username є в таблиці адмінів
        cur = await db.execute(
            """
            SELECT user_id, first_name, last_name, username
            FROM admins
            WHERE username = ? COLLATE NOCASE
            LIMIT 1
            """,
            (username,),
        )
        row = await cur.fetchone()
        if row:
            return int(row[0]), row[1], row[2], row[3]

        # 3) warnings snapshot — якщо були варни раніше
        cur = await db.execute(
            """
            SELECT user_id, NULL, NULL, user_username_snapshot
            FROM warnings
            WHERE user_username_snapshot = ? COLLATE NOCASE
            ORDER BY issued_at DESC
            LIMIT 1
            """,
            (username,),
        )
        row = await cur.fetchone()
        if row:
            return int(row[0]), None, None, row[3]

        return None


def _candidate_from_row(row: tuple) -> dict | None:
    if row is None:
        return None
    return {
        "user_id": int(row[0]),
        "reception_chat_id": int(row[1]),
        "status": str(row[2]),
        "created_at": int(row[3]),
        "review_due_at": int(row[4]),
        "wait_count": int(row[5]),
        "last_buttons_msg_id": row[6],
        "reviewed_by": row[7],
        "reviewed_at": row[8],
        "invite_link": row[9],
    }


async def upsert_candidate_on_join(
    user_id: int,
    reception_chat_id: int,
    review_due_at: int,
) -> None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO candidates (
                user_id,
                reception_chat_id,
                status,
                created_at,
                review_due_at,
                wait_count,
                last_buttons_msg_id,
                reviewed_by,
                reviewed_at,
                invite_link
            )
            VALUES (?, ?, 'candidate', ?, ?, 0, NULL, NULL, NULL, NULL)
            ON CONFLICT(user_id, reception_chat_id) DO UPDATE SET
                status='candidate',
                review_due_at=excluded.review_due_at,
                last_buttons_msg_id=NULL,
                reviewed_by=NULL,
                reviewed_at=NULL,
                invite_link=NULL
            """,
            (user_id, reception_chat_id, now, review_due_at),
        )
        await db.commit()


async def get_candidate(user_id: int, reception_chat_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT
                user_id,
                reception_chat_id,
                status,
                created_at,
                review_due_at,
                wait_count,
                last_buttons_msg_id,
                reviewed_by,
                reviewed_at,
                invite_link
            FROM candidates
            WHERE user_id=? AND reception_chat_id=?
            """,
            (user_id, reception_chat_id),
        )
        row = await cur.fetchone()
        return _candidate_from_row(row)


async def get_candidate_in_any_chat(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT
                user_id,
                reception_chat_id,
                status,
                created_at,
                review_due_at,
                wait_count,
                last_buttons_msg_id,
                reviewed_by,
                reviewed_at,
                invite_link
            FROM candidates
            WHERE user_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cur.fetchone()
        return _candidate_from_row(row)


async def update_candidate_status(
    user_id: int,
    reception_chat_id: int,
    status: str,
    reviewed_by: int | None = None,
    reviewed_at: int | None = None,
    invite_link: str | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE candidates
            SET status=?,
                reviewed_by=COALESCE(?, reviewed_by),
                reviewed_at=COALESCE(?, reviewed_at),
                invite_link=COALESCE(?, invite_link)
            WHERE user_id=? AND reception_chat_id=?
            """,
            (status, reviewed_by, reviewed_at, invite_link, user_id, reception_chat_id),
        )
        await db.commit()


async def postpone_candidate_review(
    user_id: int,
    reception_chat_id: int,
    review_due_at: int,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE candidates
            SET review_due_at=?, wait_count=wait_count+1
            WHERE user_id=? AND reception_chat_id=?
            """,
            (review_due_at, user_id, reception_chat_id),
        )
        await db.commit()


async def set_candidate_buttons_message(
    user_id: int,
    reception_chat_id: int,
    message_id: int | None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE candidates
            SET last_buttons_msg_id=?
            WHERE user_id=? AND reception_chat_id=?
            """,
            (message_id, user_id, reception_chat_id),
        )
        await db.commit()


async def schedule_task(
    task_type: str,
    run_at: int,
    chat_id: int | None = None,
    user_id: int | None = None,
    payload_json: str | None = None,
) -> int:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO scheduled_tasks (
                task_type,
                run_at,
                status,
                chat_id,
                user_id,
                payload_json,
                tries,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'pending', ?, ?, ?, 0, ?, ?)
            """,
            (task_type, run_at, chat_id, user_id, payload_json, now, now),
        )
        await db.commit()
        return int(cur.lastrowid)


async def cancel_pending_tasks(
    task_type: str,
    chat_id: int | None = None,
    user_id: int | None = None,
) -> int:
    conditions = ["task_type=?", "status='pending'"]
    params: list[int | str] = [task_type]
    if chat_id is not None:
        conditions.append("chat_id=?")
        params.append(chat_id)
    if user_id is not None:
        conditions.append("user_id=?")
        params.append(user_id)

    now = int(time.time())
    query = f"""
        UPDATE scheduled_tasks
        SET status='cancelled', updated_at=?
        WHERE {' AND '.join(conditions)}
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, (now, *params))
        await db.commit()
        return cur.rowcount


async def fetch_due_tasks(limit: int = 20) -> list[dict]:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, task_type, run_at, status, chat_id, user_id, payload_json, tries
            FROM scheduled_tasks
            WHERE status='pending' AND run_at<=?
            ORDER BY run_at ASC, id ASC
            LIMIT ?
            """,
            (now, limit),
        )
        rows = await cur.fetchall()

    return [
        {
            "id": int(row[0]),
            "task_type": str(row[1]),
            "run_at": int(row[2]),
            "status": str(row[3]),
            "chat_id": row[4],
            "user_id": row[5],
            "payload_json": row[6],
            "tries": int(row[7]),
        }
        for row in rows
    ]


async def mark_task_running(task_id: int) -> bool:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE scheduled_tasks
            SET status='running', tries=tries+1, updated_at=?
            WHERE id=? AND status='pending'
            """,
            (now, task_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def mark_task_done(task_id: int) -> None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE scheduled_tasks SET status='done', updated_at=? WHERE id=?",
            (now, task_id),
        )
        await db.commit()


async def mark_task_failed(task_id: int, error_text: str) -> None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE scheduled_tasks
            SET status='failed', last_error=?, updated_at=?
            WHERE id=?
            """,
            (error_text[:1000], now, task_id),
        )
        await db.commit()
