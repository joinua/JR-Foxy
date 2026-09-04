"""Handler for /start command."""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    """Пишемо текст на нажимання кнопки /start"""

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip() == "reliability":
        from app.handlers.events.reliability import show_own_reliability

        await show_own_reliability(message)
        return

    await message.answer(
        "Привіт, я - JRツFoxy.\n"
        "Я помічничка клану JokerRecon CODM.\n"
        "Для того, щоб я тобі сказала, що я можу - відправ /help\n"
        "Але ця команда поки не працює"
    )
