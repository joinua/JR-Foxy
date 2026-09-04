"""Shared attendance review callbacks for level 3–4 administrators."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.core.access import can_manage_events, get_effective_admin_level
from app.dao import event_reviews as reviews_dao
from app.handlers.events.keyboards import correction_result_keyboard, review_keyboard
from app.handlers.events.states import EventReviewInput
from app.services import event_service
from app.services.event_reviews import (
    finalize_review,
    publish_cancellation,
    refresh_review_message,
    render_review,
)


router = Router()
ACCESS_DENIED = "Перевірка доступна лише адміністрації рівня 3–4 в адмін-чаті."


async def _allowed(callback: CallbackQuery) -> bool:
    if (
        not callback.from_user
        or not callback.message
        or not await can_manage_events(callback.from_user.id, callback.message.chat.id)
    ):
        await callback.answer(ACCESS_DENIED, show_alert=True)
        return False
    return True


@router.callback_query(F.data.startswith("ev:v:"))
async def event_review_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.message or not await _allowed(callback):
        return
    parts = callback.data.split(":")
    if len(parts) < 4 or not parts[2].isdigit():
        await callback.answer("Некоректна дія.", show_alert=True)
        return
    event_id = int(parts[2])
    action = parts[3]
    actor_id = callback.from_user.id

    if action == "noop":
        await callback.answer()
        return
    if action == "open" and len(parts) == 5:
        await state.clear()
        await refresh_review_message(callback.message, event_id, int(parts[4]))
        await callback.answer()
        return
    if action == "set" and len(parts) == 7:
        user_id, result, page = int(parts[4]), parts[5], int(parts[6])
        code = await reviews_dao.set_result(
            event_id, user_id, result,
            actor_id=actor_id, now=event_service.now_timestamp(),
        )
        if code != "updated":
            await callback.answer("Перевірку вже завершено або гравця не знайдено.", show_alert=True)
            return
        await refresh_review_message(callback.message, event_id, page)
        await callback.answer("Результат оновлено.")
        return
    if action == "exclude" and len(parts) == 6:
        user_id, page = int(parts[4]), int(parts[5])
        review = await reviews_dao.get_review(event_id, page=page)
        player = next((row for row in (review or {}).get("players", []) if int(row["user_id"]) == user_id), None)
        if not player or review["status"] != "awaiting_review":
            await callback.answer("Перевірку вже завершено або гравця не знайдено.", show_alert=True)
            return
        await state.set_state(EventReviewInput.exclusion_reason)
        await state.update_data(
            event_id=event_id, user_id=user_id, page=page,
            actor_id=actor_id, review_chat_id=callback.message.chat.id,
            review_message_id=callback.message.message_id,
        )
        await callback.message.edit_text(
            "Надішліть коротке пояснення, чому результат "
            f"{escape(str(player['nickname']))} не потрібно враховувати — "
            "від 3 до 300 символів."
        )
        await callback.answer()
        return
    if action == "finalize" and len(parts) == 5:
        page = int(parts[4])
        result = await finalize_review(
            callback.bot, event_id, actor_id, now=event_service.now_timestamp()
        )
        if result.code == "incomplete":
            await callback.answer(
                f"Неможливо завершити перевірку. Не оцінено гравців: {result.total}.",
                show_alert=True,
            )
            return
        if result.code == "confirm":
            review = await reviews_dao.get_review(event_id)
            counts = (review or {}).get("counts", {})
            await callback.answer(
                "Перевірте підсумок: "
                f"✅ {counts.get('present', 0)}, ❌ {counts.get('no_show', 0)}, "
                f"🕒 {counts.get('late_decline', 0)}, ➖ {counts.get('excluded', 0)}. "
                f"Рейтинги зміняться для {result.total - counts.get('excluded', 0)} гравців. "
                "Натисніть «Завершити перевірку» ще раз.",
                show_alert=True,
            )
            return
        if result.code == "finalized":
            await refresh_review_message(callback.message, event_id, page)
            await callback.answer("Перевірку завершено. Результати збережено.", show_alert=True)
            return
        await callback.answer("Перевірка вже недоступна.", show_alert=True)
        return
    if action == "correct" and len(parts) == 6:
        user_id, page = int(parts[4]), int(parts[5])
        review = await reviews_dao.get_review(event_id, page=page)
        if not review or review["status"] != "completed":
            await callback.answer("Корекція вже недоступна.", show_alert=True)
            return
        level = await get_effective_admin_level(actor_id)
        now = event_service.now_timestamp()
        if level < 4 and now > int(review["finalized_at"] or 0) + 24 * 3600:
            await callback.answer(
                "24 години після фіналізації минули. Подальша корекція доступна лише адміністрації рівня 4.",
                show_alert=True,
            )
            return
        await callback.message.edit_text(
            "Оберіть новий результат. Після цього бот попросить обов’язкову причину.",
            reply_markup=correction_result_keyboard(event_id, user_id, page),
        )
        await callback.answer()
        return
    if action == "correct_result" and len(parts) == 7:
        user_id, result, page = int(parts[4]), parts[5], int(parts[6])
        await state.set_state(EventReviewInput.correction_reason)
        await state.update_data(
            event_id=event_id, user_id=user_id, result=result, page=page,
            actor_id=actor_id, review_chat_id=callback.message.chat.id,
            review_message_id=callback.message.message_id,
        )
        await callback.message.edit_text(
            "Надішліть причину корекції одним повідомленням — від 3 до 300 символів."
        )
        await callback.answer()
        return
    if action == "annul":
        if await get_effective_admin_level(actor_id) < 4:
            await callback.answer("Анулювання доступне лише адміністрації рівня 4.", show_alert=True)
            return
        review = await reviews_dao.get_review(event_id)
        if not review or review["status"] != "completed":
            await callback.answer("Подію вже не можна анулювати.", show_alert=True)
            return
        await state.set_state(EventReviewInput.annulment_reason)
        await state.update_data(
            event_id=event_id, actor_id=actor_id,
            review_chat_id=callback.message.chat.id,
            review_message_id=callback.message.message_id,
        )
        await callback.message.edit_text(
            "Надішліть причину анулювання одним повідомленням — від 3 до 300 символів."
        )
        await callback.answer()
        return
    await callback.answer("Некоректна дія.", show_alert=True)


async def _delete_input(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def _load_owned_input(message: Message, state: FSMContext) -> dict | None:
    data = await state.get_data()
    if (
        not message.from_user
        or message.from_user.id != int(data.get("actor_id", 0))
        or message.chat.id != int(data.get("review_chat_id", 0))
        or not await can_manage_events(message.from_user.id, message.chat.id)
    ):
        return None
    return data


@router.message(EventReviewInput.exclusion_reason)
async def exclusion_reason_input(message: Message, state: FSMContext) -> None:
    data = await _load_owned_input(message, state)
    if data is None:
        return
    reason = (message.text or "").strip()
    await _delete_input(message)
    if not 3 <= len(reason) <= 300:
        await message.bot.edit_message_text(
            "Причина повинна містити від 3 до 300 символів.",
            chat_id=int(data["review_chat_id"]), message_id=int(data["review_message_id"]),
        )
        return
    code = await reviews_dao.set_result(
        int(data["event_id"]), int(data["user_id"]), "excluded",
        actor_id=message.from_user.id, now=event_service.now_timestamp(), reason=reason,
    )
    await state.clear()
    if code == "updated":
        review = await reviews_dao.get_review(int(data["event_id"]), page=int(data["page"]))
        await message.bot.edit_message_text(
            render_review(review),
            chat_id=int(data["review_chat_id"]), message_id=int(data["review_message_id"]),
            parse_mode="HTML",
            reply_markup=review_keyboard(review),
            disable_web_page_preview=True,
        )


@router.message(EventReviewInput.correction_reason)
async def correction_reason_input(message: Message, state: FSMContext) -> None:
    data = await _load_owned_input(message, state)
    if data is None:
        return
    reason = (message.text or "").strip()
    await _delete_input(message)
    if not 3 <= len(reason) <= 300:
        await message.bot.edit_message_text(
            "Причина повинна містити від 3 до 300 символів.",
            chat_id=int(data["review_chat_id"]), message_id=int(data["review_message_id"]),
        )
        return
    code = await reviews_dao.correct_result(
        int(data["event_id"]), int(data["user_id"]), str(data["result"]),
        actor_id=message.from_user.id,
        admin_level=await get_effective_admin_level(message.from_user.id),
        reason=reason, now=event_service.now_timestamp(),
    )
    await state.clear()
    if code == "updated":
        from app.handlers.events.keyboards import public_event_keyboard

        await event_service.refresh_event_card(
            message.bot,
            int(data["event_id"]),
            reply_markup_factory=public_event_keyboard,
        )
        review = await reviews_dao.get_review(int(data["event_id"]), page=int(data["page"]))
        await message.bot.edit_message_text(
            render_review(review), chat_id=int(data["review_chat_id"]),
            message_id=int(data["review_message_id"]), parse_mode="HTML",
            reply_markup=review_keyboard(review), disable_web_page_preview=True,
        )
    elif code == "expired":
        await message.bot.edit_message_text(
            "24 години після фіналізації минули. Подальша корекція доступна лише адміністрації рівня 4.",
            chat_id=int(data["review_chat_id"]), message_id=int(data["review_message_id"]),
        )


@router.message(EventReviewInput.annulment_reason)
async def annulment_reason_input(message: Message, state: FSMContext) -> None:
    data = await _load_owned_input(message, state)
    if data is None:
        return
    if await get_effective_admin_level(message.from_user.id) < 4:
        await state.clear()
        await message.bot.edit_message_text(
            "Анулювання доступне лише адміністрації рівня 4.",
            chat_id=int(data["review_chat_id"]),
            message_id=int(data["review_message_id"]),
        )
        return
    reason = (message.text or "").strip()
    await _delete_input(message)
    if not 3 <= len(reason) <= 300:
        await message.bot.edit_message_text(
            "Причина повинна містити від 3 до 300 символів.",
            chat_id=int(data["review_chat_id"]), message_id=int(data["review_message_id"]),
        )
        return
    result = await reviews_dao.annul_event(
        int(data["event_id"]), actor_id=message.from_user.id,
        reason=reason, now=event_service.now_timestamp(),
    )
    await state.clear()
    if result.code == "annulled":
        await publish_cancellation(message.bot, int(data["event_id"]))
        await message.bot.edit_message_text(
            f'Подію «{result.title}» анульовано. Її результати більше не впливають на рейтинг.',
            chat_id=int(data["review_chat_id"]), message_id=int(data["review_message_id"]),
        )
