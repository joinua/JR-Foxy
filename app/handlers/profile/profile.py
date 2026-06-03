"""Handler for displaying player profiles."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services import profile_service
from app.handlers.profile.utils import html_user_mention, render_profile

router = Router()

PROFILE_NOT_FOUND = (
    "Профіль не знайдено. Спробуйте використати команду у відповідь "
    "на повідомлення користувача."
)


@router.message(Command("profile"))
async def profile_handler(message: Message) -> None:
    if not message.from_user:
        return

    await profile_service.sync_telegram_user(message.from_user)
    parts = message.text.split() if message.text else []

    if message.reply_to_message and message.reply_to_message.from_user:
        profile = await profile_service.ensure_profile(message.reply_to_message.from_user)
    elif len(parts) > 1 and parts[1].startswith("@"):
        profile = await profile_service.find_profile_by_username(parts[1])
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
    await message.answer(render_profile(profile), parse_mode="HTML")
