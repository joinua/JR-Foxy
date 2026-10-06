"""Exit workflow: duplicate events, staff exception, and honest completion."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.core import db
from app.dao import clan_exit as dao
from app.services import clan_exit as service


class ClanExitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "jrfoxy.db"
        for module in (db, dao):
            p = patch.object(module, "DB_PATH", path)
            p.start()
            self.addCleanup(p.stop)
        await db.init_db()

    async def test_duplicate_exit_does_not_create_second_case(self):
        first, created = await dao.create(42, "вийшов сам", "JRঐTest", "Name", "2025-01-01")
        second, duplicate = await dao.create(42, "вийшов сам", "JRঐTest", "Name", "2025-01-01")
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(first["id"], second["id"])
        await dao.change(first["id"], 2, "returned", status="returned")
        third, created_again = await dao.create(42, "вийшов сам", None, "Name", None)
        self.assertTrue(created_again)
        self.assertNotEqual(first["id"], third["id"])

    async def test_normal_member_removed_but_chat_admin_left_for_manual_action(self):
        case, _ = await dao.create(42, "вийшов сам", "JRঐTest", "Name", None)
        statuses = {-300: "member", -400: "administrator"}
        bot = AsyncMock()
        bot.get_chat_member.side_effect = lambda chat_id, user_id: SimpleNamespace(status=statuses[chat_id])
        async def remove(chat_id, user_id):
            statuses[chat_id] = "left"
        bot.unban_chat_member.side_effect = remove
        with patch.object(service, "OTHER_CHATS", {-300: "Родина", -400: "Офіцери"}), \
                patch.object(service, "main_member", AsyncMock(return_value=False)), \
                patch.object(service, "publish", AsyncMock()):
            updated = await service.refresh_chats(bot, case, remove=True)
        states = json.loads(updated["chat_states"])
        self.assertEqual(states["-300"], "немає")
        self.assertEqual(states["-400"], "адміністратор — вилучити вручну")
        bot.unban_chat_member.assert_awaited_once_with(-300, 42)

    async def test_no_response_requires_documented_contact_and_wait(self):
        case, _ = await dao.create(42, "вийшов сам", "JRঐTest", "Name", None)
        self.assertNotIn("Пояснення не отримано", str(service.keyboard(case)))
        await dao.change(case["id"], 2, "contact_confirmed", contact_by=2,
                         contact_at=1, explanation_status="requested")
        case = await dao.get(case["id"])
        self.assertIn("Пояснення не отримано", str(service.keyboard(case)))
        await dao.change(case["id"], 2, "explanation_unavailable",
                         explanation_status="unavailable", explanation_at=2)
        self.assertIn("пояснення не отримано", service.card(await dao.get(case["id"])))


class ClanExitCallbackTests(unittest.IsolatedAsyncioTestCase):
    """Run the real handler/DAO/service path; only Telegram and access are mocked."""

    async def asyncSetUp(self):
        await ClanExitTests.asyncSetUp(self)
        from app.handlers import clan_exit as handler
        self.handler = handler
        self.case, _ = await dao.create(42, "вийшов сам", "JR<Test>", "Name", None)
        await dao.attach_message(self.case["id"], 700)
        self.bot = AsyncMock()
        self.bot.get_chat_member.return_value = SimpleNamespace(status="left")
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=handler.ADMIN_LOG_CHAT_ID), message_id=700,
            answer=AsyncMock(), bot=self.bot,
        )
        self.query = SimpleNamespace(
            id="callback-1", message=self.message, from_user=SimpleNamespace(id=2),
            bot=self.bot, answer=AsyncMock(), data="",
        )
        self.state = AsyncMock()
        access = patch.object(handler, "has_admin_level", AsyncMock(return_value=True))
        access.start()
        self.addCleanup(access.stop)
        chats = patch.object(service, "OTHER_CHATS", {-300: "Родина", -400: "Офіцери"})
        chats.start()
        self.addCleanup(chats.stop)
        # The handler must acknowledge before either mutation or publication/checks.
        original_change = dao.change
        async def checked_change(*args, **kwargs):
            self.assertEqual(self.query.answer.await_count, 1)
            return await original_change(*args, **kwargs)
        change = patch.object(dao, "change", checked_change)
        change.start()
        self.addCleanup(change.stop)
        async def checked_member(*args, **kwargs):
            self.assertEqual(self.query.answer.await_count, 1)
            return SimpleNamespace(status="left")
        self.bot.get_chat_member.side_effect = checked_member

    async def action(self, action):
        self.query.data = f"exit:{action}:{self.case['id']}"
        await self.handler.exit_action(self.query, self.state)
        return await dao.get(self.case["id"])

    async def test_contact_updates_card_with_timestamp_and_correct_message_ids(self):
        updated = await self.action("contact")
        self.assertEqual(updated["explanation_status"], "requested")
        self.assertEqual(updated["contact_by"], 2)
        self.assertGreater(updated["contact_at"], 0)
        kwargs = self.bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], self.handler.ADMIN_LOG_CHAT_ID)
        self.assertEqual(kwargs["message_id"], 700)
        self.assertIn("запит надіслано ·", kwargs["text"])
        self.assertEqual(self.query.answer.await_count, 1)

    async def test_refresh_checks_main_once_and_updates_card(self):
        updated = await self.action("refresh")
        self.assertEqual(json.loads(updated["chat_states"])["-300"], "немає")
        main_calls = [c for c in self.bot.get_chat_member.await_args_list if c.args[0] == service.MAIN_CHAT_ID]
        self.assertEqual(len(main_calls), 1)
        self.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.query.answer.await_count, 1)

    async def test_game_confirmation_is_persisted_and_published(self):
        updated = await self.action("game")
        self.assertEqual(updated["game_by"], 2)
        self.assertGreater(updated["game_at"], 0)
        self.assertIn("вилучено · підтвердив 2", self.bot.edit_message_text.await_args.kwargs["text"])

    async def test_publish_failure_reports_saved_contact_without_losing_data(self):
        self.bot.edit_message_text.side_effect = RuntimeError("Telegram unavailable")
        with self.assertLogs(self.handler.logger, level="ERROR"):
            updated = await self.action("contact")
        self.assertEqual(updated["explanation_status"], "requested")
        self.assertIn("дані збережено", self.message.answer.await_args.args[0])
        self.assertEqual(self.query.answer.await_count, 1)
        self.bot.edit_message_text.side_effect = None
        self.query.answer.reset_mock()
        await self.action("refresh")
        self.assertIn("запит надіслано ·", self.bot.edit_message_text.await_args.kwargs["text"])

    async def test_expired_callback_does_not_prevent_update(self):
        self.query.answer.side_effect = RuntimeError("query is too old")
        with self.assertLogs(self.handler.logger, level="ERROR"):
            updated = await self.action("contact")
        self.assertEqual(updated["explanation_status"], "requested")
        self.bot.edit_message_text.assert_awaited_once()

    async def test_database_failure_is_reported_after_acknowledgement(self):
        with patch.object(dao, "change", AsyncMock(side_effect=RuntimeError("database is locked"))), \
                self.assertLogs(self.handler.logger, level="ERROR"):
            updated = await self.action("game")
        self.assertIsNone(updated["game_at"])
        self.assertIn("Не вдалося завершити", self.message.answer.await_args.args[0])
        self.assertEqual(self.query.answer.await_count, 1)

    async def test_postpone_remains_open_and_refreshes_card(self):
        updated = await self.action("later")
        self.assertEqual(updated["status"], "open")
        self.bot.edit_message_text.assert_awaited_once()
        self.assertIn("лишається відкритою", self.message.answer.await_args.args[0])

    async def test_explanation_prompt_acknowledged_before_fsm_change(self):
        async def set_state(*args):
            self.assertEqual(self.query.answer.await_count, 1)
        self.state.set_state.side_effect = set_state
        await self.action("explain")
        self.state.set_state.assert_awaited_once_with(self.handler.Explanation.text)
        self.state.update_data.assert_awaited_once_with(exit_check_id=self.case["id"])
        self.assertTrue(self.query.answer.await_args.kwargs["show_alert"])

    async def test_unavailable_is_blocked_without_waiting_period(self):
        updated = await self.action("unavailable")
        self.assertEqual(updated["explanation_status"], "not_requested")
        self.bot.edit_message_text.assert_not_awaited()
        self.assertTrue(self.query.answer.await_args.kwargs["show_alert"])

    async def test_unavailable_updates_after_documented_contact_and_wait(self):
        with patch.object(self.handler.time, "time", return_value=100):
            await self.action("contact")
        self.query.answer.reset_mock()
        updated = await self.action("unavailable")
        self.assertEqual(updated["explanation_status"], "unavailable")
        self.assertIn("пояснення не отримано", self.bot.edit_message_text.await_args.kwargs["text"])

    async def test_return_closes_card_and_removes_keyboard(self):
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(status="member")
        updated = await self.action("return")
        self.assertEqual(updated["status"], "returned")
        self.assertEqual(updated["closed_by"], 2)
        self.assertIsNone(self.bot.edit_message_text.await_args.kwargs["reply_markup"])
        self.bot.unban_chat_member.assert_not_awaited()

    async def test_unconfirmed_return_notifies_without_closing(self):
        updated = await self.action("return")
        self.assertEqual(updated["status"], "open")
        self.assertIn("не підтверджено", self.message.answer.await_args.args[0])

    async def prepare_finish(self):
        await self.action("game")
        self.query.answer.reset_mock()
        with patch.object(self.handler.time, "time", return_value=100):
            await self.action("contact")
        self.query.answer.reset_mock()
        await self.action("unavailable")
        self.query.answer.reset_mock()
        self.bot.get_chat_member.reset_mock()

    async def test_finish_requires_live_chat_check_and_all_steps(self):
        await self.prepare_finish()
        updated = await self.action("finish")
        self.assertEqual(updated["status"], "closed")
        self.assertEqual(updated["closed_by"], 2)
        main_calls = [c for c in self.bot.get_chat_member.await_args_list if c.args[0] == service.MAIN_CHAT_ID]
        self.assertEqual(len(main_calls), 1)
        self.bot.unban_chat_member.assert_not_awaited()
        self.assertIsNone(self.bot.edit_message_text.await_args.kwargs["reply_markup"])

    async def test_finish_does_not_close_with_missing_game_or_explanation(self):
        updated = await self.action("finish")
        self.assertEqual(updated["status"], "open")
        self.assertIn("незавершені кроки", self.message.answer.await_args.args[0])

    async def test_finish_marks_returned_without_other_chat_checks(self):
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(status="member")
        updated = await self.action("finish")
        self.assertEqual(updated["status"], "returned")
        self.bot.get_chat_member.assert_awaited_once_with(service.MAIN_CHAT_ID, 42)
        self.bot.unban_chat_member.assert_not_awaited()

    async def test_unavailable_main_never_removes_or_closes_and_updates_warning(self):
        await self.prepare_finish()
        self.bot.get_chat_member.side_effect = TimeoutError("main chat timed out")
        with self.assertLogs(service.logger, level="ERROR"):
            updated = await self.action("finish")
        self.assertEqual(updated["status"], "open")
        self.bot.unban_chat_member.assert_not_awaited()
        self.assertIn("Головний чат недоступний", self.bot.edit_message_text.await_args.kwargs["text"])
        self.assertEqual(json.loads(updated["chat_states"])["-300"], service.CHECK_FAILED)
        self.assertNotIn("Завершити перевірку", str(service.keyboard(updated)))

    async def test_external_chat_timeout_does_not_prevent_other_chat_or_card_update(self):
        import asyncio
        async def member(chat_id, user_id):
            if chat_id == -300:
                await asyncio.Event().wait()
            return SimpleNamespace(status="left")
        self.bot.get_chat_member.side_effect = member
        with patch.object(service, "TELEGRAM_TIMEOUT_SECONDS", 0.02), \
                self.assertLogs(service.logger, level="ERROR"):
            updated = await self.action("refresh")
        states = json.loads(updated["chat_states"])
        self.assertEqual(states["-300"], service.CHECK_FAILED)
        self.assertEqual(states["-400"], "немає")
        self.bot.edit_message_text.assert_awaited_once()

    async def test_publish_timeout_keeps_saved_contact_and_sends_feedback(self):
        import asyncio
        async def edit(*args, **kwargs):
            await asyncio.Event().wait()
        self.bot.edit_message_text.side_effect = edit
        with patch.object(service, "TELEGRAM_TIMEOUT_SECONDS", 0.02), \
                self.assertLogs(self.handler.logger, level="ERROR"):
            updated = await self.action("contact")
        self.assertEqual(updated["explanation_status"], "requested")
        self.assertIn("дані збережено", self.message.answer.await_args.args[0])

    async def test_invalid_or_inactive_callback_cannot_change_case(self):
        for data in ("exit:game:bad", "exit:missing:1", "exit:game:9999"):
            with self.subTest(data=data):
                self.query.data = data
                self.query.answer.reset_mock()
                await self.handler.exit_action(self.query, self.state)
                self.assertEqual(self.query.answer.await_count, 1)
                self.assertTrue(self.query.answer.await_args.kwargs["show_alert"])
        self.message.message_id = 701
        await self.action("game")
        self.assertIsNone((await dao.get(self.case["id"]))["game_at"])
        self.bot.edit_message_text.assert_not_awaited()

    async def test_wrong_chat_or_role_cannot_change_case(self):
        self.message.chat.id = 0
        await self.action("contact")
        self.message.chat.id = self.handler.ADMIN_LOG_CHAT_ID
        self.query.answer.reset_mock()
        with patch.object(self.handler, "has_admin_level", AsyncMock(return_value=False)):
            await self.action("contact")
        self.assertEqual((await dao.get(self.case["id"]))["explanation_status"], "not_requested")
        self.bot.edit_message_text.assert_not_awaited()

    async def test_real_aiogram_edit_method_receives_correct_address(self):
        from aiogram import Bot
        from aiogram.methods import EditMessageText
        bot = Bot("123:test")
        case = await dao.get(self.case["id"])
        with patch.object(Bot, "__call__", AsyncMock(return_value=True)) as call:
            await service.publish(bot, case)
        request = call.await_args.args[0]
        self.assertIsInstance(request, EditMessageText)
        self.assertEqual(request.chat_id, self.handler.ADMIN_LOG_CHAT_ID)
        self.assertEqual(request.message_id, 700)
        self.assertIsNone(request.business_connection_id)
        await bot.session.close()

    async def test_not_modified_is_success_but_other_edit_errors_are_reported(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageText
        method = EditMessageText(chat_id=self.handler.ADMIN_LOG_CHAT_ID, message_id=700, text="test")
        self.bot.edit_message_text.side_effect = TelegramBadRequest(method, "message is not modified")
        await self.action("contact")
        self.message.answer.assert_not_awaited()
        self.query.answer.reset_mock()
        self.bot.edit_message_text.side_effect = TelegramBadRequest(method, "message to edit not found")
        with self.assertLogs(self.handler.logger, level="ERROR"):
            await self.action("game")
        self.assertIn("дані збережено", self.message.answer.await_args.args[0])

    async def test_closed_card_can_retry_failed_publication_without_rechecking_chats(self):
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(status="member")
        self.bot.edit_message_text.side_effect = RuntimeError("Telegram unavailable")
        with self.assertLogs(self.handler.logger, level="ERROR"):
            updated = await self.action("return")
        self.assertEqual(updated["status"], "returned")
        self.query.answer.reset_mock()
        self.bot.edit_message_text.side_effect = None
        self.bot.get_chat_member.reset_mock()
        await self.action("refresh")
        self.bot.get_chat_member.assert_not_awaited()
        self.assertIsNone(self.bot.edit_message_text.await_args.kwargs["reply_markup"])
        self.query.answer.reset_mock()
        await self.action("game")
        self.assertTrue(self.query.answer.await_args.kwargs["show_alert"])
        self.assertIsNone((await dao.get(self.case["id"]))["game_at"])

    async def test_recorded_explanation_survives_failed_card_edit(self):
        await self.action("explain")
        self.state.get_data.return_value = {"exit_check_id": self.case["id"]}
        self.message.text = "Потрібна перерва <на місяць>"
        self.message.from_user = self.query.from_user
        self.bot.edit_message_text.side_effect = RuntimeError("Telegram unavailable")
        with self.assertLogs(self.handler.logger, level="ERROR"):
            await self.handler.record_explanation(self.message, self.state)
        updated = await dao.get(self.case["id"])
        self.assertEqual(updated["explanation_status"], "explained")
        self.assertEqual(updated["explanation_text"], self.message.text)
        self.state.clear.assert_awaited_once()
        self.assertIn("збережено", self.message.answer.await_args.args[0])
        self.assertIn("&lt;на місяць&gt;", service.card(updated))


if __name__ == "__main__":
    unittest.main()
