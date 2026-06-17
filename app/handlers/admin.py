"""Admin command handlers."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.config import BOT_OWNER_ID, MAIN_CHAT_ID
from app.core.db import (
    add_admin,
    delete_admin,
    get_admin_level,
    list_admins,
    set_chat_setting,
    set_admin_level,
    update_admin_profile,
)

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

    level = await get_admin_level(message.from_user.id)
    if level < min_level:
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


@router.message(Command("adda"))
async def add_admin_handler(message: Message) -> None:
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

    await add_admin(user_id)
    await message.answer("Додано.")


@router.message(Command("alvl"))
async def set_admin_level_handler(message: Message) -> None:
    if not await ensure_private(message):
        return

    await sync_owner_profile(message)

    if not await require_level(message, 4):
        return

    parts = message.text.split() if message.text else []
    if len(parts) < 3:
        await message.answer("Вкажи ID та рівень.")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("Невірний ID.")
        return

    try:
        level = int(parts[2])
    except ValueError:
        await message.answer("Невірний рівень.")
        return

    if level not in (1, 2, 3, 4):
        await message.answer("Рівень 1-4.")
        return

    updated = await set_admin_level(user_id, level)
    if not updated:
        await message.answer("Не знайдено.")
        return

    await message.answer("Готово.")


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

    deleted = await delete_admin(user_id)
    if not deleted:
        await message.answer("Не знайдено.")
        return

    await message.answer("Видалено.")


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
    from app.core.config import FAMILY_CHAT_ID
    from app.services.talktop import TALKTOP_ENABLED_KEY

    await set_chat_setting(FAMILY_CHAT_ID, TALKTOP_ENABLED_KEY, "1")
    await message.answer("Щоденний рейтинг балакунів увімкнено для чату Родини.")


@router.message(Command("talktop_off"))
async def talktop_off_handler(message: Message) -> None:
    if not await ensure_private(message):
        return
    await sync_owner_profile(message)
    if not await require_level(message, 3):
        return
    from app.core.config import FAMILY_CHAT_ID
    from app.services.talktop import TALKTOP_ENABLED_KEY

    await set_chat_setting(FAMILY_CHAT_ID, TALKTOP_ENABLED_KEY, "0")
    await message.answer("Щоденний рейтинг балакунів вимкнено.")


@router.message(Command("talktop_status"))
async def talktop_status_handler(message: Message) -> None:
    if not await ensure_private(message):
        return
    await sync_owner_profile(message)
    if not await require_level(message, 3):
        return
    from app.core.config import FAMILY_CHAT_ID
    from app.core.db import get_chat_setting
    from app.services.talktop import TALKTOP_ENABLED_KEY

    enabled = await get_chat_setting(FAMILY_CHAT_ID, TALKTOP_ENABLED_KEY) == "1"
    status = "увімкнено" if enabled else "вимкнено"
    await message.answer(f"Щоденний рейтинг балакунів зараз {status}.")


@router.message(Command("adminhelp"))
async def adminhelp_handler(message: Message) -> None:
    if not await ensure_private(message):
        return
    await sync_owner_profile(message)
    if not await require_level(message, 3):
        return

    await message.answer(
        "<b>Адмін-команди рівнів 3–4</b>\n\n"
        "<b>Рівень 3+</b>\n"
        "!send текст — надіслати оголошення в головний чат і чат Родини.\n"
        "/setwelcome — оновити вітальне повідомлення в головному чаті.\n"
        "/uploadrules посилання — оновити посилання на правила.\n"
        "/checkwelcome — перевірити збережене вітання.\n"
        "/joindate — встановити дату вступу в профілі.\n"
        "/tiktok_check — примусово перевірити TikTok.\n/warn або !warn — видати попередження гравцю.\n/unwarn або !unwarn — скасувати останнє активне попередження.\n/winfo або !winfo — показати інформацію про попередження гравця.\n\n"
        "<b>Рівень 4</b>\n"
        "/adda — додати адміністратора.\n"
        "/alvl — змінити рівень адміністратора.\n"
        "/dela — видалити адміністратора.\n"
        "/admlist — показати список адміністраторів.\n"
        "/silence_enable — увімкнути хвилину мовчання.\n"
        "/silence_disable — вимкнути хвилину мовчання.\n"
        "/tiktok_set_thread — встановити тему TikTok у головному чаті.\n"
        "/tiktok_enable — увімкнути TikTok Notify.\n"
        "/tiktok_disable — вимкнути TikTok Notify.\n\n"
        "🗣️ <b>Рейтинг балакунів</b>\n\n"
        "/talktop_on — увімкнути щоденний рейтинг активності в чаті Родини.\n"
        "/talktop_off — вимкнути щоденний рейтинг активності.\n"
        "/talktop_status — перевірити стан функції.\n\n"
        "Доступ: рівні 3–4.\n"
        "Публікація: щодня о 23:59 у чаті Родини.",
        parse_mode="HTML",
    )
