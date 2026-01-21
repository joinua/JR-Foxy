from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.core.command_registry import register_command

router = Router()

register_command("start", "Показати стартове меню/підказки", 0, "both")


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привіт, я - JRツFoxy.\n"
        "Я помічничка клану JokerRecon CODM.\n"
        "Для того, щоб я тобі сказала, що я можу - відправ /help"
    )
