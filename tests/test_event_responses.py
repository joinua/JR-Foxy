import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
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
from app.dao import event_lifecycle
from app.dao import event_responses
from app.dao import events as events_dao
from app.services.event_notifications import send_auto_reminder, send_manual_reminder
from app.services.event_responses import apply_public_response
from app.services import event_service


class EventResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "responses.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        await db.init_db()
        self.start = 2_000_040
        self.safe = self.start - 7200
        self.close = self.start - 3600
        self.event_id = await self._insert_event()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def _insert_event(self, *, status: str = "published") -> int:
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                """
                INSERT INTO events (
                    title, event_type, starts_at_utc, safe_until_utc,
                    registration_closes_at_utc, status, created_by,
                    created_at, updated_at
                ) VALUES ('Тест КБ', 'clan', ?, ?, ?, ?, 1, 1, 1)
                """,
                (self.start, self.safe, self.close, status),
            )
            event_id = int(cursor.lastrowid)
            await connection.execute(
                """
                INSERT INTO event_publications (
                    event_id, chat_id, message_id, is_current, published_at
                ) VALUES (?, -100, ?, 1, 1)
                """,
                (event_id, 100 + event_id),
            )
            await connection.commit()
            return event_id

    async def _profile(self, user_id: int, nickname: str | None) -> None:
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO profiles (
                    user_id, game_nickname, created_at, updated_at
                ) VALUES (?, ?, 'now', 'now')
                """,
                (user_id, nickname),
            )
            await connection.commit()

    async def _respond(
        self,
        user_id: int,
        action: str,
        now: int,
    ) -> event_responses.ResponseDecision:
        return await event_responses.apply_response(
            self.event_id,
            user_id,
            action,
            telegram_name=f"User {user_id}",
            now=now,
        )

    async def _status(self, user_id: int) -> str | None:
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                """
                SELECT status FROM event_responses
                WHERE event_id=? AND user_id=?
                """,
                (self.event_id, user_id),
            )
            row = await cursor.fetchone()
            return str(row[0]) if row else None

    async def test_going_requires_nickname_and_repeated_click_keeps_order(self):
        missing = await self._respond(10, "going", self.safe - 100)
        self.assertEqual(missing.code, "missing_nickname")

        await self._profile(10, "JRঐPlayer")
        first = await self._respond(10, "going", self.safe - 90)
        repeated = await self._respond(10, "going", self.safe - 10)

        self.assertEqual(first.code, "going")
        self.assertEqual(repeated.code, "already")
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                """
                SELECT joined_at FROM event_responses
                WHERE event_id=? AND user_id=10
                """,
                (self.event_id,),
            )
            self.assertEqual((await cursor.fetchone())[0], self.safe - 90)

    async def test_public_response_checks_membership_and_refreshes_card(self):
        await self._profile(10, "JRঐPlayer")
        member_bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="member")
            ),
            edit_message_text=AsyncMock(),
        )
        user = SimpleNamespace(
            id=10,
            first_name="Player",
            last_name=None,
            username=None,
        )

        feedback = await apply_public_response(
            member_bot,
            event_id=self.event_id,
            action="going",
            user=user,
            now=self.safe - 100,
        )

        self.assertFalse(feedback.show_alert)
        self.assertEqual(feedback.text, "✅ Твій статус: «Я — учасник».")
        member_bot.edit_message_text.assert_awaited_once()

        outsider_bot = SimpleNamespace(
            get_chat_member=AsyncMock(
                return_value=SimpleNamespace(status="left")
            ),
            edit_message_text=AsyncMock(),
        )
        outsider = SimpleNamespace(
            id=11,
            first_name="Outsider",
            last_name=None,
            username=None,
        )
        denied = await apply_public_response(
            outsider_bot,
            event_id=self.event_id,
            action="going",
            user=outsider,
            now=self.safe - 90,
        )
        self.assertTrue(denied.show_alert)
        self.assertEqual(await self._status(11), None)

    async def test_capacity_check_is_atomic(self):
        for user_id in range(1, 52):
            await self._profile(user_id, f"JRঐ{user_id}")

        results = await asyncio.gather(
            *(
                self._respond(user_id, "going", self.safe - 100)
                for user_id in range(1, 52)
            )
        )

        self.assertEqual(sum(result.code == "going" for result in results), 50)
        self.assertEqual(sum(result.code == "limit" for result in results), 1)

    async def test_late_decline_requires_second_click_within_60_seconds(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 1)

        warning = await self._respond(10, "declined", self.safe)
        confirmed = await self._respond(10, "declined", self.safe + 60)

        self.assertEqual(warning.code, "late_warning")
        self.assertEqual(await self._status(10), "late_declined")
        self.assertEqual(confirmed.code, "late_declined")

    async def test_expired_late_confirmation_restarts_confirmation(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 1)
        await self._respond(10, "declined", self.safe)

        expired = await self._respond(10, "declined", self.safe + 61)
        confirmed = await self._respond(10, "declined", self.safe + 62)

        self.assertEqual(expired.code, "late_confirmation_expired")
        self.assertEqual(confirmed.code, "late_declined")

    async def test_late_decline_can_rejoin_before_close_and_moves_to_end(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 1)
        await self._respond(10, "declined", self.safe)
        await self._respond(10, "declined", self.safe + 1)

        rejoined = await self._respond(10, "going", self.close - 1)

        self.assertEqual(rejoined.code, "going")
        self.assertEqual(await self._status(10), "going")
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                """
                SELECT joined_at FROM event_responses
                WHERE event_id=? AND user_id=10
                """,
                (self.event_id,),
            )
            self.assertEqual((await cursor.fetchone())[0], self.close - 1)

    async def test_late_decline_cannot_be_erased_by_thinking(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 1)
        await self._respond(10, "declined", self.safe)
        await self._respond(10, "declined", self.safe + 1)

        blocked = await self._respond(10, "thinking", self.safe + 2)

        self.assertEqual(blocked.code, "thinking_late_blocked")
        self.assertEqual(await self._status(10), "late_declined")

    async def test_time_boundaries_are_inclusive(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 1)

        thinking = await self._respond(10, "thinking", self.safe)
        self.assertEqual(thinking.code, "thinking_late_blocked")
        new_thinking = await self._respond(11, "thinking", self.safe)
        self.assertEqual(new_thinking.code, "thinking")
        closed = await self._respond(11, "thinking", self.close)
        self.assertEqual(closed.code, "registration_closed")
        started = await self._respond(10, "declined", self.start)
        self.assertEqual(started.code, "started")

    async def test_ordinary_decline_does_not_require_profile(self):
        declined = await self._respond(12, "declined", self.safe - 1)
        self.assertEqual(declined.code, "declined")
        self.assertEqual(await self._status(12), "declined")

    async def test_after_close_only_current_participant_can_late_decline(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 1)

        blocked = await self._respond(11, "declined", self.close)
        warning = await self._respond(10, "declined", self.close)

        self.assertEqual(blocked.code, "registration_closed")
        self.assertEqual(warning.code, "late_warning")

    async def test_close_expires_thinking_without_rating_effect(self):
        await self._respond(10, "thinking", self.safe - 1)

        result = await event_lifecycle.close_registration(
            self.event_id,
            expected_at=self.close,
            now=self.close,
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.thinking_expired, 1)
        self.assertEqual(await self._status(10), "thinking_expired")

    async def test_start_cancels_empty_event_but_keeps_late_commitment(self):
        empty = await event_lifecycle.start_event(
            self.event_id,
            expected_at=self.start,
            now=self.start,
        )
        self.assertEqual(empty.status, "cancelled")

        second_id = await self._insert_event()
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO event_responses (
                    event_id, user_id, status, responded_at, updated_at
                ) VALUES (?, 99, 'late_declined', 1, 1)
                """,
                (second_id,),
            )
            await connection.commit()
        started = await event_lifecycle.start_event(
            second_id,
            expected_at=self.start,
            now=self.start,
        )
        self.assertEqual(started.status, "started")

    async def test_auto_reminder_is_reserved_once(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 100)
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=777))
        )
        scheduled_at = self.start - 3 * 60 * 60

        first = await send_auto_reminder(
            bot,
            self.event_id,
            expected_at=scheduled_at,
            now=scheduled_at,
        )
        second = await send_auto_reminder(
            bot,
            self.event_id,
            expected_at=scheduled_at,
            now=scheduled_at + 1,
        )

        self.assertEqual(first, "sent")
        self.assertEqual(second, "duplicate")
        bot.send_message.assert_awaited_once()

    async def test_unknown_auto_reminder_delivery_is_not_retried(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 100)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("network")))
        scheduled_at = self.start - 3 * 60 * 60

        first = await send_auto_reminder(
            bot,
            self.event_id,
            expected_at=scheduled_at,
            now=scheduled_at,
        )
        second = await send_auto_reminder(
            bot,
            self.event_id,
            expected_at=scheduled_at,
            now=scheduled_at + 1,
        )

        self.assertEqual(first, "unknown")
        self.assertEqual(second, "duplicate")
        bot.send_message.assert_awaited_once()

    async def test_missed_auto_reminder_is_skipped_after_registration_close(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 100)
        bot = SimpleNamespace(send_message=AsyncMock())
        scheduled_at = self.start - 3 * 60 * 60

        result = await send_auto_reminder(
            bot,
            self.event_id,
            expected_at=scheduled_at,
            now=self.close,
        )

        self.assertEqual(result, "skipped")
        bot.send_message.assert_not_awaited()

    async def test_manual_reminder_enforces_one_hour_cooldown(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 100)
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=777))
        )

        first = await send_manual_reminder(
            bot,
            self.event_id,
            audience="going",
            actor_id=1,
            now=self.safe - 50,
        )
        second = await send_manual_reminder(
            bot,
            self.event_id,
            audience="going",
            actor_id=2,
            now=self.safe - 49,
        )

        self.assertEqual(first.code, "sent")
        self.assertEqual(second.code, "cooldown")
        self.assertEqual(second.retry_after, 3599)
        bot.send_message.assert_awaited_once()

    async def test_reschedule_resets_responses_and_returns_recipients(self):
        await self._profile(10, "JRঐPlayer")
        await self._respond(10, "going", self.safe - 100)
        payload = {
            "title": "Тест КБ",
            "event_type": "clan",
            "date": "1970-01-25",
            "time": "00:00",
        }
        await events_dao.create_edit_draft(
            1,
            self.event_id,
            base_version=1,
            payload=payload,
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.safe - 50,
            expires_at=self.start + 100_000,
        )
        new_start = self.start + 86_400

        result = await events_dao.apply_edit_draft(
            1,
            admin_level=3,
            title="Перенесений тест",
            event_type="clan",
            description=None,
            starts_at_utc=new_start,
            safe_until_utc=new_start - 7200,
            registration_closes_at_utc=new_start - 3600,
            now=self.safe - 40,
        )

        self.assertTrue(result["rescheduled"])
        self.assertEqual(result["respondents"][0]["user_id"], 10)
        self.assertIsNone(await self._status(10))
        card = await events_dao.get_event_card(self.event_id)
        self.assertEqual(card["status"], "published")
        self.assertEqual(card["starts_at_utc"], new_start)

    async def test_expired_edit_draft_does_not_delete_publication(self):
        await events_dao.create_edit_draft(
            1,
            self.event_id,
            base_version=1,
            payload={},
            menu_chat_id=-200,
            menu_message_id=10,
            now=1,
            expires_at=2,
        )

        deleted = await events_dao.cleanup_expired_drafts(now=3)

        self.assertEqual(deleted, 1)
        card = await events_dao.get_event_card(self.event_id)
        self.assertIsNotNone(card)
        self.assertEqual(card["publication"]["message_id"], 101)

    async def test_edit_version_conflict_rejects_stale_draft(self):
        await events_dao.create_edit_draft(
            1,
            self.event_id,
            base_version=1,
            payload={},
            menu_chat_id=-200,
            menu_message_id=10,
            now=1,
            expires_at=self.start,
        )
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                "UPDATE events SET version=2 WHERE id=?",
                (self.event_id,),
            )
            await connection.commit()

        with self.assertRaises(events_dao.EventVersionError):
            await events_dao.apply_edit_draft(
                1,
                admin_level=3,
                title="Новий тест",
                event_type="clan",
                description=None,
                starts_at_utc=self.start,
                safe_until_utc=self.safe,
                registration_closes_at_utc=self.close,
                now=2,
            )

    async def test_metadata_edit_near_start_is_allowed_but_reschedule_is_not(self):
        draft = await event_service.create_edit_draft(
            1,
            self.event_id,
            admin_level=3,
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.start - 1000,
        )
        draft = await event_service.save_draft_field(
            1,
            "title",
            "Нова назва",
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.start - 999,
        )
        unchanged = await event_service.validate_draft(
            draft,
            now=self.start - 998,
        )
        self.assertEqual(unchanged.starts_at_utc, self.start)

        current_clock = datetime.strptime(draft["payload"]["time"], "%H:%M")
        shifted_time = (current_clock + timedelta(minutes=1)).strftime("%H:%M")
        draft = await event_service.save_draft_field(
            1,
            "time",
            shifted_time,
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.start - 997,
        )
        with self.assertRaisesRegex(
            event_service.EventValidationError,
            "щонайменше за 24 години",
        ):
            await event_service.validate_draft(draft, now=self.start - 996)


if __name__ == "__main__":
    unittest.main()
