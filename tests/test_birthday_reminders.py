import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.core import db
from app.handlers.profile import profile_admin
from app.services import birthday_reminders


def profile_data(
    user_id: int = 10,
    *,
    birthday: str = "2001-09-03",
    nickname: str = "JRঐ<Fox>",
) -> dict:
    return {
        "user_id": user_id,
        "telegram_username": "fox_user",
        "telegram_full_name": "Fox <Admin>",
        "game_nickname": nickname,
        "birthday": birthday,
        "join_date": "2024-01-15",
    }


class BirthdayRenderingTests(unittest.TestCase):
    def test_next_run_is_0800_in_kyiv_from_utc_input(self):
        now = datetime(2026, 9, 2, 6, 30, tzinfo=timezone.utc)
        timestamp = birthday_reminders._next_0800_timestamp(now)
        scheduled = datetime.fromtimestamp(
            timestamp,
            tz=birthday_reminders.KYIV_TZ,
        )
        self.assertEqual(
            scheduled,
            datetime(2026, 9, 3, 8, 0, tzinfo=birthday_reminders.KYIV_TZ),
        )

    def test_day_of_message_uses_clickable_name_nickname_and_ukrainian_age(self):
        text = birthday_reminders.render_birthday_message(
            profile_data(),
            date(2026, 9, 3),
        )
        self.assertIn('href="tg://user?id=10"', text)
        self.assertIn("Fox &lt;Admin&gt;", text)
        self.assertIn("<code>JRঐ&lt;Fox&gt;</code>", text)
        self.assertIn("Святкує: 25 років", text)

    def test_pre_message_is_neutral_and_adds_clickable_responsible_admin(self):
        text = birthday_reminders.render_birthday_pre_message(
            profile_data(),
            date(2026, 9, 3),
            responsible_user_id=20,
            responsible_name="Officer <One>",
        )
        self.assertIn("ЗАВТРА 🎉", text)
        self.assertIn("свої 25 років", text)
        self.assertIn("Ігровий нік: <code>JRঐ&lt;Fox&gt;</code>", text)
        self.assertIn("З нами вже:", text)
        self.assertNotIn("Його", text)
        self.assertNotIn("її", text)
        self.assertIn("Відповідальний за привітання", text)
        self.assertIn('href="tg://user?id=20"', text)
        self.assertIn("Officer &lt;One&gt;", text)


class BirthdayDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.service_db_patch = patch.object(
            birthday_reminders,
            "DB_PATH",
            self.db_path,
        )
        self.db_patch.start()
        self.service_db_patch.start()
        await db.init_db()

    async def asyncTearDown(self):
        self.service_db_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def test_schema_migrates_existing_birthday_table(self):
        old_path = Path(self.temp_dir.name) / "old.db"
        async with aiosqlite.connect(old_path) as connection:
            await connection.executescript(
                """
                CREATE TABLE birthday_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    birthday_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    remind_at INTEGER,
                    created_at INTEGER NOT NULL,
                    UNIQUE(user_id, birthday_date)
                );
                INSERT INTO birthday_notifications (
                    user_id, birthday_date, status, created_at
                ) VALUES (10, '2026-09-03', 'pending', 1);
                """
            )
            await db._ensure_birthday_schema(connection)
            await connection.commit()
            cursor = await connection.execute(
                "PRAGMA table_info(birthday_notifications)"
            )
            columns = {row[1] for row in await cursor.fetchall()}
            self.assertIn("message_id", columns)
            self.assertIn("sent_at", columns)
            cursor = await connection.execute(
                "SELECT user_id, birthday_date FROM birthday_notifications"
            )
            self.assertEqual(await cursor.fetchone(), (10, "2026-09-03"))
            cursor = await connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='birthday_pre_notifications'"
            )
            self.assertIsNotNone(await cursor.fetchone())

    async def test_startup_after_0800_schedules_an_immediate_catch_up(self):
        fixed_now = datetime(
            2026,
            9,
            2,
            12,
            0,
            tzinfo=birthday_reminders.KYIV_TZ,
        )
        with (
            patch.object(
                birthday_reminders,
                "_kyiv_datetime",
                return_value=fixed_now,
            ),
            patch.object(birthday_reminders.time, "time", return_value=1_000),
            patch.object(
                birthday_reminders,
                "cancel_pending_tasks",
                AsyncMock(),
            ),
            patch.object(
                birthday_reminders,
                "schedule_task",
                AsyncMock(),
            ) as schedule,
        ):
            await birthday_reminders.register_birthday_daily_task(
                catch_up_today=True
            )

        schedule.assert_awaited_once_with(
            birthday_reminders.BIRTHDAY_DAILY_TASK,
            1_001,
        )

    async def test_day_of_reservation_is_single_send_and_released_after_failure(self):
        first, second = await asyncio.gather(
            birthday_reminders.ensure_birthday_notification(10, "2026-09-03"),
            birthday_reminders.ensure_birthday_notification(10, "2026-09-03"),
        )
        notification_id = first or second
        self.assertIsNotNone(notification_id)
        self.assertEqual(sum(value is not None for value in (first, second)), 1)

        await birthday_reminders.release_birthday_notification(notification_id)
        retry_id = await birthday_reminders.ensure_birthday_notification(
            10,
            "2026-09-03",
        )
        self.assertIsNotNone(retry_id)

    async def test_first_admin_atomically_claims_pre_notification(self):
        notification_id = await birthday_reminders.ensure_birthday_pre_notification(
            10,
            "2026-09-03",
        )
        self.assertIsNotNone(notification_id)
        await birthday_reminders.finish_birthday_pre_notification_send(
            notification_id,
            500,
        )

        claims = await asyncio.gather(
            birthday_reminders.claim_birthday_pre_notification(
                notification_id,
                20,
                "First Admin",
            ),
            birthday_reminders.claim_birthday_pre_notification(
                notification_id,
                30,
                "Second Admin",
            ),
        )
        winners = [claim for claim in claims if claim is not None]
        self.assertEqual(len(winners), 1)
        self.assertIn(winners[0]["responsible_user_id"], {20, 30})
        self.assertEqual(winners[0]["status"], "claimed")

    async def _insert_profile(self, profile: dict) -> None:
        now = "2026-09-02T00:00:00+00:00"
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO profiles (
                    user_id, telegram_username, telegram_full_name,
                    game_nickname, birthday, join_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile["user_id"],
                    profile["telegram_username"],
                    profile["telegram_full_name"],
                    profile["game_nickname"],
                    profile["birthday"],
                    profile["join_date"],
                    now,
                    now,
                ),
            )
            await connection.commit()

    async def test_daily_run_sends_separate_today_and_tomorrow_messages(self):
        await self._insert_profile(
            profile_data(user_id=10, birthday="2000-09-02", nickname="JRঐToday")
        )
        await self._insert_profile(
            profile_data(user_id=11, birthday="2001-09-03", nickname="JRঐTomorrow")
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        bot.send_message.side_effect = [
            SimpleNamespace(message_id=101),
            SimpleNamespace(message_id=102),
        ]

        fixed_now = datetime(
            2026,
            9,
            2,
            8,
            0,
            tzinfo=birthday_reminders.KYIV_TZ,
        )
        with (
            patch.object(
                birthday_reminders,
                "_kyiv_datetime",
                return_value=fixed_now,
            ),
            patch.object(
                birthday_reminders,
                "register_birthday_daily_task",
                AsyncMock(),
            ) as register,
        ):
            await birthday_reminders.send_daily_birthday_reminders(bot)

        self.assertEqual(bot.send_message.await_count, 2)
        first_text = bot.send_message.await_args_list[0].args[1]
        second_text = bot.send_message.await_args_list[1].args[1]
        self.assertIn("Сьогодні день народження", first_text)
        self.assertIn("JRঐToday", first_text)
        self.assertIn("ЗАВТРА", second_text)
        self.assertIn("JRঐTomorrow", second_text)
        register.assert_awaited_once()

    async def test_send_failure_releases_reservation_and_preserves_next_run(self):
        await self._insert_profile(profile_data(user_id=10, birthday="2000-09-02"))
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
        )
        fixed_now = datetime(
            2026,
            9,
            2,
            8,
            0,
            tzinfo=birthday_reminders.KYIV_TZ,
        )
        with (
            patch.object(
                birthday_reminders,
                "_kyiv_datetime",
                return_value=fixed_now,
            ),
            patch.object(
                birthday_reminders,
                "register_birthday_daily_task",
                AsyncMock(),
            ) as register,
        ):
            with self.assertRaises(RuntimeError):
                await birthday_reminders.send_daily_birthday_reminders(bot)

        register.assert_awaited_once()
        retry_id = await birthday_reminders.ensure_birthday_notification(
            10,
            "2026-09-02",
        )
        self.assertIsNotNone(retry_id)


class BirthdayCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_notification_claim_edits_message_with_responsible_mention(self):
        callback = SimpleNamespace(
            data="bdpre:claim:7",
            from_user=SimpleNamespace(id=20, full_name="Officer <One>"),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        claimed = {
            "user_id": 10,
            "birthday_date": "2026-09-03",
            "responsible_user_id": 20,
            "responsible_name": "Officer <One>",
        }
        with (
            patch.object(
                profile_admin,
                "_effective_admin_level",
                AsyncMock(return_value=1),
            ),
            patch.object(
                profile_admin,
                "claim_birthday_pre_notification",
                AsyncMock(return_value=claimed),
            ),
            patch.object(
                profile_admin.profile_service,
                "get_profile",
                AsyncMock(return_value=profile_data()),
            ),
        ):
            await profile_admin.birthday_pre_reminder_callback(callback)

        edited_text = callback.message.edit_text.await_args.args[0]
        self.assertIn("Відповідальний за привітання", edited_text)
        self.assertIn('href="tg://user?id=20"', edited_text)
        self.assertIsNone(callback.message.edit_text.await_args.kwargs["reply_markup"])

    async def test_pre_notification_claim_rejects_non_admin(self):
        callback = SimpleNamespace(
            data="bdpre:claim:7",
            from_user=SimpleNamespace(id=99, full_name="Not Admin"),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        with (
            patch.object(
                profile_admin,
                "_effective_admin_level",
                AsyncMock(return_value=0),
            ),
            patch.object(
                profile_admin,
                "claim_birthday_pre_notification",
                AsyncMock(),
            ) as claim,
        ):
            await profile_admin.birthday_pre_reminder_callback(callback)

        claim.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            profile_admin.ACCESS_DENIED,
            show_alert=True,
        )
