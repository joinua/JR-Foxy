import asyncio
import ast
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from app.core import db

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import GetChatMember
from app.handlers import invite
from app.services import db_scheduler


class CandidateRulesDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        await db.init_db()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def _join(self, user_id=1, due=10_000):
        await db.upsert_candidate_on_join(user_id, -100, due)
        return await db.get_candidate(user_id, -100)

    async def test_departure_closes_active_records_and_cancels_review(self):
        with patch.object(invite, "INVITE_CHAT_ID", -100):
            for status in ("candidate", "wait", "invited"):
                await self._join()
                await db.update_candidate_status(1, -100, status)
                await db.schedule_task(task_type="invite_review_due", run_at=1, chat_id=-100, user_id=1)
                await invite._close_departed_candidate(1)
                self.assertEqual((await db.get_candidate(1, -100))["status"], "left")
                async with aiosqlite.connect(self.db_path) as connection:
                    row = await (await connection.execute(
                        "SELECT COUNT(*) FROM scheduled_tasks WHERE task_type='invite_review_due' AND status='pending'"
                    )).fetchone()
                self.assertEqual(row[0], 0)
            await db.update_candidate_status(1, -100, "accepted")
            await invite._close_departed_candidate(1)
            self.assertEqual((await db.get_candidate(1, -100))["status"], "accepted")

    async def test_join_starts_fresh_rules_session_without_changing_welcome_constant(
        self,
    ):
        candidate = await self._join()
        source = Path("app/handlers/invite.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and node.targets[0].id == "INVITE_WELCOME_TEXT"
        )
        self.assertTrue(
            ast.literal_eval(assignment.value).startswith("Привіт! Цей чат")
        )
        self.assertEqual(candidate["rules_status"], "not_sent")
        self.assertIsNone(candidate["rules_first_message_id"])
        self.assertIsNone(candidate["rules_message_id"])
        self.assertIsNone(candidate["rules_responded_at"])

    async def test_two_simultaneous_messages_reserve_only_one_rules_message(self):
        await self._join()
        results = await asyncio.gather(
            db.reserve_candidate_rules(1, -100, 101),
            db.reserve_candidate_rules(1, -100, 102),
        )
        self.assertEqual(sorted(results), [False, True])
        candidate = await db.get_candidate(1, -100)
        self.assertEqual(candidate["rules_status"], "pending")
        self.assertIn(candidate["rules_first_message_id"], {101, 102})

    async def test_send_failure_releases_reservation(self):
        await self._join()
        self.assertTrue(await db.reserve_candidate_rules(1, -100, 101))
        await db.release_candidate_rules_reservation(1, -100)
        self.assertTrue(await db.reserve_candidate_rules(1, -100, 102))

    async def test_candidates_are_independent_and_state_persists(self):
        await self._join(1)
        await self._join(2)
        await db.reserve_candidate_rules(1, -100, 101)
        await db.finish_candidate_rules_send(1, -100, 201)
        await db.answer_candidate_rules(1, -100, "accepted", 300)

        first = await db.get_candidate(1, -100)
        second = await db.get_candidate(2, -100)
        self.assertEqual(first["rules_status"], "accepted")
        self.assertEqual(first["rules_responded_at"], 300)
        self.assertEqual(second["rules_status"], "not_sent")

    async def test_answer_is_single_use(self):
        await self._join()
        await db.reserve_candidate_rules(1, -100, 101)
        await db.finish_candidate_rules_send(1, -100, 201)
        self.assertTrue(await db.answer_candidate_rules(1, -100, "accepted", 300))
        self.assertFalse(await db.answer_candidate_rules(1, -100, "declined", 301))
        self.assertEqual((await db.get_candidate(1, -100))["rules_status"], "accepted")

    async def test_resend_preserves_review_fields_and_status(self):
        await self._join(due=10_000)
        await db.postpone_candidate_review(1, -100, 20_000)
        await db.reserve_candidate_rules(1, -100, 101)
        await db.answer_candidate_rules(1, -100, "declined", 300)
        before = await db.get_candidate(1, -100)
        self.assertTrue(await db.reset_candidate_rules(1, -100))
        after = await db.get_candidate(1, -100)
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["review_due_at"], 20_000)
        self.assertEqual(after["wait_count"], 1)
        self.assertEqual(after["rules_status"], "pending")
        self.assertIsNone(after["rules_responded_at"])

    async def test_rejoin_starts_new_rules_session(self):
        await self._join()
        await db.reserve_candidate_rules(1, -100, 101)
        await db.finish_candidate_rules_send(1, -100, 201)
        await db.answer_candidate_rules(1, -100, "accepted", 300)
        await self._join(due=50_000)
        candidate = await db.get_candidate(1, -100)
        self.assertEqual(candidate["rules_status"], "not_sent")
        self.assertIsNone(candidate["rules_message_id"])
        self.assertEqual(candidate["review_due_at"], 50_000)

    async def test_schema_migrates_existing_candidates_without_data_loss(self):
        other_path = Path(self.temp_dir.name) / "old.db"
        async with aiosqlite.connect(other_path) as connection:
            await connection.executescript("""
                CREATE TABLE candidates (
                    user_id INTEGER NOT NULL, reception_chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at INTEGER NOT NULL,
                    review_due_at INTEGER NOT NULL, wait_count INTEGER NOT NULL DEFAULT 0,
                    last_buttons_msg_id INTEGER, reviewed_by INTEGER, reviewed_at INTEGER,
                    invite_link TEXT, UNIQUE(user_id, reception_chat_id)
                );
                INSERT INTO candidates VALUES (9, -100, 'wait', 1, 2, 3, NULL, NULL, NULL, NULL);
            """)
            await db._ensure_candidates_schema(connection)
            await connection.commit()
        with patch.object(db, "DB_PATH", other_path):
            candidate = await db.get_candidate(9, -100)
        self.assertEqual(candidate["status"], "wait")
        self.assertEqual(candidate["wait_count"], 3)
        self.assertEqual(candidate["rules_status"], "not_sent")


class CandidateRulesHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_member_is_not_a_failed_main_chat_check(self):
        method = GetChatMember(chat_id=invite.MAIN_CHAT_ID, user_id=10)
        for reason in ("user not found", "member not found", "user not participant"):
            bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=TelegramBadRequest(method, reason)))
            self.assertIs(await invite._is_main_member(bot, 10), False)
        for reason in ("chat not found", "PARTICIPANT_ID_INVALID", "not enough rights"):
            bot = SimpleNamespace(get_chat_member=AsyncMock(
                side_effect=TelegramBadRequest(method, reason)
            ))
            self.assertIsNone(await invite._is_main_member(bot, 10))

    async def test_startup_reconciliation_closes_only_verified_members(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch.object(invite, "list_active_candidate_ids", AsyncMock(return_value=[10, 20, 30])),
            patch.object(invite, "_is_chat_member", AsyncMock(return_value=True)),
            patch.object(invite, "_is_main_member", AsyncMock(side_effect=[True, False, None])),
            patch.object(invite, "_stop_existing_candidate", AsyncMock()) as stop,
        ):
            await invite.reconcile_reception_candidates(bot)
        stop.assert_awaited_once_with(10)
        self.assertEqual(bot.send_message.await_count, 2)  # Error and one conflict summary.

    async def test_scheduled_review_checks_main_chat_before_buttons(self):
        bot = SimpleNamespace(get_chat_member=AsyncMock(), send_message=AsyncMock())
        with (
            patch.object(db_scheduler, "get_candidate", AsyncMock(return_value={"status": "candidate"})),
            patch.object(invite, "_is_chat_member", AsyncMock(return_value=True)),
            patch.object(invite, "_is_main_member", AsyncMock(return_value=True)),
            patch.object(invite, "_stop_existing_candidate", AsyncMock()) as stop,
        ):
            await db_scheduler._handle_invite_review_due(bot, {"user_id": 10, "chat_id": invite.INVITE_CHAT_ID})
        stop.assert_awaited_once_with(10)
        bot.send_message.assert_not_awaited()

    async def test_startup_closes_departed_candidates_before_main_chat_probe(self):
        method = GetChatMember(chat_id=invite.INVITE_CHAT_ID, user_id=10)
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(side_effect=TelegramBadRequest(method, "member not found")),
            send_message=AsyncMock(),
        )
        with (
            patch.object(invite, "list_active_candidate_ids", AsyncMock(return_value=[10])),
            patch.object(invite, "_close_departed_candidate", AsyncMock()) as close,
            patch.object(invite, "_is_main_member", AsyncMock()) as main,
        ):
            await invite.reconcile_reception_candidates(bot)
        close.assert_awaited_once_with(10)
        main.assert_not_awaited()
        self.assertIn("Вже вийшли", bot.send_message.await_args.args[1])

    async def test_unknown_reception_error_preserves_candidate(self):
        method = GetChatMember(chat_id=invite.INVITE_CHAT_ID, user_id=10)
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(side_effect=TelegramBadRequest(method, "PARTICIPANT_ID_INVALID")),
            send_message=AsyncMock(),
        )
        with (
            patch.object(invite, "list_active_candidate_ids", AsyncMock(return_value=[10])),
            patch.object(invite, "_close_departed_candidate", AsyncMock()) as close,
            patch.object(invite, "_is_main_member", AsyncMock(return_value=False)),
        ):
            await invite.reconcile_reception_candidates(bot)
        close.assert_not_awaited()
        self.assertIn("Статус не змінено", bot.send_message.await_args.args[1])

    async def test_due_review_closes_absent_candidate(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch.object(db_scheduler, "get_candidate", AsyncMock(return_value={"status": "candidate"})),
            patch.object(invite, "_is_chat_member", AsyncMock(return_value=False)),
            patch.object(invite, "_is_main_member", AsyncMock()) as main,
            patch.object(invite, "_close_departed_candidate", AsyncMock()) as close,
        ):
            await db_scheduler._handle_invite_review_due(bot, {"user_id": 10, "chat_id": invite.INVITE_CHAT_ID})
        close.assert_awaited_once_with(10)
        main.assert_not_awaited()
        self.assertIsNone(bot.send_message.await_args.kwargs.get("reply_markup"))

    async def test_leave_notification_closes_candidate(self):
        message = SimpleNamespace(left_chat_member=SimpleNamespace(id=10))
        with patch.object(invite, "_close_departed_candidate", AsyncMock()) as close:
            await invite.on_candidate_leave_reception(message)
        close.assert_awaited_once_with(10)

    async def test_retry_closes_absent_candidate(self):
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), data="inv:retry:10", bot=object(), answer=AsyncMock())
        with (
            patch.object(invite, "get_admin_level", AsyncMock(return_value=2)),
            patch.object(invite, "_is_chat_member", AsyncMock(return_value=False)),
            patch.object(invite, "_close_departed_candidate", AsyncMock()) as close,
            patch.object(invite, "_admit_reception_user", AsyncMock()) as admit,
        ):
            await invite.retry_reception_check(query)
        close.assert_awaited_once_with(10)
        admit.assert_not_awaited()

    async def test_existing_member_reentering_reception_never_becomes_candidate(self):
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            send_message=AsyncMock(),
        )
        with (
            patch.object(invite, "get_admin_level", AsyncMock(return_value=1)),
            patch.object(invite, "get_candidate", AsyncMock(return_value=None)),
            patch.object(invite, "cancel_pending_tasks", AsyncMock()),
            patch.object(invite, "upsert_candidate_on_join", AsyncMock()) as upsert,
            patch.object(invite, "schedule_task", AsyncMock()) as schedule,
        ):
            await invite._admit_reception_user(bot, 10)
        upsert.assert_not_awaited()
        schedule.assert_not_awaited()
        bot.send_message.assert_awaited_once()

    async def test_unknown_membership_fails_closed_and_offers_retry(self):
        bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=RuntimeError("network")), send_message=AsyncMock())
        with (
            patch.object(invite, "get_admin_level", AsyncMock(return_value=0)),
            patch.object(invite, "upsert_candidate_on_join", AsyncMock()) as upsert,
        ):
            await invite._admit_reception_user(bot, 10)
        upsert.assert_not_awaited()
        call = bot.send_message.await_args
        self.assertEqual(call.args[0], invite.ADMIN_LOG_CHAT_ID)
        self.assertIn("inv:retry:10", call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data)

    async def test_filter_accepts_common_user_content_and_rejects_services(self):
        rule_filter = invite.CandidateFirstMessageFilter()
        with patch.object(
            invite,
            "get_candidate",
            AsyncMock(return_value={"status": "candidate", "rules_status": "not_sent"}),
        ):
            for content_type in (
                "text",
                "photo",
                "video",
                "voice",
                "video_note",
                "document",
                "sticker",
                "animation",
            ):
                message = SimpleNamespace(
                    from_user=SimpleNamespace(id=10, is_bot=False),
                    text="hello" if content_type == "text" else None,
                    content_type=content_type,
                )
                self.assertTrue(await rule_filter(message), content_type)
            service = SimpleNamespace(
                from_user=SimpleNamespace(id=10, is_bot=False),
                text=None,
                content_type="new_chat_members",
            )
            self.assertFalse(await rule_filter(service))

    async def test_first_message_sends_reply_and_later_message_does_not_duplicate(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=10), message_id=50, reply=AsyncMock()
        )
        message.reply.return_value = SimpleNamespace(message_id=60)
        with (
            patch.object(
                invite, "reserve_candidate_rules", AsyncMock(side_effect=[True, False])
            ),
            patch.object(invite, "finish_candidate_rules_send", AsyncMock()) as finish,
        ):
            await invite.send_rules_after_first_candidate_message(message)
            await invite.send_rules_after_first_candidate_message(message)
        message.reply.assert_awaited_once()
        finish.assert_awaited_once_with(10, -100, 60)

    async def test_other_user_cannot_answer_rules(self):
        query = SimpleNamespace(
            data="rules:yes:10",
            from_user=SimpleNamespace(id=11),
            message=SimpleNamespace(message_id=60),
            answer=AsyncMock(),
        )
        with patch.object(invite, "answer_candidate_rules", AsyncMock()) as answer:
            await invite.on_rules_callback(query)
        answer.assert_not_awaited()
        query.answer.assert_awaited_once_with(
            "Ці кнопки призначені кандидату.", show_alert=True
        )

    async def test_yes_and_no_persist_and_edit_without_kicking(self):
        for action, expected in (("yes", "accepted"), ("no", "declined")):
            message = SimpleNamespace(message_id=60, edit_text=AsyncMock())
            query = SimpleNamespace(
                data=f"rules:{action}:10",
                from_user=SimpleNamespace(id=10, full_name="<Fox>"),
                message=message,
                answer=AsyncMock(),
            )
            candidate = {
                "status": "candidate",
                "rules_status": "pending",
                "rules_message_id": 60,
            }
            with (
                patch.object(
                    invite, "get_candidate", AsyncMock(return_value=candidate)
                ),
                patch.object(
                    invite, "answer_candidate_rules", AsyncMock(return_value=True)
                ) as answer,
            ):
                await invite.on_rules_callback(query)
            self.assertEqual(answer.await_args.args[2], expected)
            message.edit_text.assert_awaited_once()
            self.assertIn("&lt;Fox&gt;", message.edit_text.await_args.args[0])

    async def test_accept_is_blocked_until_rules_are_accepted(self):
        for state in ("not_sent", "pending", "declined"):
            blocked = SimpleNamespace(message_id=90)
            message = SimpleNamespace(
                chat=SimpleNamespace(id=-100), answer=AsyncMock(return_value=blocked)
            )
            bot = SimpleNamespace(
                edit_message_text=AsyncMock(), create_chat_invite_link=AsyncMock(),
                get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left")),
            )
            query = SimpleNamespace(
                data="inv:accept:10",
                from_user=SimpleNamespace(id=1),
                message=message,
                bot=bot,
                answer=AsyncMock(),
            )
            candidate = {
                "status": "candidate",
                "rules_status": state,
                "rules_block_message_id": None,
            }
            with (
                patch.object(invite, "get_admin_level", AsyncMock(return_value=2)),
                patch.object(
                    invite, "get_candidate", AsyncMock(return_value=candidate)
                ),
                patch.object(invite, "set_candidate_rules_block_message", AsyncMock()),
            ):
                await invite.on_invite_callback(query)
            bot.create_chat_invite_link.assert_not_awaited()
            message.answer.assert_awaited_once()

    async def test_accept_reaches_existing_invite_flow_when_rules_accepted(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=-100), answer=AsyncMock())
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left")),
            create_chat_invite_link=AsyncMock(
                side_effect=TelegramForbiddenError(
                    method="createChatInviteLink", message="forbidden"
                )
            )
        )
        query = SimpleNamespace(
            data="inv:accept:10",
            from_user=SimpleNamespace(id=1),
            message=message,
            bot=bot,
            answer=AsyncMock(),
        )
        candidate = {
            "status": "candidate",
            "rules_status": "accepted",
            "rules_block_message_id": None,
        }
        with (
            patch.object(invite, "get_admin_level", AsyncMock(return_value=2)),
            patch.object(invite, "get_candidate", AsyncMock(return_value=candidate)),
        ):
            await invite.on_invite_callback(query)
        bot.create_chat_invite_link.assert_awaited_once()

    async def test_resend_requires_level_two(self):
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
        with patch.object(invite, "get_admin_level", AsyncMock(return_value=1)):
            await invite._resend_candidate_rules(query, 10)
        query.answer.assert_awaited_once_with(
            "Слухаюся лише адміністраторів.", show_alert=True
        )


if __name__ == "__main__":
    unittest.main()
