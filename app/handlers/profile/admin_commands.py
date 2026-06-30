"""PR2 administrative profile commands."""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message

from app.core.bot import bot
from app.core.config import ADMIN_LOG_CHAT_ID, ALLOWED_CHATS, BOT_OWNER_ID, MAIN_CHAT_ID
from app.core.db import get_admin_level
from app.handlers.profile.profile import PROFILE_NOT_FOUND
from app.handlers.profile.utils import has_explicit_user_reply, parse_user_date
from app.services import profile_service

router = Router()
logger = logging.getLogger(__name__)

ACCESS_DENIED = "Недостатньо прав для цієї команди."
TARGET_NOT_FOUND = (
    "Користувача не знайдено. Спробуйте використати команду у відповідь на повідомлення."
)
PROFILE_AUDIT_LOADING_TEXT = "⏳ Збираю дані профілів з основного чату..."
ADMIN_CHAT_IDS = {
    chat_id for chat_id, name in ALLOWED_CHATS.items() if "адміністрац" in name.lower()
}
ADMIN_CHAT_IDS.add(ADMIN_LOG_CHAT_ID)
OFFICER_CHAT_IDS = {
    chat_id for chat_id, name in ALLOWED_CHATS.items() if "офіц" in name.lower()
}
ADMIN_SAFE_CHAT_IDS = ADMIN_CHAT_IDS | OFFICER_CHAT_IDS


async def _effective_admin_level(user_id: int) -> int:
    if user_id == BOT_OWNER_ID:
        return 4
    return await get_admin_level(user_id)


def _is_private(message: Message) -> bool:
    return message.chat.type == "private"


def _in_admin_chat(message: Message) -> bool:
    return message.chat.id in ADMIN_CHAT_IDS


def _in_admin_safe_chat(message: Message) -> bool:
    return message.chat.id in ADMIN_SAFE_CHAT_IDS


async def _resolve_profile_command_target(
    message: Message,
) -> tuple[Any | int | None, list[str]]:
    parts = message.text.split()[1:] if message.text else []
    if has_explicit_user_reply(message):
        return message.reply_to_message.from_user, parts
    if parts and parts[0].startswith("@"):
        profile = await profile_service.find_profile_by_username(parts[0])
        return (profile["user_id"] if profile else None), parts[1:]
    return None, parts


def _user_snapshot_name(user: Any | int) -> str:
    if isinstance(user, int):
        return str(user)
    return user.full_name or user.username or str(user.id)


@router.message(Command("helpprofile"))
async def help_profile_handler(message: Message) -> None:
    if not message.from_user:
        return

    level = await _effective_admin_level(message.from_user.id)
    show_audit = _in_admin_safe_chat(message) or (_is_private(message) and level >= 1)
    show_join_date = _in_admin_chat(message) or (_is_private(message) and level >= 3)

    lines = [
        "<b>Допомога по профілю JR</b>",
        "",
        "<b>Основні команди</b>",
        "/profile — показати свій профіль",
        "/profile @username — показати профіль гравця",
        "/profile у відповідь на повідомлення — показати профіль гравця",
        "/nickname JRঐВашНік — вказати ігровий нік",
        "/uid 1234567891234567891 — вказати UID CODM, рівно 19 цифр",
        "/birthday 15.08.2000 — вказати дату народження",
        "",
        "<b>Адміністративні команди</b>",
    ]
    if show_audit:
        lines.append("/profileaudit — перевірити, у кого не заповнені профілі")
    if show_join_date:
        lines.append("/joindate — вручну змінити дату вступу гравця")
    lines.extend(
        [
            "/role — змінити роль гравця. Доступно тільки Лідеру.",
            "/profileadmin — адмін-панель профілю",
        ]
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("role"))
async def role_handler(message: Message) -> None:
    if not message.from_user:
        return
    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("Недостатньо прав. /role доступна тільки Лідеру.")
        return

    target, values = await _resolve_profile_command_target(message)
    if target is None:
        await message.answer(TARGET_NOT_FOUND)
        return
    if len(values) != 1 or values[0] not in profile_service.ALLOWED_ROLES:
        await message.answer(
            "Вкажіть роль: Заступник, Адміністратор, Офіцер або Боєць."
        )
        return

    target_id = target if isinstance(target, int) else target.id
    if target_id == BOT_OWNER_ID:
        await message.answer("Роль Лідера не можна змінити або понизити через /role.")
        return

    try:
        await profile_service.set_role(target, values[0])
    except profile_service.ProfileError:
        await message.answer(PROFILE_NOT_FOUND)
        return

    target_name = escape(_user_snapshot_name(target))
    role = escape(values[0])
    await message.answer(
        f"Роль для {target_name} змінено на {role}.",
        parse_mode="HTML",
    )


@router.message(Command("joindate"))
async def join_date_handler(message: Message) -> None:
    if not message.from_user:
        return

    level = await _effective_admin_level(message.from_user.id)
    if level < 3:
        await message.answer(ACCESS_DENIED)
        return

    target, values = await _resolve_profile_command_target(message)
    if target is None:
        await message.answer(TARGET_NOT_FOUND)
        return
    if len(values) != 1:
        await message.answer("Правильний формат: 15.01.2024")
        return

    try:
        join_date = parse_user_date(values[0]).isoformat()
    except ValueError:
        await message.answer("Правильний формат: 15.01.2024")
        return

    try:
        await profile_service.set_join_date(target, join_date)
    except profile_service.ProfileError:
        await message.answer(PROFILE_NOT_FOUND)
        return

    await message.answer("Дату вступу збережено.")


def _audit_display_name(row: dict) -> str:
    if row.get("game_nickname"):
        return escape(str(row["game_nickname"]))
    username = row.get("telegram_username") or row.get("call_username")
    if username:
        return f"@{escape(str(username).lstrip('@'))}"
    full_name = row.get("telegram_full_name") or " ".join(
        part for part in (row.get("call_first_name"), row.get("call_last_name")) if part
    ).strip()
    if full_name:
        return escape(full_name)
    return escape(str(row.get("user_id") or "невідомий користувач"))


def _render_profile_audit(rows: list[dict]) -> str:
    if not rows:
        return "Немає незаповнених профілів учасників клану! Гарна робота!"

    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    visible_rows = rows[:5]
    lines = ["📋 <b>Аудит профілів клану JokerRecon</b>", "━━━━━━━━━━━━"]

    for index, row in enumerate(visible_rows):
        lines.extend([f"{number_emojis[index]} {_audit_display_name(row)}", ""])
        lines.extend(f"❌ {escape(str(field))}" for field in row["missing_fields"])
        lines.append("━━━━━━━━━━━━")

    hidden_count = len(rows) - len(visible_rows)
    if hidden_count > 0:
        lines.extend(
            ["", f"Ще потребують контролю: {hidden_count} учасники клану"]
        )

    lines.extend(
        [
            "",
            (
                "Прохання Офіцерам клану допомогти учасникам заповнити вище "
                "вказану інформацію у профілі гравця зі списку. Якщо виникнуть "
                "складнощі, зверніться в чат Офіцерів за профільною допомогою."
            ),
        ]
    )
    return "\n".join(lines)


def _is_missing_chat_member_error(exc: TelegramBadRequest) -> bool:
    message = str(exc).lower()
    return "user not found" in message or "member not found" in message


def _apply_live_user_to_audit_row(row: dict, user: Any) -> None:
    row.update(
        {
            "call_username": user.username,
            "call_first_name": user.first_name,
            "call_last_name": user.last_name,
        }
    )


async def _refresh_profile_audit_rows(rows: list[dict]) -> list[dict]:
    """Verify known users against the main chat and refresh live Telegram names."""

    refreshed_rows = []
    for row in rows:
        user_id = row.get("user_id")
        if user_id in (None, ""):
            continue

        try:
            member = await bot.get_chat_member(MAIN_CHAT_ID, int(user_id))
        except TelegramBadRequest as exc:
            if _is_missing_chat_member_error(exc):
                await profile_service.archive_profile(int(user_id))
                continue
            logger.warning(
                "could not verify profile audit membership: user_id=%s error=%s",
                user_id,
                exc,
            )
            refreshed_rows.append(row)
            continue
        except TelegramForbiddenError as exc:
            logger.warning(
                "could not verify profile audit membership: user_id=%s error=%s",
                user_id,
                exc,
            )
            refreshed_rows.append(row)
            continue

        status = getattr(member.status, "value", member.status)
        if status in {"left", "kicked"}:
            await profile_service.archive_profile(int(user_id))
            continue

        _apply_live_user_to_audit_row(row, member.user)
        profile = await profile_service.sync_telegram_user(member.user, create=True)
        profile = await profile_service.fill_missing_join_date(int(user_id)) or profile
        if profile:
            row.update(
                {
                    "telegram_username": profile.get("telegram_username"),
                    "telegram_full_name": profile.get("telegram_full_name"),
                    "game_nickname": profile.get("game_nickname"),
                    "codm_uid": profile.get("codm_uid"),
                    "birthday": profile.get("birthday"),
                    "join_date": profile.get("join_date"),
                    "role": profile.get("role"),
                }
            )
        refreshed_rows.append(row)

    return refreshed_rows


@router.message(Command("profileaudit"))
async def profile_audit_handler(message: Message) -> None:
    if not message.from_user:
        return

    level = await _effective_admin_level(message.from_user.id)
    allowed_location = _in_admin_safe_chat(message) or (
        _is_private(message) and level >= 1
    )
    if level < 1 or not allowed_location:
        await message.answer(ACCESS_DENIED)
        return

    status_message = await message.answer(PROFILE_AUDIT_LOADING_TEXT)
    rows = await profile_service.list_profile_audit_candidates()
    rows = await _refresh_profile_audit_rows(rows)
    rows = profile_service.build_profile_audit_rows(rows)
    text = _render_profile_audit(rows)

    try:
        await status_message.edit_text(text, parse_mode="HTML")
    except TelegramBadRequest:
        await status_message.delete()
        await message.answer(text, parse_mode="HTML")
