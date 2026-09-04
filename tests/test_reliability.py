import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiogram.types import User

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.core import db
from app.dao import reliability as reliability_dao
from app.dao import event_reviews
from app.handlers.events import reliability as reliability_handler
from app.handlers.profile.utils import render_profile
from app.services import reliability_service


def rows(*results: str) -> list[dict]:
    return [{"result": result} for result in results]


class ReliabilityFormulaTests(unittest.TestCase):
    def test_formula_uses_half_miss_and_half_up_rounding(self):
        summary = reliability_service.calculate_summary(
            10,
            rows("present", *("no_show" for _ in range(7))),
        )
        self.assertEqual(summary.percentage, 13)
        self.assertEqual(summary.zone, "red")

        late = reliability_service.calculate_summary(
            10,
            rows("present", "late_decline", "late_decline"),
        )
        self.assertEqual(late.percentage, 50)

    def test_gray_zone_applies_until_three_evaluated_events(self):
        summary = reliability_service.calculate_summary(
            10,
            rows("present", "present"),
        )
        self.assertEqual(summary.percentage, 100)
        self.assertFalse(summary.qualified)
        self.assertEqual(summary.zone, "gray")
        self.assertEqual(
            reliability_service.render_profile_line(summary),
            "⚪ Надійність: недостатньо даних · 2 із 3 подій",
        )

    def test_zone_boundaries_are_inclusive(self):
        green = reliability_service.calculate_summary(
            10,
            rows(*("present" for _ in range(7)), *("no_show" for _ in range(3))),
        )
        yellow = reliability_service.calculate_summary(
            10,
            rows("present", "present", "no_show", "no_show", "no_show"),
        )
        red = reliability_service.calculate_summary(
            10,
            rows("present", "no_show", "no_show"),
        )
        self.assertEqual((green.percentage, green.zone), (70, "green"))
        self.assertEqual((yellow.percentage, yellow.zone), (40, "yellow"))
        self.assertEqual((red.percentage, red.zone), (33, "red"))

        rounded_green = reliability_service.calculate_summary(
            10,
            rows(
                *("present" for _ in range(8)),
                *("no_show" for _ in range(3)),
                "late_decline",
            ),
        )
        self.assertEqual((rounded_green.percentage, rounded_green.zone), (70, "green"))

    def test_only_first_twelve_rows_are_used(self):
        summary = reliability_service.calculate_summary(
            10,
            rows(*("present" for _ in range(12)), "no_show"),
        )
        self.assertEqual(summary.evaluated_count, 12)
        self.assertEqual(summary.no_show, 0)
        self.assertEqual(summary.percentage, 100)

    def test_profile_contains_only_short_reliability_line(self):
        summary = reliability_service.calculate_summary(
            10,
            rows("present", "present", "no_show"),
        )
        profile = {
            "user_id": 10,
            "telegram_full_name": "Player",
            "telegram_username": None,
            "game_nickname": "JRঐPlayer",
            "codm_uid": None,
            "birthday": None,
            "join_date": None,
            "role": "Боєць",
        }
        rendered = render_profile(profile, summary)
        self.assertIn("🟡 Надійність: 67%", rendered)
        self.assertNotIn("Неявки:", rendered)


class ReliabilityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "reliability.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        await db.init_db()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def _result(
        self,
        event_id: int,
        result: str,
        *,
        status: str = "completed",
        reason: str | None = None,
    ) -> None:
        start = 1_000_000 + event_id * 60
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO events (
                    id, title, event_type, starts_at_utc, safe_until_utc,
                    registration_closes_at_utc, status, created_by,
                    created_at, updated_at, finalized_at
                ) VALUES (?, ?, 'clan', ?, ?, ?, ?, 1, 1, 1, 2)
                """,
                (event_id, f"Подія {event_id}", start, start - 7200, start - 3600, status),
            )
            await connection.execute(
                """
                INSERT INTO event_results (
                    event_id, user_id, result, exclusion_reason,
                    nickname_snapshot, source, marked_at, finalized_at
                ) VALUES (?, 10, ?, ?, 'JRঐSnapshot', 'admin', 1, 2)
                """,
                (event_id, result, reason),
            )
            await connection.commit()

    async def test_window_skips_neutral_cancelled_and_annulled_events(self):
        for event_id in range(1, 14):
            await self._result(event_id, "present" if event_id > 1 else "no_show")
        await self._result(20, "excluded", reason="Поважна причина")
        await self._result(21, "no_show", status="cancelled")
        await self._result(22, "no_show", status="annulled")

        rating = await reliability_dao.get_rating_rows(10)
        recent = await reliability_dao.get_recent_rows(10)

        self.assertEqual(len(rating), 12)
        self.assertNotIn(1, {row["event_id"] for row in rating})
        self.assertNotIn(20, {row["event_id"] for row in rating})
        self.assertEqual(recent[0]["event_id"], 20)
        self.assertNotIn(21, {row["event_id"] for row in recent})
        self.assertNotIn(22, {row["event_id"] for row in recent})

    async def test_correction_is_reflected_without_stored_rating(self):
        await self._result(1, "present")
        await self._result(2, "present")
        await self._result(3, "present")
        before = await reliability_service.get_summary(10)
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                "UPDATE event_results SET result='no_show' WHERE event_id=3 AND user_id=10"
            )
            await connection.commit()
        after = await reliability_service.get_summary(10)
        self.assertEqual(before.percentage, 100)
        self.assertEqual(after.percentage, 67)

    async def test_result_counts_only_after_finalize_and_disappears_after_annulment(self):
        start = 1_500_000
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                """
                INSERT INTO events (
                    title, event_type, starts_at_utc, safe_until_utc,
                    registration_closes_at_utc, status, created_by,
                    created_at, updated_at, review_created_at
                ) VALUES ('Інтеграційна подія', 'clan', ?, ?, ?,
                          'awaiting_review', 1, 1, 1, 2)
                """,
                (start, start - 7200, start - 3600),
            )
            event_id = int(cursor.lastrowid)
            await connection.execute(
                """
                INSERT INTO event_responses (
                    event_id, user_id, status, nickname_snapshot,
                    joined_at, responded_at, updated_at
                ) VALUES (?, 10, 'going', 'JRঐPlayer', 1, 1, 1)
                """,
                (event_id,),
            )
            await connection.execute(
                """
                INSERT INTO event_results (
                    event_id, user_id, result, nickname_snapshot,
                    source, marked_by, marked_at
                ) VALUES (?, 10, 'present', 'JRঐPlayer', 'admin', 30, 2)
                """,
                (event_id,),
            )
            await connection.commit()

        before = await reliability_service.get_summary(10)
        await event_reviews.request_or_finalize(event_id, actor_id=30, now=3)
        await event_reviews.request_or_finalize(event_id, actor_id=30, now=4)
        finalized = await reliability_service.get_summary(10)
        await event_reviews.annul_event(
            event_id,
            actor_id=1,
            reason="Результат події анульовано",
            now=5,
        )
        annulled = await reliability_service.get_summary(10)

        self.assertEqual(before.evaluated_count, 0)
        self.assertEqual(finalized.evaluated_count, 1)
        self.assertEqual(finalized.percentage, 100)
        self.assertEqual(annulled.evaluated_count, 0)

    async def test_details_show_five_recent_events_and_neutral_reason(self):
        for event_id in range(1, 7):
            result = "excluded" if event_id == 6 else "present"
            await self._result(
                event_id,
                result,
                reason="Службове виключення" if result == "excluded" else None,
            )
        summary = await reliability_service.get_summary(10, include_recent=True)
        rendered = reliability_service.render_details(summary, "JRঐ<Player>")
        self.assertEqual(len(summary.recent), 5)
        self.assertIn("Причина: Службове виключення", rendered)
        self.assertIn("JRঐ&lt;Player&gt;", rendered)

    async def test_profile_status_does_not_reset_history_bound_to_telegram_id(self):
        await self._result(1, "present")
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                """
                INSERT INTO profiles (
                    user_id, game_nickname, status, created_at, updated_at
                ) VALUES (10, 'JRঐPlayer', 'archived', 'now', 'now')
                """
            )
            await connection.commit()
        archived = await reliability_service.get_summary(10)
        async with aiosqlite.connect(self.db_path) as connection:
            await connection.execute(
                "UPDATE profiles SET status='active' WHERE user_id=10"
            )
            await connection.commit()
        returned = await reliability_service.get_summary(10)
        self.assertEqual(archived, returned)


def telegram_user(user_id: int, name: str = "Player") -> User:
    return User(id=user_id, is_bot=False, first_name=name)


def message_for(
    *,
    actor_id: int,
    text: str,
    chat_id: int,
    chat_type: str,
    reply_user: User | None = None,
):
    reply = (
        SimpleNamespace(
            message_id=90,
            from_user=reply_user,
            forum_topic_created=None,
        )
        if reply_user
        else None
    )
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="JR_Foxy_bot"))
    )
    return SimpleNamespace(
        from_user=telegram_user(actor_id),
        text=text,
        entities=[],
        reply_to_message=reply,
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        bot=bot,
        answer=AsyncMock(),
    )


class ReliabilityAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_payload_cannot_publish_details_in_group(self):
        message = message_for(
            actor_id=10,
            text="/start reliability",
            chat_id=-1002551613807,
            chat_type="supergroup",
        )
        with patch.object(
            reliability_handler.reliability_service,
            "get_summary",
            AsyncMock(),
        ) as summary:
            await reliability_handler.show_own_reliability(message)
        summary.assert_not_awaited()
        self.assertEqual(
            message.answer.await_args.args[0],
            reliability_handler.PRIVATE_REQUIRED,
        )

    async def test_own_details_in_group_are_redirected_to_private_chat(self):
        message = message_for(
            actor_id=10,
            text="/reliability",
            chat_id=-1002551613807,
            chat_type="supergroup",
        )
        with (
            patch.object(reliability_handler.profile_service, "sync_telegram_user", AsyncMock()),
            patch.object(reliability_handler, "get_effective_admin_level", AsyncMock(return_value=0)),
            patch.object(reliability_handler.reliability_service, "get_summary", AsyncMock()) as summary,
        ):
            await reliability_handler.reliability_handler(message)
        summary.assert_not_awaited()
        kwargs = message.answer.await_args.kwargs
        self.assertEqual(message.answer.await_args.args[0], reliability_handler.PRIVATE_REQUIRED)
        self.assertIn("start=reliability", kwargs["reply_markup"].inline_keyboard[0][0].url)

    async def test_foreign_details_require_level_three_and_admin_chat(self):
        target = telegram_user(20, "Target")
        outside = message_for(
            actor_id=30, text="/reliability", chat_id=-1002551613807,
            chat_type="supergroup", reply_user=target,
        )
        with (
            patch.object(reliability_handler.profile_service, "sync_telegram_user", AsyncMock()),
            patch.object(reliability_handler, "get_effective_admin_level", AsyncMock(return_value=3)),
        ):
            await reliability_handler.reliability_handler(outside)
        outside.answer.assert_awaited_once_with(reliability_handler.ADMIN_CHAT_REQUIRED)

        low_level = message_for(
            actor_id=30, text="/reliability", chat_id=-200,
            chat_type="supergroup", reply_user=target,
        )
        with (
            patch.object(reliability_handler.profile_service, "sync_telegram_user", AsyncMock()),
            patch.object(reliability_handler, "get_effective_admin_level", AsyncMock(return_value=2)),
        ):
            await reliability_handler.reliability_handler(low_level)
        low_level.answer.assert_awaited_once_with(reliability_handler.FOREIGN_ACCESS_DENIED)

    async def test_level_three_can_view_foreign_profile_in_admin_chat(self):
        target = telegram_user(20, "Target")
        message = message_for(
            actor_id=30, text="/reliability", chat_id=-200,
            chat_type="supergroup", reply_user=target,
        )
        profile = {"user_id": 20, "game_nickname": "JRঐTarget"}
        summary = reliability_service.calculate_summary(
            20, rows("present", "present", "present"), []
        )
        with (
            patch.object(reliability_handler.profile_service, "sync_telegram_user", AsyncMock()),
            patch.object(reliability_handler.profile_service, "ensure_profile", AsyncMock(return_value=profile)),
            patch.object(reliability_handler, "get_effective_admin_level", AsyncMock(return_value=3)),
            patch.object(reliability_handler.reliability_service, "get_summary", AsyncMock(return_value=summary)),
        ):
            await reliability_handler.reliability_handler(message)
        rendered = message.answer.await_args.args[0]
        self.assertIn("JRঐTarget", rendered)
        self.assertIn("🟢 100%", rendered)
