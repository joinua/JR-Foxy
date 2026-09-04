import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import MessageEntityType
from aiogram.types import MessageEntity, User

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.handlers.profile import admin_commands
from app.handlers.profile import profile as profile_handler_module
from app.handlers.profile.utils import (
    has_explicit_user_reply,
    resolve_command_user_reference,
)


def user(user_id: int, name: str, username: str | None = None) -> User:
    return User(id=user_id, is_bot=False, first_name=name, username=username)


def text_mention(text: str, label: str, target: User) -> MessageEntity:
    return MessageEntity(
        type=MessageEntityType.TEXT_MENTION,
        offset=text.index(label),
        length=len(label),
        user=target,
    )


class ProfileTargetResolverTests(unittest.TestCase):
    def test_explicit_reply_to_thread_root_is_not_discarded(self):
        target = user(20, "Solomia")
        reply = SimpleNamespace(
            message_id=777,
            from_user=target,
            forum_topic_created=None,
        )
        message = SimpleNamespace(
            text="/profile",
            entities=[],
            reply_to_message=reply,
            message_thread_id=777,
            is_topic_message=False,
        )

        self.assertTrue(has_explicit_user_reply(message))
        resolved, values = resolve_command_user_reference(message)
        self.assertIs(resolved, target)
        self.assertEqual(values, [])

    def test_forum_topic_service_message_is_not_a_profile_target(self):
        reply = SimpleNamespace(
            message_id=777,
            from_user=user(20, "Solomia"),
            forum_topic_created=object(),
        )
        message = SimpleNamespace(
            text="/profile",
            entities=[],
            reply_to_message=reply,
            message_thread_id=777,
            is_topic_message=True,
        )

        resolved, values = resolve_command_user_reference(message)
        self.assertIsNone(resolved)
        self.assertEqual(values, [])

    def test_implicit_forum_root_is_not_a_profile_target(self):
        reply = SimpleNamespace(
            message_id=777,
            from_user=user(20, "Topic creator"),
            forum_topic_created=None,
        )
        message = SimpleNamespace(
            text="/profile",
            entities=[],
            reply_to_message=reply,
            message_thread_id=777,
            is_topic_message=True,
        )

        resolved, values = resolve_command_user_reference(message)
        self.assertIsNone(resolved)
        self.assertEqual(values, [])

    def test_text_mention_without_username_resolves_user_and_keeps_role(self):
        target = user(20, "Solomia", username=None)
        text = "/role Solomia Офіцер"
        message = SimpleNamespace(
            text=text,
            entities=[text_mention(text, "Solomia", target)],
            reply_to_message=None,
            message_thread_id=None,
        )

        resolved, values = resolve_command_user_reference(message)

        self.assertIs(resolved, target)
        self.assertEqual(values, ["Офіцер"])


class ProfileTargetHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_reply_in_regular_thread_opens_replied_users_profile(self):
        author = user(1, "Alex", "alex")
        target = user(20, "Solomia", username=None)
        reply = SimpleNamespace(
            message_id=777,
            from_user=target,
            forum_topic_created=None,
        )
        message = SimpleNamespace(
            from_user=author,
            text="/profile",
            entities=[],
            reply_to_message=reply,
            message_thread_id=777,
            is_topic_message=False,
            answer=AsyncMock(),
        )
        target_profile = {"user_id": target.id}

        with (
            patch.object(
                profile_handler_module.profile_service,
                "sync_telegram_user",
                AsyncMock(),
            ),
            patch.object(
                profile_handler_module.profile_service,
                "ensure_profile",
                AsyncMock(return_value=target_profile),
            ) as ensure_profile,
            patch.object(
                profile_handler_module.profile_service,
                "fill_missing_join_date",
                AsyncMock(return_value=target_profile),
            ),
            patch.object(
                profile_handler_module,
                "render_profile",
                return_value="TARGET PROFILE",
            ),
        ):
            await profile_handler_module.profile_handler(message)

        ensure_profile.assert_awaited_once_with(target)
        message.answer.assert_awaited_once_with(
            "TARGET PROFILE",
            parse_mode="HTML",
        )

    async def test_profile_text_mention_opens_mentioned_users_profile(self):
        author = user(1, "Alex", "alex")
        target = user(20, "Solomia", username=None)
        text = "/profile Solomia"
        message = SimpleNamespace(
            from_user=author,
            text=text,
            entities=[text_mention(text, "Solomia", target)],
            reply_to_message=None,
            message_thread_id=None,
            answer=AsyncMock(),
        )
        target_profile = {"user_id": target.id}

        with (
            patch.object(
                profile_handler_module.profile_service,
                "sync_telegram_user",
                AsyncMock(),
            ),
            patch.object(
                profile_handler_module.profile_service,
                "ensure_profile",
                AsyncMock(return_value=target_profile),
            ) as ensure_profile,
            patch.object(
                profile_handler_module.profile_service,
                "fill_missing_join_date",
                AsyncMock(return_value=target_profile),
            ),
            patch.object(
                profile_handler_module,
                "render_profile",
                return_value="TARGET PROFILE",
            ),
        ):
            await profile_handler_module.profile_handler(message)

        ensure_profile.assert_awaited_once_with(target)
        message.answer.assert_awaited_once_with(
            "TARGET PROFILE",
            parse_mode="HTML",
        )

    async def test_unknown_plain_profile_argument_does_not_open_author_profile(self):
        author = user(1, "Alex", "alex")
        message = SimpleNamespace(
            from_user=author,
            text="/profile Solomia",
            entities=[],
            reply_to_message=None,
            message_thread_id=None,
            answer=AsyncMock(),
        )

        with (
            patch.object(
                profile_handler_module.profile_service,
                "sync_telegram_user",
                AsyncMock(),
            ),
            patch.object(
                profile_handler_module.profile_service,
                "get_profile",
                AsyncMock(),
            ) as get_profile,
        ):
            await profile_handler_module.profile_handler(message)

        get_profile.assert_not_awaited()
        message.answer.assert_awaited_once_with(
            profile_handler_module.PROFILE_NOT_FOUND
        )

    async def test_role_accepts_text_mention_without_username(self):
        author = user(1, "Alex", "alex")
        target = user(20, "Solomia", username=None)
        text = "/role Solomia Офіцер"
        message = SimpleNamespace(
            from_user=author,
            text=text,
            entities=[text_mention(text, "Solomia", target)],
            reply_to_message=None,
            message_thread_id=None,
            answer=AsyncMock(),
        )

        with patch.object(
            admin_commands.profile_service,
            "set_role",
            AsyncMock(),
        ) as set_role:
            await admin_commands.role_handler(message)

        set_role.assert_awaited_once_with(target, "Офіцер")
        message.answer.assert_awaited_once_with(
            "Роль для Solomia змінено на Офіцер.",
            parse_mode="HTML",
        )
