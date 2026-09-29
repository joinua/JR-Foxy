import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.handlers.profile import admin_commands


def member(user_id, name, *, username=None, is_bot=False, status="member"):
    return SimpleNamespace(
        status=status,
        user=SimpleNamespace(
            id=user_id,
            username=username,
            first_name=name,
            last_name=None,
            is_bot=is_bot,
        ),
    )


class ProfileAuditTests(unittest.IsolatedAsyncioTestCase):
    def test_current_telegram_identity_is_clickable_even_with_game_nickname(self):
        text = admin_commands._render_profile_audit(
            [{
                "user_id": 42,
                "game_nickname": "JRঐDONbass",
                "telegram_full_name": "Old name",
                "call_first_name": "Поточне ім’я",
                "call_last_name": None,
                "missing_fields": ["Дата вступу"],
            }],
            total_members=6,
            completed_profiles=1,
        )
        self.assertIn('<a href="tg://user?id=42">Поточне ім’я</a>', text)
        self.assertNotIn("JRঐDONbass", text)
        self.assertIn("Всього в клані: <b>6 учасників</b>", text)
        self.assertIn("Заповнені профілі з них у: <b>1 учасника</b>", text)

    def test_totals_remain_visible_when_no_profiles_are_missing(self):
        text = admin_commands._render_profile_audit(
            [], total_members=4, completed_profiles=4
        )
        self.assertIn("Серед перевірених профілів незаповнених немає", text)
        self.assertIn("Всього в клані: <b>4 учасники</b>", text)
        self.assertIn("Заповнені профілі з них у: <b>4 учасників</b>", text)

    async def test_handler_counts_filled_profiles_and_excludes_foxy(self):
        complete = {
            "user_id": 20,
            "game_nickname": "JRঐOne",
            "codm_uid": "1234567890123456789",
            "birthday": "2000-01-01",
            "join_date": "2024-01-01",
        }
        incomplete = {"user_id": 30, "telegram_full_name": "Two"}
        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(type="private", id=1),
            answer=AsyncMock(return_value=status),
        )
        with (
            patch.object(admin_commands, "_effective_admin_level", AsyncMock(return_value=4)),
            patch.object(
                admin_commands.profile_service,
                "list_profile_audit_candidates",
                AsyncMock(return_value=[complete, incomplete]),
            ),
            patch.object(
                admin_commands,
                "_refresh_profile_audit_rows",
                AsyncMock(return_value=[complete, incomplete]),
            ),
            patch.object(
                admin_commands.bot,
                "get_chat_member_count",
                AsyncMock(return_value=7),
            ) as count_members,
        ):
            await admin_commands.profile_audit_handler(message)

        count_members.assert_awaited_once_with(admin_commands.MAIN_CHAT_ID)
        rendered = status.edit_text.await_args.args[0]
        self.assertIn("Всього в клані: <b>6 учасників</b>", rendered)
        self.assertIn("Заповнені профілі з них у: <b>1 учасника</b>", rendered)

    async def test_only_verified_humans_in_main_chat_are_shown(self):
        rows = [
            {"user_id": 123},  # The running bot itself.
            {"user_id": 20, "telegram_full_name": "Old name"},
            {"user_id": 30, "telegram_full_name": "Another bot"},
            {"user_id": 40, "telegram_full_name": "Former member"},
        ]
        responses = {
            20: member(20, "Current", username="current"),
            30: member(30, "Another bot", is_bot=True),
            40: member(40, "Former member", status="left"),
        }

        async def get_member(chat_id, user_id):
            self.assertEqual(chat_id, admin_commands.MAIN_CHAT_ID)
            return responses[user_id]

        with (
            patch.object(admin_commands.bot, "get_chat_member", side_effect=get_member),
            patch.object(
                admin_commands.profile_service,
                "sync_telegram_user",
                AsyncMock(return_value=None),
            ),
            patch.object(
                admin_commands.profile_service,
                "fill_missing_join_date",
                AsyncMock(return_value=None),
            ),
            patch.object(
                admin_commands.profile_service,
                "archive_profile",
                AsyncMock(),
            ) as archive,
        ):
            result = await admin_commands._refresh_profile_audit_rows(rows)

        self.assertEqual([row["user_id"] for row in result], [20])
        self.assertEqual(result[0]["call_username"], "current")
        archive.assert_awaited_once_with(40)

    async def test_failed_membership_check_does_not_show_stale_snapshot(self):
        with patch.object(
            admin_commands.bot,
            "get_chat_member",
            AsyncMock(side_effect=admin_commands.TelegramForbiddenError(
                method=admin_commands.bot.get_chat_member,
                message="Forbidden",
            )),
        ):
            result = await admin_commands._refresh_profile_audit_rows(
                [{"user_id": 20, "telegram_full_name": "Stale name"}]
            )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
