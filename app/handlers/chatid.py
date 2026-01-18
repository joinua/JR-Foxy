from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()

@router.message(Command("chatid"))
async def chatid_handler(message: Message):
    # 1) Команда тільки для груп/супергруп
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Ця команда працює тільки в групових чатах, куди я додана.")
        return

    # 2) Перевірка, чи бот адмін у цьому чаті
    me = await message.bot.get_me()
    member = await message.bot.get_chat_member(message.chat.id, me.id)

    # статуси: administrator / creator / member / restricted / left / kicked
    if member.status not in ("administrator", "creator"):
        await message.answer("Я не адміністраторка у цьому чаті. Дай мені права адміна — тоді зможу працювати коректно.")
        return

    # 3) Віддаємо ID у форматі коду для копіювання
    chat_id = message.chat.id
    await message.answer(f"ID цього чату:\n`{chat_id}`", parse_mode="Markdown")
