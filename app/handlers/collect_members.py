import time
from aiogram import F, Router
from aiogram.types import Message

from app.core.db import upsert_call_member

router = Router()


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    ~F.text.regexp(r"^[!/]\w+"),
)
async def collect_member(message: Message) -> None:
    # Працюємо тільки в групах/супергрупах
    if message.chat.type not in ("group", "supergroup"):
        return

    user = message.from_user
    if not user or user.is_bot:
        return

    await upsert_call_member(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        last_seen=int(time.time()),
    )
