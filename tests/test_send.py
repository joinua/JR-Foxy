"""Announcement publication must not resend to a chat after partial success."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.handlers import broadcast


class FakeState:
    def __init__(self, data):
        self.data = data

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.data.clear()


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_delivery_retries_only_failed_destination(self):
        data = {"chat_id": 10, "panel_id": 15, "choice": "both", "text": "Вітаю", "previewed": True, "sent": []}
        state = FakeState(data)
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        bot.send_message.side_effect = [SimpleNamespace(message_id=100), RuntimeError("unavailable"), SimpleNamespace(message_id=200), SimpleNamespace(message_id=300)]
        query = SimpleNamespace(
            data="send:publish", from_user=SimpleNamespace(id=3, full_name="Admin"),
            message=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=15, edit_text=AsyncMock()),
            bot=bot, answer=AsyncMock(),
        )
        with patch.object(broadcast, "has_admin_level", AsyncMock(return_value=True)):
            await broadcast.send_callback(query, state)
            self.assertEqual(state.data["sent"], ["main"])
            await broadcast.send_callback(query, state)
        destinations = [call.args[0] for call in bot.send_message.await_args_list]
        self.assertEqual(destinations[:3], [broadcast.MAIN_CHAT_ID, broadcast.FAMILY_CHAT_ID, broadcast.FAMILY_CHAT_ID])
        self.assertEqual(destinations.count(broadcast.MAIN_CHAT_ID), 1)
        self.assertEqual(state.data, {})

    async def test_photo_requires_short_caption(self):
        state = FakeState({"chat_id": 10, "panel_id": 15, "text": "x" * 1025})
        message = SimpleNamespace(photo=[SimpleNamespace(file_id="photo")], answer=AsyncMock(), bot=SimpleNamespace(edit_message_text=AsyncMock()))
        await broadcast.input_photo(message, state)
        self.assertNotIn("photo", state.data)
        self.assertIn("1024", message.bot.edit_message_text.await_args.args[0])
