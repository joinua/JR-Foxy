"""The `/reliability` command and its privacy boundary."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.access import get_effective_admin_level, is_admin_chat
from app.handlers.profile.utils import resolve_command_user_reference
from app.services import profile_service, reliability_service


router = Router()

PROFILE_NOT_FOUND = (
    "Профіль не знайдено. Використайте команду у відповідь на повідомлення "
    "користувача, @username, клікабельну згадку Telegram або Telegram ID."
)
FOREIGN_ACCESS_DENIED = "Ви можете переглядати лише власну статистику надійності."
ADMIN_CHAT_REQUIRED = (
    "Детальний перегляд чужої статистики доступний адміністрації рівнів 3–4 "
    "лише в адмін-чаті."
)
PRIVATE_REQUIRED = "Детальна статистика надійності доступна в приватному чаті з ботом."


async def _resolve_target(message: Message):
    target, parts = resolve_command_user_reference(message)
    if target is not None:
        return target, not parts
    if not parts:
        return message.from_user.id, True
    raw = parts[0]
    if len(parts) != 1:
        return None, False
    if raw.startswith("@"):
        profile = await profile_service.find_profile_by_username(raw)
        return (int(profile["user_id"]) if profile else None), bool(profile)
    if raw.isdigit():
        return int(raw), True
    return None, False


async def _private_button(message: Message) -> InlineKeyboardMarkup:
    bot_user = await message.bot.get_me()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Відкрити мою статистику",
                    url=f"https://t.me/{bot_user.username}?start=reliability",
                )
            ]
        ]
    )


async def show_own_reliability(message: Message) -> None:
    """Render the caller's details; used by `/reliability` and the deep link."""

    if not message.from_user:
        return
    if message.chat.type != "private":
        await message.answer(
            PRIVATE_REQUIRED,
            reply_markup=await _private_button(message),
        )
        return
    await profile_service.sync_telegram_user(message.from_user)
    profile = await profile_service.get_profile(message.from_user.id)
    if not profile:
        await message.answer(PROFILE_NOT_FOUND)
        return
    summary = await reliability_service.get_summary(
        message.from_user.id,
        include_recent=True,
    )
    nickname = (
        profile.get("game_nickname")
        or profile.get("telegram_full_name")
        or profile.get("telegram_username")
        or str(profile["user_id"])
    )
    await message.answer(
        reliability_service.render_details(summary, str(nickname)),
        parse_mode="HTML",
    )


@router.message(Command("reliability"))
async def reliability_handler(message: Message) -> None:
    if not message.from_user:
        return
    await profile_service.sync_telegram_user(message.from_user)
    target, valid = await _resolve_target(message)
    if not valid or target is None:
        await message.answer(PROFILE_NOT_FOUND)
        return
    target_id = int(target if isinstance(target, int) else target.id)
    own = target_id == message.from_user.id
    level = await get_effective_admin_level(message.from_user.id)
    in_admin_chat = is_admin_chat(message.chat.id)

    if not own and not in_admin_chat:
        await message.answer(ADMIN_CHAT_REQUIRED if level >= 3 else FOREIGN_ACCESS_DENIED)
        return
    if not own and level < 3:
        await message.answer(FOREIGN_ACCESS_DENIED)
        return
    if own and message.chat.type != "private" and not (in_admin_chat and level >= 3):
        await message.answer(
            PRIVATE_REQUIRED,
            reply_markup=await _private_button(message),
        )
        return

    if isinstance(target, int):
        profile = await profile_service.get_profile(target_id)
    else:
        profile = await profile_service.ensure_profile(target)
    if not profile:
        await message.answer(PROFILE_NOT_FOUND)
        return
    summary = await reliability_service.get_summary(target_id, include_recent=True)
    nickname = (
        profile.get("game_nickname")
        or (summary.recent[0].get("nickname_snapshot") if summary.recent else None)
        or profile.get("telegram_full_name")
        or profile.get("telegram_username")
        or str(target_id)
    )
    await message.answer(
        reliability_service.render_details(summary, str(nickname)),
        parse_mode="HTML",
    )
