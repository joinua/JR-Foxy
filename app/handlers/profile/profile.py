"""Handler for displaying player profiles."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services import profile_service
from app.handlers.profile.utils import render_profile

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
        if not profile:
            await message.answer(
                "Ваш профіль ще не створено. Скористайтеся /helpprofile, "
                "щоб дізнатися, як його заповнити."
            )
            return

    await message.answer(render_profile(profile), parse_mode="HTML")
