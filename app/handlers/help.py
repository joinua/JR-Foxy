from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.command_registry import list_commands_exact_level, register_command
from app.core.db import get_admin_level

router = Router()

register_command("help", "Показати публічні команди", 0, "both")
register_command("help1", "Команди рівня 1", 1, "private")
register_command("help2", "Команди рівня 2", 2, "private")
register_command("help3", "Команди рівня 3", 3, "private")
register_command("help4", "Команди рівня 4", 4, "private")

PRIVATE_ONLY_TEXT = "Ця команда доступна тільки в приватних повідомленнях бота."
TOO_EARLY_TEXT = (
    "Ще рано тобі використовувати такі команди. "
    "Будь активний і принось користь - тоді можливо щось зміниться 😉"
)
LEVEL_MISMATCH_TEXT = (
    "Нічого не вийде. Команди описані в цьому розділі не доступні тобі. "
    "Використовуй цифру зі своїм рівнем."
)


def format_commands(title: str, commands: list[str]) -> str:
    if not commands:
        return "Поки що немає команд для цього розділу."
    return "\n".join([title, *commands])


def scope_matches(command_scope: str, chat_type: str) -> bool:
    if command_scope == "both":
        return True
    if chat_type == "private":
        return command_scope == "private"
    return command_scope == "group"


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    chat_type = message.chat.type
    commands = [
        f"/{info.command} - {info.description_ua}"
        for info in list_commands_exact_level(0)
        if scope_matches(info.scope, chat_type)
    ]
    commands = sorted(commands)
    text = format_commands("Публічні команди:", commands)
    await message.answer(text)


@router.message(Command(commands=["help1", "help2", "help3", "help4"]))
async def help_level_handler(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer(PRIVATE_ONLY_TEXT)
        return

    if not message.from_user:
        await message.answer(TOO_EARLY_TEXT)
        return

    command_text = message.text.split()[0] if message.text else ""
    command_name = command_text.lstrip("/").split("@")[0]
    level_text = command_name.replace("help", "")
    try:
        requested_level = int(level_text)
    except ValueError:
        await message.answer("Невірний рівень.")
        return

    current_level = await get_admin_level(message.from_user.id)
    if current_level <= 0:
        await message.answer(TOO_EARLY_TEXT)
        return

    if current_level < requested_level:
        await message.answer(LEVEL_MISMATCH_TEXT)
        return

    commands = [
        f"/{info.command} - {info.description_ua}"
        for info in list_commands_exact_level(requested_level)
    ]
    commands = sorted(commands)
    text = format_commands(f"Команди рівня {requested_level}:", commands)
    await message.answer(text)
