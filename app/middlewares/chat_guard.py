from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery

from app.core.config import ALLOWED_CHATS, WRONG_CHAT_TEXT


class ChatGuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        chat = getattr(event, "chat", None)
        if chat is None and isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat

        # Працюємо тільки з group/supergroup
        if chat and chat.type in ("group", "supergroup"):
            if chat.id not in ALLOWED_CHATS:
                try:
                    # Коректна відповідь для різних типів апдейтів
                    if isinstance(event, Message):
                        await event.answer(WRONG_CHAT_TEXT)
                    elif isinstance(event, CallbackQuery) and event.message:
                        await event.answer(WRONG_CHAT_TEXT, show_alert=True)
                finally:
                    # Вихід з чату
                    bot = data["bot"]
                    try:
                        await bot.leave_chat(chat.id)
                    except Exception:
                        pass

                return  # стопимо обробку апдейту

        return await handler(event, data)
