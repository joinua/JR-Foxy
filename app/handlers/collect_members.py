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
    if not message.from_user:
        return

    await upsert_call_member(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        last_seen=int(time.time()),
    )

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
