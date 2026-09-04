"""Handler for displaying player profiles."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services import profile_service
from app.services import reliability_service
from app.handlers.profile.utils import (
    html_user_mention,
    render_profile,
    resolve_command_user_reference,
)

router = Router()

PROFILE_NOT_FOUND = (
    "Профіль не знайдено. Використайте команду у відповідь на повідомлення "
    "користувача, @username або клікабельну згадку Telegram."
)


@router.message(Command("profile"))
async def profile_handler(message: Message) -> None:
    if not message.from_user:
        return

    await profile_service.sync_telegram_user(message.from_user)
    target, parts = resolve_command_user_reference(message)

    if target is not None:
        if isinstance(target, int):
            profile = await profile_service.get_profile(target)
        else:
            profile = await profile_service.ensure_profile(target)
        if not profile:
            await message.answer(PROFILE_NOT_FOUND)
            return
    elif parts:
        if not parts[0].startswith("@"):
            await message.answer(PROFILE_NOT_FOUND)
            return
        profile = await profile_service.find_profile_by_username(parts[0])
        if not profile:
            await message.answer(PROFILE_NOT_FOUND)
            return
    else:
        profile = await profile_service.get_profile(message.from_user.id)
        if profile:
            profile = await profile_service.fill_missing_join_date(message.from_user.id)
        if not profile:
            mention = html_user_mention(
                message.from_user.id, message.from_user.full_name
            )
            await message.answer(
                f"{mention}, твій профіль ще не створено. "
                "Скористайся /helpprofile, щоб дізнатися, як його "
                "заповнити, або попроси допомоги у адміністрації клану.",
                parse_mode="HTML",
            )
            return

    profile = await profile_service.fill_missing_join_date(profile["user_id"]) or profile
    reliability = await reliability_service.get_summary(int(profile["user_id"]))
    await message.answer(render_profile(profile, reliability), parse_mode="HTML")
