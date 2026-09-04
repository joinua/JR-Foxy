"""Temporary public callback boundary for event cards created in PR 2."""

from aiogram import F, Router
from aiogram.types import CallbackQuery


router = Router()


@router.callback_query(F.data.startswith("ev:r:"))
async def event_response_pending(callback: CallbackQuery) -> None:
    await callback.answer(
        "Реєстрація учасників буде активована в наступному оновленні.",
        show_alert=True,
    )
