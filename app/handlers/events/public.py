"""Public response callbacks for active event cards."""

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.core.config import MAIN_CHAT_ID
from app.dao import event_responses as responses_dao
from app.services.event_responses import apply_public_response


router = Router()


@router.callback_query(F.data.startswith("ev:r:"))
async def event_response_pending(callback: CallbackQuery) -> None:
    if not callback.data or not callback.message or not callback.from_user:
        return
    parts = callback.data.split(":", 3)
    if (
        len(parts) != 4
        or not parts[2].isdigit()
        or callback.message.chat.id != MAIN_CHAT_ID
    ):
        await callback.answer(
            "Стан події вже змінився. Відкрийте актуальну картку.",
            show_alert=True,
        )
        return
    event_id = int(parts[2])
    if not await responses_dao.is_current_publication(
        event_id,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
    ):
        await callback.answer(
            "Стан події вже змінився. Відкрийте актуальну картку.",
            show_alert=True,
        )
        return
    feedback = await apply_public_response(
        callback.bot,
        event_id=event_id,
        action=parts[3],
        user=callback.from_user,
    )
    await callback.answer(feedback.text, show_alert=feedback.show_alert)
