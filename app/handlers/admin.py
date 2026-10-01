"""Admin command handlers."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.access import has_admin_level
from app.core.config import BOT_OWNER_ID, FAMILY_CHAT_ID, MAIN_CHAT_ID
from app.core.db import (
    add_admin,
    get_admin_level,
    get_chat_setting,
    list_admins,
    set_chat_setting,
    update_admin_profile,
)
from app.services.talktop import TALKTOP_ENABLED_KEY

router = Router()

def is_private(message: Message) -> bool:
    return message.chat.type == "private"


async def ensure_private(message: Message) -> bool:
    if is_private(message):
        return True
    await message.answer("Ходи в приватні, пошалим там.")
    return False


async def sync_owner_profile(message: Message) -> None:
    if message.from_user and message.from_user.id == BOT_OWNER_ID:
        await add_admin(
            message.from_user.id,
            message.from_user.first_name or "",
            message.from_user.last_name or "",
            message.from_user.username or "",
        )


async def require_level(message: Message, min_level: int) -> bool:
    if not message.from_user:
        await message.answer("Недостатній рівень.")
        return False

    if not await has_admin_level(message.from_user.id, min_level):
        await message.answer("Недостатній рівень.")
        return False

    return True


@router.message(Command("myid"))
async def myid_handler(message: Message) -> None:
    if not is_private(message):
        await message.answer("Тільки в приваті.")
        return

    await sync_owner_profile(message)

    first_name = message.from_user.first_name if message.from_user else ""
    last_name = message.from_user.last_name if message.from_user else ""
    username = message.from_user.username if message.from_user else ""
    if message.from_user:
        level = await get_admin_level(message.from_user.id)
        if level > 0:
            normalized_username = username.lstrip("@") if username else ""
            await update_admin_profile(
                message.from_user.id,
                first_name or "",
                last_name or "",
                normalized_username,
            )
            username = normalized_username
    name = " ".join(part for part in (first_name, last_name) if part).strip()
    if not name:
        name = "Без імені"

    parts = [f"{name} — {message.from_user.id}"]
    if username:
        parts.append(f"@{username}")
    await message.answer(" — ".join(parts))


@router.message(Command("dela"))
async def delete_admin_handler(message: Message) -> None:
    if not await ensure_private(message):
        return

    await sync_owner_profile(message)

    if not await require_level(message, 4):
        return

    parts = message.text.split() if message.text else []
    if len(parts) < 2:
        await message.answer("Вкажи ID.")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("Невірний ID.")
        return

    from app.services import profile_service

    if user_id == BOT_OWNER_ID:
        await message.answer("Права Лідера не можна відкликати через /dela.")
        return
    try:
        await profile_service.set_role(user_id, "Боєць")
    except profile_service.ProfileError:
        await message.answer("Не знайдено.")
        return

    await message.answer("Права відкликано. Роль змінено на Боєць.")


@router.message(Command("admlist"))
async def admin_list_handler(message: Message) -> None:
    if not await ensure_private(message):
        return

    await sync_owner_profile(message)

    if not await require_level(message, 4):
        return

    rows = await list_admins()
    if not rows:
        await message.answer("Список порожній.")
        return

    lines: list[str] = []
    for user_id, first_name, last_name, username, level in rows:
        name = " ".join(part for part in (first_name, last_name) if part).strip()
        if not name:
            name = "Без імені"
        line = f"{name} — {user_id} — {level}"
        if username:
            line += f" — @{username}"
        lines.append(line)

    await message.answer("\n".join(lines))


@router.message(Command("silence_enable"))
async def silence_enable_handler(message: Message) -> None:
    if not await ensure_private(message):
        return

    await sync_owner_profile(message)

    if not await require_level(message, 4):
        return

    await set_chat_setting(MAIN_CHAT_ID, "silence_enabled", "1")
    await message.answer("Хвилину мовчання УВІМКНЕНО.")


@router.message(Command("silence_disable"))
async def silence_disable_handler(message: Message) -> None:
    if not await ensure_private(message):
        return

    await sync_owner_profile(message)

    if not await require_level(message, 4):
        return

    await set_chat_setting(MAIN_CHAT_ID, "silence_enabled", "0")
    await message.answer("Хвилину мовчання ВИМКНЕНО.")

@router.message(Command("talktop_on"))
async def talktop_on_handler(message: Message) -> None:
    if not await ensure_private(message):
        return
    await sync_owner_profile(message)
    if not await require_level(message, 3):
        return

    await set_chat_setting(FAMILY_CHAT_ID, TALKTOP_ENABLED_KEY, "1")
    await message.answer("Щоденний рейтинг балакунів увімкнено для чату Родини.")


@router.message(Command("talktop_off"))
async def talktop_off_handler(message: Message) -> None:
    if not await ensure_private(message):
        return
    await sync_owner_profile(message)
    if not await require_level(message, 3):
        return

    await set_chat_setting(FAMILY_CHAT_ID, TALKTOP_ENABLED_KEY, "0")
    await message.answer("Щоденний рейтинг балакунів вимкнено.")


@router.message(Command("talktop_status"))
async def talktop_status_handler(message: Message) -> None:
    if not await ensure_private(message):
        return
    await sync_owner_profile(message)
    if not await require_level(message, 2):
        return

    enabled = await get_chat_setting(FAMILY_CHAT_ID, TALKTOP_ENABLED_KEY) == "1"
    status = "увімкнено" if enabled else "вимкнено"
    await message.answer(f"Щоденний рейтинг балакунів зараз {status}.")
