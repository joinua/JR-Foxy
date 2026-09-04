import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiogram.types import CallbackQuery

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.core import db
from app.core import access
from app.core.dates import (
    KYIV_TZ,
    combine_kyiv_datetime,
    format_ua_datetime,
    localize_kyiv_datetime,
    parse_user_time,
    to_utc_timestamp,
)
from app.middlewares.chat_guard import ChatGuardMiddleware


class EventDateTests(unittest.TestCase):
    def test_event_datetime_is_aware_and_formats_in_ukrainian(self):
        value = combine_kyiv_datetime(date(2026, 10, 5), parse_user_time("21:00"))

        self.assertEqual(value.tzinfo, KYIV_TZ)
        self.assertEqual(
            format_ua_datetime(value),
            "5 жовтня 2026 року о 21:00 (понеділок)",
        )
        self.assertEqual(
            datetime.fromtimestamp(to_utc_timestamp(value), timezone.utc),
            datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc),
        )

    def test_time_input_is_strict(self):
        self.assertEqual(parse_user_time("09:05").isoformat(), "09:05:00")
        for invalid in ("9:05", "24:00", "09:5", "text"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_user_time(invalid)

    def test_dst_gap_and_ambiguous_wall_time_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            localize_kyiv_datetime(datetime(2024, 3, 31, 3, 30))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            localize_kyiv_datetime(datetime(2024, 10, 27, 3, 30))


class EventSchemaAndSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        await db.init_db()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def _insert_event(self, starts_at: int, status: str = "published") -> int:
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                """
                INSERT INTO events (
                    title, event_type, starts_at_utc, safe_until_utc,
                    registration_closes_at_utc, status,
                    created_by, created_at, updated_at
                ) VALUES (?, 'clan', ?, ?, ?, ?, 1, 1, 1)
                """,
                ("Тестова подія", starts_at, starts_at - 7200, starts_at - 3600, status),
            )
            await connection.commit()
            return int(cursor.lastrowid)

    async def test_event_schema_is_idempotent_and_complete(self):
        await db.init_db()
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'event%'"
            )
            tables = {row[0] for row in await cursor.fetchall()}

        self.assertEqual(
            tables,
            {
                "events",
                "event_publications",
                "event_responses",
                "event_late_confirmations",
                "event_results",
                "event_drafts",
                "event_notifications",
                "event_audit_log",
            },
        )

    async def test_active_events_cannot_share_start_minute(self):
        first_id = await self._insert_event(2_000_000_000)
        with self.assertRaises(sqlite3.IntegrityError):
            await self._insert_event(2_000_000_000, "awaiting_review")

        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                "UPDATE events SET status='cancelled' WHERE id=?",
                (first_id,),
            )
            await connection.commit()

        second_id = await self._insert_event(2_000_000_000)
        self.assertNotEqual(first_id, second_id)

    async def test_task_dedupe_returns_the_existing_task(self):
        first, second = await asyncio.gather(
            db.schedule_task("event_start", 100, dedupe_key="event:1:v1:start"),
            db.schedule_task("event_start", 100, dedupe_key="event:1:v1:start"),
        )
        self.assertEqual(first, second)

        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM scheduled_tasks WHERE dedupe_key=?",
                ("event:1:v1:start",),
            )
            self.assertEqual((await cursor.fetchone())[0], 1)

    async def test_scheduler_migration_preserves_legacy_tasks(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        async with aiosqlite.connect(legacy_path) as connection:
            await connection.executescript(
                """
                CREATE TABLE scheduled_tasks (
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
                INSERT INTO scheduled_tasks (
                    task_type, run_at, status, payload_json,
                    tries, created_at, updated_at
                ) VALUES ('legacy_task', 123, 'pending', '{}', 2, 100, 101);
                """
            )
            await connection.commit()

        with patch.object(db, "DB_PATH", legacy_path):
            await db.init_db()

        async with aiosqlite.connect(legacy_path) as connection:
            cursor = await connection.execute(
                """
                SELECT task_type, run_at, status, payload_json, tries,
                       dedupe_key, locked_at, max_attempts
                FROM scheduled_tasks
                """
            )
            self.assertEqual(
                await cursor.fetchone(),
                ("legacy_task", 123, "pending", "{}", 2, None, None, 4),
            )

    async def test_stale_running_task_is_recovered_or_failed(self):
        recovered_id = await db.schedule_task(
            "event_start",
            100,
            dedupe_key="event:2:v1:start",
        )
        self.assertTrue(await db.mark_task_running(recovered_id))

        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                "UPDATE scheduled_tasks SET locked_at=100 WHERE id=?",
                (recovered_id,),
            )
            await connection.commit()

        result = await db.recover_stale_running_tasks(300, now=1_000)
        self.assertEqual(result, {"recovered": 1, "failed": 0})

        self.assertTrue(await db.mark_task_running(recovered_id))
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                """
                UPDATE scheduled_tasks
                SET locked_at=100, tries=3, max_attempts=4
                WHERE id=?
                """,
                (recovered_id,),
            )
            await connection.commit()

        result = await db.recover_stale_running_tasks(300, now=1_000)
        self.assertEqual(result, {"recovered": 0, "failed": 1})
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                "SELECT status, tries, locked_at FROM scheduled_tasks WHERE id=?",
                (recovered_id,),
            )
            self.assertEqual(await cursor.fetchone(), ("failed", 4, None))


class EventAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_is_always_effective_level_four(self):
        with patch.object(access, "get_admin_level", AsyncMock(return_value=0)):
            self.assertEqual(await access.get_effective_admin_level(1), 4)

    async def test_event_management_requires_level_and_admin_chat(self):
        with patch.object(access, "get_admin_level", AsyncMock(return_value=3)):
            self.assertTrue(await access.can_manage_events(10, -200))
            self.assertFalse(await access.can_manage_events(10, -999))

        with patch.object(access, "get_admin_level", AsyncMock(return_value=2)):
            self.assertFalse(await access.can_manage_events(10, -200))


class ChatGuardCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_from_allowed_chat_reaches_handler(self):
        middleware = ChatGuardMiddleware()
        handler = AsyncMock(return_value="handled")
        allowed_chat_id = next(iter(access.ADMIN_SAFE_CHAT_IDS))
        event = AsyncMock(spec=CallbackQuery)
        event.message = SimpleNamespace(
            chat=SimpleNamespace(id=allowed_chat_id, type="supergroup")
        )
        bot = SimpleNamespace(leave_chat=AsyncMock())

        result = await middleware(handler, event, {"bot": bot})

        self.assertEqual(result, "handled")
        handler.assert_awaited_once_with(event, {"bot": bot})
        bot.leave_chat.assert_not_awaited()

    async def test_callback_chat_is_checked_and_disallowed_chat_is_left(self):
        middleware = ChatGuardMiddleware()
        handler = AsyncMock()
        event = AsyncMock(spec=CallbackQuery)
        event.message = SimpleNamespace(
            chat=SimpleNamespace(id=-999, type="supergroup")
        )
        event.answer = AsyncMock()
        bot = SimpleNamespace(leave_chat=AsyncMock())

        await middleware(handler, event, {"bot": bot})

        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
        bot.leave_chat.assert_awaited_once_with(-999)
