"""FSM-based administrative profile panel and birthday reminder buttons."""

from __future__ import annotations

from html import escape
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.config import ADMIN_LOG_CHAT_ID, BOT_OWNER_ID
from app.handlers.profile.admin_commands import (
    ACCESS_DENIED,
    TARGET_NOT_FOUND,
    _effective_admin_level,
    _resolve_profile_command_target,
)
from app.handlers.profile.profile import PROFILE_NOT_FOUND
from app.handlers.profile.utils import parse_user_date, render_profile
from app.services import profile_service
from app.services.birthday_reminders import complete_birthday_notification, postpone_birthday_notification

router = Router()

MENU_LOCKED_ALERT = "Це меню відкрите іншим адміністратором."
ROLE_OWNER_ONLY_ALERT = "Змінювати ролі може тільки Лідер."
JOIN_DATE_LEVEL_ALERT = "Редагувати дату вступу можуть тільки адміністратори рівня 3–4."


class ProfileAdminEdit(StatesGroup):
    nickname = State()
    uid = State()
    birthday = State()
    join_date = State()


def _menu_callback(action: str, admin_id: int, target_id: int) -> str:
    return f"pa:{action}:{admin_id}:{target_id}"


def _role_callback(role: str, admin_id: int, target_id: int) -> str:
    return f"pa:role:{admin_id}:{target_id}:{role}"


def _reminder_callback(action: str, notification_id: int) -> str:
    return f"bd:{action}:{notification_id}"


def birthday_reminder_keyboard(notification_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Привітали", callback_data=_reminder_callback("done", notification_id)),
                InlineKeyboardButton(text="⏰ Нагадати через 6 годин", callback_data=_reminder_callback("later", notification_id)),
            ]
        ]
    )


def _admin_menu_keyboard(admin_id: int, target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Нік", callback_data=_menu_callback("nick", admin_id, target_id)),
                InlineKeyboardButton(text="🆔 UID", callback_data=_menu_callback("uid", admin_id, target_id)),
            ],
            [
                InlineKeyboardButton(text="🎂 День народження", callback_data=_menu_callback("birthday", admin_id, target_id)),
                InlineKeyboardButton(text="📅 Дата вступу", callback_data=_menu_callback("join", admin_id, target_id)),
            ],
            [InlineKeyboardButton(text="🏷 Роль", callback_data=_menu_callback("roles", admin_id, target_id))],
            [InlineKeyboardButton(text="👤 Показати профіль", callback_data=_menu_callback("show", admin_id, target_id))],
            [
                InlineKeyboardButton(text="🔄 Оновити", callback_data=_menu_callback("refresh", admin_id, target_id)),
                InlineKeyboardButton(text="❌ Закрити", callback_data=_menu_callback("close", admin_id, target_id)),
            ],
        ]
    )


def _role_keyboard(admin_id: int, target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=role, callback_data=_role_callback(role, admin_id, target_id))]
            for role in ("Заступник", "Адміністратор", "Офіцер", "Боєць")
        ]
    )


def _profile_title(profile: dict) -> str:
    name = profile.get("game_nickname") or profile.get("telegram_full_name") or profile.get("telegram_username") or profile["user_id"]
    return escape(str(name))


def _render_panel(profile: dict, admin_id: int) -> str:
    return (
        "<b>Адміністрування профілю</b>\n"
        f"Гравець: {_profile_title(profile)}\n"
        f"User ID: <code>{profile['user_id']}</code>\n"
        f"Адміністратор: <code>{admin_id}</code>"
    )


async def _refresh_panel(message: Message, admin_id: int, target_id: int) -> None:
    profile = await profile_service.fill_missing_join_date(target_id) or await profile_service.get_profile(target_id)
    if not profile:
        await message.edit_text(PROFILE_NOT_FOUND, reply_markup=None)
        return
    await message.edit_text(_render_panel(profile, admin_id), parse_mode="HTML", reply_markup=_admin_menu_keyboard(admin_id, target_id))


async def _check_callback_owner(callback: CallbackQuery, admin_id: int) -> bool:
    if not callback.from_user or callback.from_user.id != admin_id:
        await callback.answer(MENU_LOCKED_ALERT, show_alert=True)
        return False
    if not 1 <= await _effective_admin_level(callback.from_user.id) <= 4:
        await callback.answer(ACCESS_DENIED, show_alert=True)
        return False
    return True


@router.message(Command("profileadmin"))
async def profile_admin_handler(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    level = await _effective_admin_level(message.from_user.id)
    if not 1 <= level <= 4:
        await message.answer(ACCESS_DENIED)
        return

    await profile_service.sync_telegram_user(message.from_user)
    target, _values = await _resolve_profile_command_target(message)
    if target is None:
        await message.answer(TARGET_NOT_FOUND)
        return

    if not isinstance(target, int):
        profile = await profile_service.ensure_profile(target)
    else:
        profile = await profile_service.get_profile(target)
    if not profile:
        await message.answer(PROFILE_NOT_FOUND)
        return

    try:
        await message.delete()
    except Exception:
        pass

    await state.clear()
    await message.bot.send_message(
        ADMIN_LOG_CHAT_ID,
        _render_panel(profile, message.from_user.id),
        parse_mode="HTML",
        reply_markup=_admin_menu_keyboard(message.from_user.id, int(profile["user_id"])),
    )


@router.callback_query(F.data.startswith("pa:"))
async def profile_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.message:
        return
    parts = callback.data.split(":")
    action = parts[1]
    admin_id = int(parts[2])
    target_id = int(parts[3])
    if not await _check_callback_owner(callback, admin_id):
        return

    if action == "close":
        await state.clear()
        await callback.message.edit_text("Меню закрито.", reply_markup=None)
    elif action in {"refresh", "show"}:
        if action == "show":
            profile = await profile_service.fill_missing_join_date(target_id) or await profile_service.get_profile(target_id)
            await callback.message.answer(render_profile(profile), parse_mode="HTML")
        await _refresh_panel(callback.message, admin_id, target_id)
    elif action == "nick":
        await state.set_state(ProfileAdminEdit.nickname)
        await state.update_data(admin_id=admin_id, target_id=target_id, panel_chat_id=callback.message.chat.id, panel_message_id=callback.message.message_id)
        await callback.message.answer("Надішліть новий ігровий нік.")
    elif action == "uid":
        await state.set_state(ProfileAdminEdit.uid)
        await state.update_data(admin_id=admin_id, target_id=target_id, panel_chat_id=callback.message.chat.id, panel_message_id=callback.message.message_id)
        await callback.message.answer("Надішліть UID.")
    elif action == "birthday":
        await state.set_state(ProfileAdminEdit.birthday)
        await state.update_data(admin_id=admin_id, target_id=target_id, panel_chat_id=callback.message.chat.id, panel_message_id=callback.message.message_id)
        await callback.message.answer("Надішліть дату народження у форматі ДД.ММ.РРРР")
    elif action == "join":
        if await _effective_admin_level(admin_id) < 3:
            await callback.answer(JOIN_DATE_LEVEL_ALERT, show_alert=True)
            return
        await state.set_state(ProfileAdminEdit.join_date)
        await state.update_data(admin_id=admin_id, target_id=target_id, panel_chat_id=callback.message.chat.id, panel_message_id=callback.message.message_id)
        await callback.message.answer("Надішліть дату вступу у форматі ДД.ММ.РРРР")
    elif action == "roles":
        if admin_id != BOT_OWNER_ID:
            await callback.answer(ROLE_OWNER_ONLY_ALERT, show_alert=True)
            return
        await callback.message.answer("Оберіть роль:", reply_markup=_role_keyboard(admin_id, target_id))
    elif action == "role":
        if admin_id != BOT_OWNER_ID:
            await callback.answer(ROLE_OWNER_ONLY_ALERT, show_alert=True)
            return
        role = parts[4]
        if target_id == BOT_OWNER_ID:
            await callback.answer("Роль Лідера не можна змінити.", show_alert=True)
            return
        await profile_service.set_role(target_id, role)
        await _refresh_panel(callback.message, admin_id, target_id)
    await callback.answer()


async def _ensure_state_owner(message: Message, state: FSMContext) -> dict | None:
    data = await state.get_data()
    if not message.from_user or data.get("admin_id") != message.from_user.id:
        return None
    level = await _effective_admin_level(message.from_user.id)
    if not 1 <= level <= 4:
        await state.clear()
        return None
    data["admin_level"] = level
    return data


async def _update_original_panel(message: Message, data: dict) -> None:
    profile = await profile_service.get_profile(int(data["target_id"]))
    if profile:
        await message.bot.edit_message_text(
            _render_panel(profile, int(data["admin_id"])),
            chat_id=int(data["panel_chat_id"]),
            message_id=int(data["panel_message_id"]),
            parse_mode="HTML",
            reply_markup=_admin_menu_keyboard(int(data["admin_id"]), int(data["target_id"])),
        )


@router.message(ProfileAdminEdit.nickname)
async def admin_edit_nickname(message: Message, state: FSMContext) -> None:
    data = await _ensure_state_owner(message, state)
    if not data or not message.text:
        return
    try:
        await profile_service.set_nickname(int(data["target_id"]), message.text.strip(), is_admin=True)
    except ValueError:
        await message.answer("Згідно з правилами клану, ігровий нік має починатися з JRঐ")
        return
    await _update_original_panel(message, data)
    await state.clear()
    await message.answer("Ігровий нік збережено.")


@router.message(ProfileAdminEdit.uid)
async def admin_edit_uid(message: Message, state: FSMContext) -> None:
    data = await _ensure_state_owner(message, state)
    if not data or not message.text:
        return
    try:
        await profile_service.set_uid(int(data["target_id"]), message.text.strip(), is_admin=True)
    except (ValueError, profile_service.DuplicateUIDError) as exc:
        await message.answer(str(exc) if isinstance(exc, ValueError) else "Такий UID вже внесено. Перевірте правильність UID.")
        return
    await _update_original_panel(message, data)
    await state.clear()
    await message.answer("UID збережено.")


@router.message(ProfileAdminEdit.birthday)
async def admin_edit_birthday(message: Message, state: FSMContext) -> None:
    data = await _ensure_state_owner(message, state)
    if not data or not message.text:
        return
    try:
        birthday = parse_user_date(message.text.strip()).isoformat()
    except ValueError:
        await message.answer("Невірна дата. Правильний формат: 15.08.2000")
        return
    await profile_service.set_birthday(int(data["target_id"]), birthday, is_admin=True)
    await _update_original_panel(message, data)
    await state.clear()
    await message.answer("Дату народження збережено.")


@router.message(ProfileAdminEdit.join_date)
async def admin_edit_join_date(message: Message, state: FSMContext) -> None:
    data = await _ensure_state_owner(message, state)
    if not data or not message.text:
        return
    if int(data.get("admin_level", 0)) < 3:
        await state.clear()
        await message.answer(JOIN_DATE_LEVEL_ALERT)
        return
    try:
        join_date = parse_user_date(message.text.strip()).isoformat()
    except ValueError:
        await message.answer("Правильний формат: 15.01.2024")
        return
    await profile_service.set_join_date(int(data["target_id"]), join_date)
    await _update_original_panel(message, data)
    await state.clear()
    await message.answer("Дату вступу збережено.")


@router.callback_query(F.data.startswith("bd:"))
async def birthday_reminder_callback(callback: CallbackQuery) -> None:
    if not callback.data or not callback.message or not callback.from_user:
        return
    if not 1 <= await _effective_admin_level(callback.from_user.id) <= 4:
        await callback.answer(ACCESS_DENIED, show_alert=True)
        return
    _, action, notification_id = callback.data.split(":", 2)
    if action == "done":
        await complete_birthday_notification(int(notification_id))
        await callback.message.edit_text("✅ Учасника вже привітали.", reply_markup=None)
    elif action == "later":
        await postpone_birthday_notification(int(notification_id))
        await callback.message.edit_text("⏰ Нагадування заплановано через 6 годин.", reply_markup=None)
    await callback.answer()
