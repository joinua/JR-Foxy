"""Birthday draft controls attached to the existing admin reminder card."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.config import ADMIN_LOG_CHAT_ID
from app.core.db import get_admin_level
from app.services import birthday_greetings as service

router = Router()


class GreetingInput(StatesGroup):
    text = State()


async def authorized(user_id, row):
    level = await get_admin_level(user_id)
    return level >= 3 or (level >= 1 and row.get("responsible_user_id") == user_id)


@router.callback_query(F.data.startswith("bdg:"))
async def greeting_callback(query: CallbackQuery, state: FSMContext):
    if not query.message or query.message.chat.id != ADMIN_LOG_CHAT_ID:
        await query.answer("Ця дія доступна в чаті адміністрації.", show_alert=True)
        return
    try:
        _, action, raw_id = query.data.split(":")
        nid = int(raw_id)
    except (ValueError, AttributeError):
        await query.answer("Некоректна дія.", show_alert=True)
        return
    async with service.lock(nid):
        row = await service.load(nid)
        if not row or query.message.message_id != row.get("message_id"):
            await query.answer("Картка вже неактуальна.", show_alert=True)
            return
        if not await authorized(query.from_user.id, row):
            await query.answer("Редагувати може відповідальний або адміністрація 3+.", show_alert=True)
            return
        if not service.editable(row):
            await query.answer("Підготовку цього привітання вже завершено.", show_alert=True)
            return
        if action == "takeover":
            if await get_admin_level(query.from_user.id) < 3:
                await query.answer("Змінити відповідального може адміністрація 3+.", show_alert=True)
                return
            await service.update(nid, responsible_user_id=query.from_user.id, responsible_name=query.from_user.full_name)
        elif action == "text":
            await state.clear()
            prompt = await query.message.answer(
                f'<a href="tg://user?id={query.from_user.id}">Відповідальний</a>, надішліть текст привітання відповіддю на це повідомлення (до 4096 символів). Форматування збережеться. Для виходу — /cancel.',
                parse_mode="HTML",
                reply_markup=ForceReply(selective=True),
            )
            await state.set_state(GreetingInput.text)
            await state.update_data(notification_id=nid, prompt_id=prompt.message_id)
            await query.answer()
            return
        elif action == "preview":
            if row.get("greeting_html"):
                await query.message.answer(row["greeting_html"], parse_mode="HTML", disable_web_page_preview=True)
            await query.answer()
            return
        elif action == "delete":
            await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑️ Так, видалити текст", callback_data=f"bdg:confirmdelete:{nid}"),
                InlineKeyboardButton(text="↩️ Залишити", callback_data=f"bdg:back:{nid}"),
            ]]))
            await query.answer()
            return
        elif action == "confirmdelete":
            await service.update(nid, greeting_html=None, issue_notified=None)
        elif action == "main":
            await service.update(nid, allow_main=1, issue_notified=None)
        elif action == "skip":
            await service.update(nid, greeting_status="skipped")
        elif action not in {"refresh", "back"}:
            await query.answer("Невідома дія.", show_alert=True)
            return
        if action in {"refresh", "main", "takeover"}:
            await service.check_destination(query.bot, nid)
        await service.refresh_card(query.bot, nid)
    await query.answer("Збережено.")


@router.message(GreetingInput.text, F.chat.id == ADMIN_LOG_CHAT_ID)
async def greeting_text(message: Message, state: FSMContext):
    if (message.text or "").split("@")[0] == "/cancel":
        await state.clear()
        await message.answer("Введення скасовано. Збережений текст залишається в картці.")
        return
    data = await state.get_data()
    if not message.reply_to_message or message.reply_to_message.message_id != data.get("prompt_id"):
        await message.answer("Надішліть текст саме відповіддю на запит бота або /cancel.")
        return
    if not message.text or len(message.text.encode("utf-16-le")) // 2 > 4096:
        await message.answer("Потрібне текстове повідомлення до 4096 символів.")
        return
    nid = data["notification_id"]
    async with service.lock(nid):
        row = await service.load(nid)
        if not row or not service.editable(row) or not await authorized(message.from_user.id, row):
            await state.clear()
            await message.answer("Картка вже закрита або у вас більше немає права редагувати її.")
            return
        await service.update(nid, greeting_html=message.html_text, issue_notified=None)
        await service.check_destination(message.bot, nid)
        await service.refresh_card(message.bot, nid)
    await state.clear()
    await message.answer("✅ Текст збережено. Попередній перегляд:")
    await message.answer(message.html_text, parse_mode="HTML", disable_web_page_preview=True)
