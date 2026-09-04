import os
import tempfile
import unittest
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
from app.dao import event_reviews
from app.handlers.events.keyboards import review_keyboard
from app.services.event_render import render_public_card
from app.services.event_reviews import create_and_send_review, send_review_reminder


class EventReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "reviews.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        await db.init_db()
        self.start = 2_000_000
        self.review_at = self.start + 3600
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                """
                INSERT INTO events (
                    title, event_type, starts_at_utc, safe_until_utc,
                    registration_closes_at_utc, status,
                    created_by, created_at, updated_at
                ) VALUES ('Кланова гра', 'clan', ?, ?, ?, 'started', 1, 1, 1)
                """,
                (self.start, self.start - 7200, self.start - 3600),
            )
            self.event_id = int(cursor.lastrowid)
            await connection.execute(
                """
                INSERT INTO event_publications (
                    event_id, chat_id, message_id, is_current, published_at
                ) VALUES (?, -100, 50, 1, 1)
                """,
                (self.event_id,),
            )
            await connection.commit()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def _response(self, user_id: int, status: str, joined: int | None = None):
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO event_responses (
                    event_id, user_id, status, nickname_snapshot,
                    telegram_name_snapshot, joined_at, responded_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.event_id, user_id, status, f"JRঐ{user_id}",
                    f"User {user_id}", joined, user_id, user_id,
                ),
            )
            await connection.commit()

    async def _create(self):
        return await event_reviews.create_review(
            self.event_id, expected_at=self.review_at, now=self.review_at
        )

    async def test_review_contains_only_committed_players_and_prefills_late_decline(self):
        await self._response(10, "going", 10)
        await self._response(11, "late_declined", 11)
        await self._response(12, "thinking_expired", 12)

        created = await self._create()
        review = await event_reviews.get_review(self.event_id)

        self.assertEqual(created.code, "created")
        self.assertEqual(review["total"], 2)
        self.assertEqual(review["done"], 1)
        self.assertEqual(review["players"][1]["result"], "late_decline")

    async def test_review_pages_by_ten_and_callback_data_stays_short(self):
        for user_id in range(1, 13):
            await self._response(user_id, "going", user_id)
        await self._create()

        first = await event_reviews.get_review(self.event_id, page=0)
        second = await event_reviews.get_review(self.event_id, page=1)
        keyboard = review_keyboard(second)

        self.assertEqual(len(first["players"]), 10)
        self.assertEqual(len(second["players"]), 2)
        self.assertTrue(
            all(
                len(button.callback_data.encode()) <= 64
                for row in keyboard.inline_keyboard
                for button in row
            )
        )

    async def test_last_admin_mark_wins_and_exclusion_requires_reason(self):
        await self._response(10, "going", 10)
        await self._create()
        self.assertEqual(
            await event_reviews.set_result(
                self.event_id, 10, "present", actor_id=30, now=self.review_at + 1
            ),
            "updated",
        )
        self.assertEqual(
            await event_reviews.set_result(
                self.event_id, 10, "no_show", actor_id=31, now=self.review_at + 2
            ),
            "updated",
        )
        with self.assertRaises(ValueError):
            await event_reviews.set_result(
                self.event_id, 10, "excluded", actor_id=31,
                now=self.review_at + 3, reason="x",
            )
        review = await event_reviews.get_review(self.event_id)
        self.assertEqual(review["players"][0]["result"], "no_show")

    async def test_finalization_requires_all_results_and_same_admin_second_click(self):
        await self._response(10, "going", 10)
        await self._create()
        incomplete = await event_reviews.request_or_finalize(
            self.event_id, actor_id=30, now=self.review_at + 1
        )
        self.assertEqual(incomplete.code, "incomplete")
        await event_reviews.set_result(
            self.event_id, 10, "present", actor_id=30, now=self.review_at + 2
        )
        first = await event_reviews.request_or_finalize(
            self.event_id, actor_id=30, now=self.review_at + 3
        )
        other_admin = await event_reviews.request_or_finalize(
            self.event_id, actor_id=31, now=self.review_at + 4
        )
        second = await event_reviews.request_or_finalize(
            self.event_id, actor_id=31, now=self.review_at + 5
        )
        self.assertEqual(first.code, "confirm")
        self.assertEqual(other_admin.code, "confirm")
        self.assertEqual(second.code, "finalized")

    async def test_result_change_invalidates_finalize_confirmation(self):
        await self._response(10, "going", 10)
        await self._create()
        await event_reviews.set_result(
            self.event_id, 10, "present", actor_id=30, now=self.review_at + 1
        )
        self.assertEqual(
            (await event_reviews.request_or_finalize(
                self.event_id, actor_id=30, now=self.review_at + 2
            )).code,
            "confirm",
        )
        await event_reviews.set_result(
            self.event_id, 10, "no_show", actor_id=31, now=self.review_at + 3
        )
        self.assertEqual(
            (await event_reviews.request_or_finalize(
                self.event_id, actor_id=30, now=self.review_at + 4
            )).code,
            "confirm",
        )

    async def test_correction_window_and_level_four_override(self):
        await self._response(10, "going", 10)
        await self._create()
        await event_reviews.set_result(
            self.event_id, 10, "present", actor_id=30, now=self.review_at + 1
        )
        await event_reviews.request_or_finalize(
            self.event_id, actor_id=30, now=self.review_at + 2
        )
        finalized = await event_reviews.request_or_finalize(
            self.event_id, actor_id=30, now=self.review_at + 3
        )
        late = int(finalized.finalized_at) + 24 * 3600 + 1

        self.assertEqual(
            await event_reviews.correct_result(
                self.event_id, 10, "no_show", actor_id=31, admin_level=3,
                reason="Виправлення факту", now=late,
            ),
            "expired",
        )
        self.assertEqual(
            await event_reviews.correct_result(
                self.event_id, 10, "no_show", actor_id=1, admin_level=4,
                reason="Виправлення факту", now=late,
            ),
            "updated",
        )

    async def test_cancellation_requires_repeated_confirmation(self):
        first = await event_reviews.request_or_cancel(
            self.event_id, actor_id=30, reason="Гру перенесено", now=100
        )
        second = await event_reviews.request_or_cancel(
            self.event_id, actor_id=30, reason="Гру перенесено", now=101
        )
        self.assertEqual(first.code, "confirm")
        self.assertEqual(second.code, "cancelled")

    async def test_review_delivery_and_reminder_are_not_duplicated(self):
        await self._response(10, "going", 10)
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=77))
        )
        first = await create_and_send_review(
            bot, self.event_id, expected_at=self.review_at, now=self.review_at
        )
        duplicate = await create_and_send_review(
            bot, self.event_id, expected_at=self.review_at, now=self.review_at + 1
        )
        reminder_at = self.review_at + 6 * 3600
        reminder = await send_review_reminder(
            bot, self.event_id, expected_at=reminder_at, now=reminder_at
        )
        repeated = await send_review_reminder(
            bot, self.event_id, expected_at=reminder_at, now=reminder_at + 1
        )
        self.assertEqual((first, duplicate, reminder, repeated),
                         ("sent", "duplicate", "sent", "duplicate"))
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_completed_public_card_contains_only_aggregate_results(self):
        card = {
            "title": "Кланова гра", "event_type": "clan", "description": None,
            "starts_at_utc": self.start, "safe_until_utc": self.start - 7200,
            "registration_closes_at_utc": self.start - 3600,
            "status": "completed", "participants": [{"user_id": 10, "nickname": "Secret"}],
            "result_counts": {"present": 1, "no_show": 2, "late_decline": 1, "excluded": 1},
        }
        rendered = render_public_card(card)
        self.assertIn("Присутні: 1", rendered)
        self.assertNotIn("Secret", rendered)

    async def test_nickname_is_current_until_finalize_then_frozen(self):
        await self._response(10, "going", 10)
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO profiles (user_id, game_nickname, created_at, updated_at)
                VALUES (10, 'JRঐCurrent', 'now', 'now')
                """
            )
            await connection.commit()
        await self._create()
        await event_reviews.set_result(
            self.event_id, 10, "present", actor_id=30, now=self.review_at + 1
        )
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                "UPDATE profiles SET game_nickname='JRঐBeforeFinal' WHERE user_id=10"
            )
            await connection.commit()
        self.assertEqual(
            (await event_reviews.get_review(self.event_id))["players"][0]["nickname"],
            "JRঐBeforeFinal",
        )
        await event_reviews.request_or_finalize(
            self.event_id, actor_id=30, now=self.review_at + 2
        )
        await event_reviews.request_or_finalize(
            self.event_id, actor_id=30, now=self.review_at + 3
        )
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                "UPDATE profiles SET game_nickname='JRঐAfterFinal' WHERE user_id=10"
            )
            await connection.commit()
        self.assertEqual(
            (await event_reviews.get_review(self.event_id))["players"][0]["nickname"],
            "JRঐBeforeFinal",
        )
