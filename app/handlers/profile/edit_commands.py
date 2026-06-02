"""Handlers for editing the PR1 player-profile fields."""

from __future__ import annotations

from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.db import get_admin_level
from app.handlers.profile.profile import PROFILE_NOT_FOUND
from app.handlers.profile.utils import parse_user_date
from app.services import profile_service

router = Router()

ACCESS_ERROR = "Недостатньо прав. Ви можете змінювати лише власний профіль."
EDIT_LIMIT_ERROR = "Ліміт змін вичерпано. Зверніться до адміністрації."


async def _resolve_edit_target(message: Message) -> tuple[Any | int | None, list[str]]:
    """Resolve reply, @username, or author target and return field arguments."""

    if not message.from_user:
        return None, []

    parts = message.text.split()[1:] if message.text else []
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user, parts
    if parts and parts[0].startswith("@"):
        profile = await profile_service.find_profile_by_username(parts[0])
        return (profile["user_id"] if profile else None), parts[1:]
    return message.from_user, parts


async def _prepare_edit(message: Message) -> tuple[Any | int | None, list[str], bool]:
    if not message.from_user:
        return None, [], False

    await profile_service.sync_telegram_user(message.from_user)
    target, values = await _resolve_edit_target(message)
    if target is None:
        await message.answer(PROFILE_NOT_FOUND)
        return None, [], False

    target_id = target if isinstance(target, int) else target.id
    is_admin = 1 <= await get_admin_level(message.from_user.id) <= 4
    if target_id != message.from_user.id and not is_admin:
        await message.answer(ACCESS_ERROR)
        return None, [], False
    return target, values, is_admin


@router.message(Command("nickname"))
async def nickname_handler(message: Message) -> None:
    target, values, is_admin = await _prepare_edit(message)
    if target is None:
        return
    if len(values) != 1:
        await message.answer("Вкажіть ігровий нік. Приклад: /nickname JRঐName")
        return

    try:
        await profile_service.set_nickname(target, values[0], is_admin=is_admin)
    except ValueError:
        await message.answer(
            "Згідно з правилами клану, ігровий нік має починатися з JRঐ"
        )
        return
    except profile_service.NicknameCooldownError:
        await message.answer("Ігровий нік можна змінювати не частіше ніж раз на 7 днів.")
        return
    await message.answer("Ігровий нік збережено.")


@router.message(Command("uid"))
async def uid_handler(message: Message) -> None:
    target, values, is_admin = await _prepare_edit(message)
    if target is None:
        return
    if len(values) != 1:
        await message.answer("Вкажіть UID. Приклад: /uid 1234567891234567891")
        return

    try:
        await profile_service.set_uid(target, values[0], is_admin=is_admin)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    except profile_service.EditLimitError:
        await message.answer(EDIT_LIMIT_ERROR)
        return
    except profile_service.DuplicateUIDError:
        await message.answer("Такий UID вже внесено. Перевірте правильність UID.")
        return
    await message.answer("UID збережено.")


@router.message(Command("birthday"))
async def birthday_handler(message: Message) -> None:
    target, values, is_admin = await _prepare_edit(message)
    if target is None:
        return
    if len(values) != 1:
        await message.answer("Вкажіть дату народження. Правильний формат: 15.08.2000")
        return

    try:
        birthday = parse_user_date(values[0]).isoformat()
    except ValueError:
        await message.answer("Невірна дата. Правильний формат: 15.08.2000")
        return

    try:
        await profile_service.set_birthday(target, birthday, is_admin=is_admin)
    except profile_service.EditLimitError:
        await message.answer(EDIT_LIMIT_ERROR)
        return
    await message.answer("Дату народження збережено.")
