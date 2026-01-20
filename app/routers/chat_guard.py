from aiogram import Router
from aiogram.types import ChatMemberUpdated

from app.core.config import ALLOWED_CHATS, WRONG_CHAT_TEXT


router = Router()

@router.my_chat_member()
async def on_bot_added(event: ChatMemberUpdated):
    chat = event.chat

    # Тільки групи/супергрупи
    if chat.type not in ("group", "supergroup"):
        return

    # Перевіряємо, що зміна статусу стосується саме бота
    if event.new_chat_member.user.id != event.bot.id:
        return

    new_status = event.new_chat_member.status

    # Статуси, які означають що бота додали/повернули
    if new_status in ("member", "administrator"):
        if chat.id not in ALLOWED_CHATS:
            try:
                await event.bot.send_message(chat.id, WRONG_CHAT_TEXT)
            finally:
                try:
                    await event.bot.leave_chat(chat.id)
                except Exception:
                    pass
