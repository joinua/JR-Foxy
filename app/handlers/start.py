from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router()

@router.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Привіт, я - JRツFoxy.\n"
        "Я помічник клану JokerRecon в CODM."
    )
