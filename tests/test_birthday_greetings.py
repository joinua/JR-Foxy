import asyncio
import tempfile
import os
import unittest
from datetime import datetime, date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.core import db
from app.core.dates import KYIV_TZ
from app.handlers import birthday_greetings as handler
from app.services import birthday_greetings as service
from app.services import birthday_reminders


class BirthdayGreetingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.patches = [patch.object(db, "DB_PATH", self.path), patch.object(birthday_reminders, "DB_PATH", self.path)]
        for item in self.patches:
            item.start()
        service._locks.clear()
        await db.init_db()
        self.nid = await birthday_reminders.ensure_birthday_pre_notification(10, "2026-10-03")
        await birthday_reminders.claim_birthday_pre_notification(self.nid, 20, "Admin")
        await birthday_reminders.finish_birthday_pre_notification_send(self.nid, 500)
        await service.update(self.nid, greeting_html="<b>Вітаємо!</b>")
        self.profile = {"user_id": 10, "birthday": "2000-10-03", "status": "active", "game_nickname": "JR Fox"}
        self.bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
            edit_message_text=AsyncMock(),
        )
        self.profile_patch = patch.object(service.profile_service, "get_profile", AsyncMock(return_value=self.profile))
        self.profile_patch.start()
        self.card_patch = patch.object(service, "refresh_card", AsyncMock())
        self.card_patch.start()
        self.now = datetime(2026, 10, 3, 0, 1, tzinfo=KYIV_TZ)
        self.clock_patch = patch.object(service, "to_kyiv_datetime", return_value=self.now)
        self.clock_patch.start()

    async def asyncTearDown(self):
        self.clock_patch.stop()
        self.card_patch.stop()
        self.profile_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def process(self, now=None):
        await service.process_one(self.bot, self.nid, now or self.now)
        return await service.load(self.nid)

    async def test_no_send_before_0001_and_single_send_at_0001(self):
        await self.process(self.now.replace(minute=0))
        self.bot.send_message.assert_not_awaited()
        await asyncio.gather(self.process(), self.process())
        self.bot.send_message.assert_awaited_once_with(
            service.FAMILY_CHAT_ID, "<b>Вітаємо!</b>", parse_mode="HTML", disable_web_page_preview=True,
        )
        row = await service.load(self.nid)
        self.assertEqual(row["greeting_status"], "sent")
        self.assertEqual(row["greeting_message_id"], 99)

    async def test_same_day_catchup_but_expire_on_next_day(self):
        row = await self.process(self.now.replace(hour=18))
        self.assertEqual(row["greeting_status"], "sent")
        await service.update(self.nid, greeting_status="draft")
        self.bot.send_message.reset_mock()
        row = await self.process(self.now.replace(day=4))
        self.assertEqual(row["greeting_status"], "expired")
        self.bot.send_message.assert_not_awaited()

    async def test_missing_family_requires_explicit_permission(self):
        self.bot.get_chat_member.side_effect = [SimpleNamespace(status="member"), SimpleNamespace(status="left")]
        row = await self.process()
        self.assertEqual(row["greeting_status"], "draft")
        self.assertEqual(self.bot.send_message.await_args.args[0], service.ADMIN_LOG_CHAT_ID)
        await service.update(self.nid, allow_main=1)
        self.bot.get_chat_member.side_effect = [SimpleNamespace(status="member"), SimpleNamespace(status="left")]
        self.bot.send_message.reset_mock()
        row = await self.process()
        self.assertEqual(row["greeting_chat_id"], service.MAIN_CHAT_ID)
        self.assertEqual(self.bot.send_message.await_args.args[0], service.MAIN_CHAT_ID)

    async def test_unknown_family_never_routes_to_main(self):
        await service.update(self.nid, allow_main=1)
        method = GetChatMember(chat_id=service.FAMILY_CHAT_ID, user_id=10)
        self.bot.get_chat_member.side_effect = [SimpleNamespace(status="member"), TelegramBadRequest(method, "PARTICIPANT_ID_INVALID")]
        row = await self.process()
        self.assertEqual(row["greeting_status"], "draft")
        self.assertEqual(self.bot.send_message.await_args.args[0], service.ADMIN_LOG_CHAT_ID)

    async def test_departed_or_archived_player_is_cancelled(self):
        self.bot.get_chat_member.return_value = SimpleNamespace(status="left")
        self.assertEqual((await self.process())["greeting_status"], "cancelled")
        self.bot.send_message.assert_not_awaited()
        await service.update(self.nid, greeting_status="draft")
        self.profile["status"] = "archived"
        self.assertEqual((await self.process())["greeting_status"], "cancelled")

    async def test_changed_birthday_cancels_old_publication(self):
        self.profile["birthday"] = "2000-10-04"
        self.assertEqual((await self.process())["greeting_status"], "cancelled")
        self.bot.send_message.assert_not_awaited()

    async def test_missing_text_is_reported_once_and_can_be_added_today(self):
        await service.update(self.nid, greeting_html=None)
        await self.process()
        await self.process()
        self.assertEqual(self.bot.send_message.await_count, 1)
        await service.update(self.nid, greeting_html="Нове привітання", issue_notified=None)
        self.assertEqual((await self.process())["greeting_status"], "sent")

    async def test_evening_reminder_is_once_at_2100(self):
        await service.update(self.nid, greeting_html=None)
        before = self.now.replace(day=2, hour=20, minute=59)
        await self.process(before)
        self.bot.send_message.assert_not_awaited()
        await self.process(before.replace(hour=21))
        await self.process(before.replace(hour=22))
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn('tg://user?id=20', self.bot.send_message.await_args.args[1])

    async def test_ambiguous_network_result_prevents_duplicate_send(self):
        self.bot.send_message.side_effect = TimeoutError("connection lost")
        self.assertEqual((await self.process())["greeting_status"], "uncertain")
        await self.process()
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_crash_with_sending_reservation_needs_manual_check(self):
        await service.update(self.nid, greeting_status="sending")
        self.assertEqual((await self.process())["greeting_status"], "uncertain")
        self.bot.send_message.assert_not_awaited()

    async def test_delete_keeps_responsible_and_stops_publication(self):
        query = SimpleNamespace(
            data=f"bdg:confirmdelete:{self.nid}", message=SimpleNamespace(chat=SimpleNamespace(id=service.ADMIN_LOG_CHAT_ID), message_id=500),
            from_user=SimpleNamespace(id=20), answer=AsyncMock(), bot=self.bot,
        )
        with patch.object(handler, "get_admin_level", AsyncMock(return_value=1)):
            await handler.greeting_callback(query, AsyncMock())
        row = await service.load(self.nid)
        self.assertIsNone(row["greeting_html"])
        self.assertEqual(row["responsible_user_id"], 20)
        self.assertEqual(row["greeting_status"], "draft")

    async def test_permissions_owner_and_three_plus_and_revoked_roles(self):
        row = await service.load(self.nid)
        with patch.object(handler, "get_admin_level", AsyncMock(return_value=1)):
            self.assertTrue(await handler.authorized(20, row))
            self.assertFalse(await handler.authorized(30, row))
        with patch.object(handler, "get_admin_level", AsyncMock(return_value=0)):
            self.assertFalse(await handler.authorized(20, row))
        with patch.object(handler, "get_admin_level", AsyncMock(return_value=3)):
            self.assertTrue(await handler.authorized(30, row))

    async def test_restricted_membership_requires_is_member(self):
        self.bot.get_chat_member.return_value = SimpleNamespace(status="restricted", is_member=False)
        self.assertFalse(await service.membership(self.bot, service.FAMILY_CHAT_ID, 10))
        self.bot.get_chat_member.return_value.is_member = True
        self.assertTrue(await service.membership(self.bot, service.FAMILY_CHAT_ID, 10))

    async def test_greeting_migration_preserves_claimed_text_and_is_idempotent(self):
        async with aiosqlite.connect(self.path) as connection:
            await db._ensure_birthday_schema(connection)
            await db._ensure_birthday_schema(connection)
        row = await service.load(self.nid)
        self.assertEqual(row["responsible_user_id"], 20)
        self.assertEqual(row["greeting_html"], "<b>Вітаємо!</b>")

    async def test_ready_text_survives_new_connection(self):
        async with aiosqlite.connect(self.path) as connection:
            row = await (await connection.execute("SELECT greeting_html FROM birthday_pre_notifications WHERE id=?", (self.nid,))).fetchone()
        self.assertEqual(row[0], "<b>Вітаємо!</b>")

    def test_kyiv_due_time_handles_summer_and_winter(self):
        from datetime import time, timezone
        summer = datetime.combine(date(2026, 10, 3), time(0, 1), KYIV_TZ).astimezone(timezone.utc)
        winter = datetime.combine(date(2026, 12, 3), time(0, 1), KYIV_TZ).astimezone(timezone.utc)
        self.assertEqual((summer.day, summer.hour, summer.minute), (2, 21, 1))
        self.assertEqual((winter.day, winter.hour, winter.minute), (2, 22, 1))

    async def test_database_reservation_is_atomic(self):
        results = await asyncio.gather(
            service.reserve_publication(self.nid, service.FAMILY_CHAT_ID),
            service.reserve_publication(self.nid, service.FAMILY_CHAT_ID),
        )
        self.assertEqual(sum(results), 1)

    async def test_definite_telegram_rejection_allows_retry(self):
        from aiogram.methods import SendMessage
        error = TelegramBadRequest(SendMessage(chat_id=service.FAMILY_CHAT_ID, text="test"), "not enough rights")
        self.bot.send_message.side_effect = [error, SimpleNamespace(message_id=100), SimpleNamespace(message_id=101)]
        self.assertEqual((await self.process())["greeting_status"], "draft")
        self.assertEqual((await self.process())["greeting_status"], "sent")
        self.assertEqual(self.bot.send_message.await_count, 3)

    async def test_success_completes_existing_day_of_reminder(self):
        note = await birthday_reminders.ensure_birthday_notification(10, "2026-10-03")
        await birthday_reminders.postpone_birthday_notification(note)
        await self.process()
        self.assertTrue(await service.was_published(10, "2026-10-03"))
        async with aiosqlite.connect(self.path) as connection:
            row = await (await connection.execute("SELECT status FROM birthday_notifications WHERE id=?", (note,))).fetchone()
            self.assertEqual(row[0], "completed")

    async def test_text_submission_preserves_formatting_and_rejects_unrelated_message(self):
        state = AsyncMock()
        state.get_data.return_value = {"notification_id": self.nid, "prompt_id": 700}
        message = SimpleNamespace(
            text="Вітаємо!", html_text="<b>Вітаємо!</b>", reply_to_message=SimpleNamespace(message_id=701),
            from_user=SimpleNamespace(id=20), bot=self.bot, answer=AsyncMock(),
        )
        await handler.greeting_text(message, state)
        state.clear.assert_not_awaited()
        message.reply_to_message.message_id = 700
        with patch.object(handler, "get_admin_level", AsyncMock(return_value=1)):
            await handler.greeting_text(message, state)
        self.assertEqual((await service.load(self.nid))["greeting_html"], "<b>Вітаємо!</b>")
        state.clear.assert_awaited_once()
        self.assertEqual(message.answer.await_args.args[0], "<b>Вітаємо!</b>")

    async def test_wrong_chat_callback_cannot_mutate(self):
        query = SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=0)), answer=AsyncMock())
        await handler.greeting_callback(query, AsyncMock())
        self.assertEqual((await service.load(self.nid))["greeting_html"], "<b>Вітаємо!</b>")
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
