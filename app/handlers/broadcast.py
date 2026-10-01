"""Announcement composer for the main and family chats."""

import asyncio
import logging
from collections import defaultdict
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.access import has_admin_level
from app.core.config import ADMIN_LOG_CHAT_ID, FAMILY_CHAT_ID, MAIN_CHAT_ID

router = Router()
logger = logging.getLogger(__name__)
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
CHATS = {"main": (MAIN_CHAT_ID, "Головний"), "family": (FAMILY_CHAT_ID, "Родина")}


class Composer(StatesGroup):
    text = State()
    photo = State()


def targets(data: dict) -> tuple[str, ...]:
    choice = data.get("choice", "both")
    return ("main", "family") if choice == "both" else (choice,)


def keyboard() -> InlineKeyboardMarkup:
    def button(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label, callback_data=f"send:{action}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Головний", "to:main"), button("Родина", "to:family"), button("Обидва", "to:both")],
        [button("✏️ Текст", "text"), button("📷 Фото", "photo"), button("🗑 Без фото", "remove")],
        [button("👁 Перегляд", "preview"), button("📤 Опублікувати", "publish"), button("❌ Скасувати", "cancel")],
    ])


def panel_text(data: dict) -> str:
    destination = ", ".join(CHATS[key][1] for key in targets(data))
    body = data.get("text") or "не додано"
    if len(body) > 160:
        body = body[:157] + "…"
    sent = ", ".join(CHATS[key][1] for key in data.get("sent", [])) or "немає"
    instruction = data.get("instruction", "Вибери чати, додай текст і за потреби фото.")
    return (f"<b>Публікація JR</b>\nЧати: {destination}\nТекст: {escape(body)}\n"
            f"Фото: {'додано' if data.get('photo') else 'немає'}\nОпубліковано: {sent}\n\n{escape(instruction)}")


def publication_html(data: dict) -> str:
    return data.get("formatted_text") or escape(data["text"])


async def remove_preview(bot, data: dict) -> None:
    if data.get("preview_id"):
        try:
            await bot.delete_message(data["chat_id"], data["preview_id"])
        except Exception:
            logger.debug("Preview already gone", exc_info=True)
        data.pop("preview_id", None)


async def update_panel(bot, state: FSMContext, data: dict) -> None:
    try:
        await bot.edit_message_text(panel_text(data), chat_id=data["chat_id"],
                                    message_id=data["panel_id"], parse_mode="HTML", reply_markup=keyboard())
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await state.update_data(**data)


@router.message(Command("send"))
async def send_handler(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await has_admin_level(message.from_user.id, 3):
        await message.answer("Недостатньо прав. Потрібен рівень 3+.")
        return
    if message.chat.type != "private":
        await message.answer("Підготуй публікацію в приватному чаті з ботом.")
        return
    old = await state.get_data()
    if old.get("chat_id") == message.chat.id:
        await remove_preview(message.bot, old)
        if old.get("panel_id"):
            try:
                await message.bot.delete_message(message.chat.id, old["panel_id"])
            except Exception:
                logger.debug("Old panel unavailable", exc_info=True)
    await state.clear()
    data = {"chat_id": message.chat.id, "choice": "both", "sent": []}
    panel = await message.answer(panel_text(data), parse_mode="HTML", reply_markup=keyboard())
    await state.update_data(**data, panel_id=panel.message_id)


@router.callback_query(F.data.startswith("send:"))
async def send_callback(query: CallbackQuery, state: FSMContext) -> None:
    async with _locks[query.from_user.id]:
        await _send_callback_locked(query, state)


async def _send_callback_locked(query: CallbackQuery, state: FSMContext) -> None:
    if not query.message or not query.data:
        return
    data = await state.get_data()
    if not data or query.message.chat.id != data.get("chat_id") or query.message.message_id != data.get("panel_id"):
        await query.answer("Це меню застаріло. Використай /send.", show_alert=True)
        return
    if not await has_admin_level(query.from_user.id, 3):
        await query.answer("Недостатньо прав.", show_alert=True)
        return
    action = query.data.split(":")
    if action[1] == "cancel":
        await remove_preview(query.bot, data)
        await state.clear()
        if data["sent"]:
            await query.message.edit_text("Частково опубліковано: " + ", ".join(CHATS[key][1] for key in data["sent"]) + ". Решту скасовано.")
        else:
            await query.message.edit_text("Публікацію скасовано.")
    elif action[1] == "to":
        if data["sent"]:
            await query.answer("Після початку публікації чати змінити не можна.", show_alert=True)
            return
        await remove_preview(query.bot, data)
        data["previewed"] = False
        data["choice"] = action[2]
        await update_panel(query.bot, state, data)
    elif action[1] in {"text", "photo", "remove"}:
        if data["sent"]:
            await query.answer("Після початку публікації вміст змінити не можна.", show_alert=True)
            return
        await remove_preview(query.bot, data)
        data["previewed"] = False
        if action[1] == "remove":
            data.pop("photo", None)
            data["instruction"] = "Фото прибрано."
        else:
            await state.set_state(Composer.text if action[1] == "text" else Composer.photo)
            data["instruction"] = "Надішли текст публікації." if action[1] == "text" else "Надішли фото для публікації."
        await update_panel(query.bot, state, data)
    elif action[1] == "preview":
        if not data.get("text"):
            await query.answer("Спочатку додай текст.", show_alert=True)
            return
        await remove_preview(query.bot, data)
        if data.get("photo"):
            sent = await query.bot.send_photo(data["chat_id"], data["photo"], caption=publication_html(data), parse_mode="HTML")
        else:
            sent = await query.bot.send_message(data["chat_id"], publication_html(data), parse_mode="HTML")
        data["preview_id"] = sent.message_id
        data["previewed"] = True
        data["instruction"] = "Перевір публікацію нижче й натисни «Опублікувати»."
        await update_panel(query.bot, state, data)
    elif action[1] == "publish":
        if not data.get("text"):
            await query.answer("Спочатку додай текст.", show_alert=True)
            return
        if not data.get("previewed"):
            await query.answer("Спочатку переглянь публікацію.", show_alert=True)
            return
        await remove_preview(query.bot, data)
        failed = []
        for key in targets(data):
            if key in data["sent"]:
                continue
            chat_id, label = CHATS[key]
            try:
                if data.get("photo"):
                    await query.bot.send_photo(chat_id, data["photo"], caption=publication_html(data), parse_mode="HTML")
                else:
                    await query.bot.send_message(chat_id, publication_html(data), parse_mode="HTML")
            except Exception:
                logger.exception("Announcement delivery failed", extra={"destination": key})
                failed.append(label)
            else:
                data["sent"].append(key)
                await state.update_data(**data)
        if failed:
            data["instruction"] = "Помилка для: " + ", ".join(failed) + ". Натисни «Опублікувати» повторно лише для них."
            await update_panel(query.bot, state, data)
        else:
            await state.clear()
            await query.message.edit_text("✅ Опубліковано: " + ", ".join(CHATS[key][1] for key in data["sent"]))
            try:
                await query.bot.send_message(ADMIN_LOG_CHAT_ID, f"Публікацію зробив {query.from_user.full_name}. Чати: " + ", ".join(CHATS[key][1] for key in data["sent"]))
            except Exception:
                logger.exception("Could not log announcement")
    await query.answer()


@router.message(Composer.text)
async def input_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    limit = 1024 if data.get("photo") else 4096
    if not message.text or len(message.text) > limit:
        data["instruction"] = f"Надішли текст до {limit} символів."
        await update_panel(message.bot, state, data)
        return
    data["text"] = message.text
    data["formatted_text"] = message.html_text
    data["previewed"] = False
    data.pop("instruction", None)
    await state.set_state(None)
    await update_panel(message.bot, state, data)


@router.message(Composer.photo)
async def input_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not message.photo:
        data["instruction"] = "Надішли саме фото."
        await update_panel(message.bot, state, data)
        return
    if len(data.get("text", "")) > 1024:
        data["instruction"] = "Скороти текст до 1024 символів перед додаванням фото."
        await update_panel(message.bot, state, data)
        return
    data["photo"] = message.photo[-1].file_id
    data["previewed"] = False
    data.pop("instruction", None)
    await state.set_state(None)
    await update_panel(message.bot, state, data)
