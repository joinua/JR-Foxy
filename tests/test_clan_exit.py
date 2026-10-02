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


if __name__ == "__main__":
    unittest.main()
