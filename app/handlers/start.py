"""Handler for /start command."""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    """Пишемо текст на нажимання кнопки /start"""

    await message.answer(
        "Привіт, я - JRツFoxy.\n"
        "Я помічничка клану JokerRecon CODM.\n"
        "Для того, щоб я тобі сказала, що я можу - відправ /help\n"
        "Але ця команда поки не працює"
    )
