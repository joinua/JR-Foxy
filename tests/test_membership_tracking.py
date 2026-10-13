"""Membership reconciliation uses only verified main-chat evidence."""
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
from app.dao import clan_exit as exit_dao, profile_dao
from app.services import member_tracking, profile_service


def user(uid):
    return SimpleNamespace(id=uid, username="fighter", first_name="Боєць",
                           last_name=None, is_bot=False)


class MemberReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "test.db"
        for module in (db, exit_dao, profile_dao, member_tracking):
            p = patch.object(module, "DB_PATH", path)
            p.start()
            self.addCleanup(p.stop)
        await db.init_db()
        self.bot = AsyncMock()
        self.bot.get_me.return_value = SimpleNamespace(id=999)

    async def test_missing_exit_is_recovered_once_for_observed_member(self):
        await profile_service.sync_telegram_user(user(42), create=True)
        await member_tracking.observe_main_member(42)
        self.bot.get_chat_member.return_value = SimpleNamespace(
            status="left", user=user(42))
        with patch.object(member_tracking.logger, "warning"), patch(
            "app.services.clan_exit.record_exit", new_callable=AsyncMock
        ) as record:
            await member_tracking.reconcile_members(self.bot)
            await member_tracking.reconcile_members(self.bot)
        self.assertEqual((await profile_service.get_profile(42))["status"], "archived")
        record.assert_awaited_once()

    async def test_reception_only_profile_is_not_treated_as_exit(self):
        await profile_service.sync_telegram_user(user(43), create=True)
        self.bot.get_chat_member.return_value = SimpleNamespace(
            status="left", user=user(43))
        with patch("app.services.clan_exit.record_exit",
                   new_callable=AsyncMock) as record:
            await member_tracking.reconcile_members(self.bot)
        self.assertEqual((await profile_service.get_profile(43))["status"], "active")
        record.assert_not_awaited()

    async def test_api_error_does_not_archive_verified_member(self):
        await profile_service.sync_telegram_user(user(44), create=True)
        await member_tracking.observe_main_member(44)
        self.bot.get_chat_member.side_effect = RuntimeError("PARTICIPANT_ID_INVALID")
        await member_tracking.reconcile_members(self.bot)
        self.assertEqual((await profile_service.get_profile(44))["status"], "active")

    async def test_verified_join_is_added_to_observed_roster(self):
        await db.ensure_clan_member(45, 100)
        self.bot.get_chat_member.return_value = SimpleNamespace(
            status="member", user=user(45))
        await member_tracking.reconcile_members(self.bot)
        self.assertEqual((await profile_service.get_profile(45))["status"], "active")
        self.assertIn((45, True), await member_tracking.known_member_ids())


if __name__ == "__main__":
    unittest.main()
