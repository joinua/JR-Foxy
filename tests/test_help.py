"""Role boundaries and public command menu for the help release."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.handlers import help as help_handler


class HelpTests(unittest.IsolatedAsyncioTestCase):
    def test_public_command_list_excludes_start_and_staff(self):
        self.assertEqual(
            [item.command for item in help_handler.PUBLIC_COMMANDS],
            ["help", "helpprofile", "profile", "reliability", "predict"],
        )

    def test_help_displays_only_inherited_role_menus(self):
        for level, expected in [(0, []), (1, ["officer"]), (2, ["officer", "moder"]), (3, ["officer", "moder", "admin"])]:
            rendered = help_handler.render_help(level)
            for menu in ("officer", "moder", "admin"):
                self.assertEqual(f"/help_{menu}" in rendered, menu in expected)
        self.assertNotIn("/tiktok_check", help_handler.ADMIN_HELP)
        self.assertIn("/tiktok_check", help_handler.LEADER_HELP)

    async def test_admin_help_hides_leader_commands_from_deputy(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=30))
        with (
            patch.object(help_handler, "_level", AsyncMock(return_value=3)),
            patch.object(help_handler, "answer_help", AsyncMock()) as answer,
        ):
            await help_handler.help_admin_handler(message)
        self.assertIn("/event", answer.await_args.args[1])
        self.assertNotIn("/dela", answer.await_args.args[1])

    async def test_help_in_group_is_scheduled_for_deletion(self):
        sent = SimpleNamespace(delete=AsyncMock())
        message = SimpleNamespace(chat=SimpleNamespace(type="supergroup"), answer=AsyncMock(return_value=sent))
        with patch.object(help_handler.asyncio, "create_task") as create_task:
            await help_handler.answer_help(message, "test")
        self.assertEqual(create_task.call_count, 1)
        create_task.call_args.args[0].close()
