"""Облік активності для щоденного рейтингу балакунів."""

from aiogram import F, Router
from aiogram.types import Message

from app.core.config import FAMILY_CHAT_ID
from app.core.db import increment_daily_talk_activity
from app.services.talktop import today_kyiv

router = Router()


@router.message(F.chat.id == FAMILY_CHAT_ID, ~F.text.regexp(r"^[!/]\w+"))
async def count_family_activity(message: Message) -> None:
    user = message.from_user
    if not user or user.is_bot:
        return
    if not any(
        (
            message.text, message.caption, message.photo, message.video, message.animation,
            message.audio, message.document, message.sticker, message.voice,
            message.video_note, message.contact, message.location, message.venue,
            message.poll, message.dice, message.game,
        )
    ):
        return
    full_name = " ".join(part for part in (user.first_name, user.last_name) if part).strip() or None
    await increment_daily_talk_activity(
        FAMILY_CHAT_ID,
        user.id,
        today_kyiv().isoformat(),
        user.username,
        full_name,
    )
