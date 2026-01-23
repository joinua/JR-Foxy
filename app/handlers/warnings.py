"""Warning command handlers."""

from __future__ import annotations

import html
from aiogram import Router
from aiogram.filters import Command, Text
from aiogram.types import Message

from app.core.config import ADMIN_LOG_CHAT_ID
from app.core.db import get_admin_level
from app.services.punishments import enforce_warning_ban
from app.services.warnings import (
    build_mention,
    create_warning,
    format_uk_date,
    list_active_warnings,
    list_warning_history,
    revoke_latest_warning,
    warning_status_label,
)

router = Router()


async def _require_admin_level(message: Message, min_level: int) -> bool:
    if not message.from_user:
        await message.answer("Недостатній рівень.")
        return False

    level = await get_admin_level(message.from_user.id)
    if level < min_level:
        await message.answer("Недостатній рівень.")
        return False

    return True


def _extract_args(message: Message, command: str) -> list[str]:
    text = message.text or ""
    if not text.lower().startswith(command):
        return []
    raw = text[len(command):].strip()
    return raw.split() if raw else []


def _resolve_target_from_reply(message: Message) -> tuple[int | None, str | None, str | None]:
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        return user.id, user.first_name, user.last_name
    return None, None, None


async def _resolve_target_by_username(message: Message, username: str) -> tuple[int, str | None, str | None] | None:
    try:
        chat = await message.bot.get_chat(username)
    except Exception:
        return None

    first_name = getattr(chat, "first_name", None)
    last_name = getattr(chat, "last_name", None)
    return chat.id, first_name, last_name


@router.message(Text(startswith="!warn"))
async def warn_handler(message: Message) -> None:
    if not await _require_admin_level(message, 3):
        return

    if not message.from_user:
        return

    args = _extract_args(message, "!warn")
    reply_user_id, reply_first_name, reply_last_name = _resolve_target_from_reply(message)

    username_token = None
    if args and args[0].startswith("@"):
        username_token = args.pop(0)

    if reply_user_id is None:
        if not username_token:
            await message.answer("Вкажи користувача або відповідай на його повідомлення.")
            return
        resolved = await _resolve_target_by_username(message, username_token)
        if not resolved:
            await message.answer("Не вдалося знайти користувача.")
            return
        target_user_id, target_first_name, target_last_name = resolved
    else:
        target_user_id = reply_user_id
        target_first_name = reply_first_name
        target_last_name = reply_last_name

    reason = " ".join(args).strip()
    if not reason:
        await message.answer("Вкажи причину попередження.")
        return

    admin_level = await get_admin_level(message.from_user.id)
    warning, active_count = await create_warning(
        user_id=target_user_id,
        chat_id=message.chat.id,
        reason=reason,
        issued_by=message.from_user.id,
        issued_by_level=admin_level,
    )

    await enforce_warning_ban(message.bot, message.chat.id, target_user_id, active_count)

    member_mention = build_mention(target_user_id, target_first_name, target_last_name)
    admin_mention = build_mention(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.last_name,
    )
    formatted_expires = format_uk_date(warning.expires_at)

    await message.answer(
        "\n".join(
            [
                f"Учасник клану {member_mention} - отримав попередження!",
                f" Адміністратор, що виніс попередження: {admin_mention}",
                f" Причина: {html.escape(reason)}",
                f" Діє до: {formatted_expires}",
            ]
        ),
        parse_mode="HTML",
    )


@router.message(Text(startswith="!unwarn"))
async def unwarn_handler(message: Message) -> None:
    if not await _require_admin_level(message, 3):
        return

    if not message.from_user:
        return

    args = _extract_args(message, "!unwarn")
    reply_user_id, reply_first_name, reply_last_name = _resolve_target_from_reply(message)

    username_token = None
    if args and args[0].startswith("@"):
        username_token = args.pop(0)

    if reply_user_id is None:
        if not username_token:
            await message.answer("Вкажи користувача або відповідай на його повідомлення.")
            return
        resolved = await _resolve_target_by_username(message, username_token)
        if not resolved:
            await message.answer("Не вдалося знайти користувача.")
            return
        target_user_id, target_first_name, target_last_name = resolved
    else:
        target_user_id = reply_user_id
        target_first_name = reply_first_name
        target_last_name = reply_last_name

    revoked_warning, active_count = await revoke_latest_warning(
        user_id=target_user_id,
        revoked_by=message.from_user.id,
    )

    if not revoked_warning:
        await message.answer("Немає активних попереджень для зняття.")
        return

    await enforce_warning_ban(message.bot, message.chat.id, target_user_id, active_count)

    member_mention = build_mention(target_user_id, target_first_name, target_last_name)
    await message.answer(
        f"Знято останнє активне попередження з {member_mention}.",
        parse_mode="HTML",
    )


@router.message(Command("winfo"))
async def winfo_handler(message: Message) -> None:
    if not await _require_admin_level(message, 1):
        return

    args = _extract_args(message, "/winfo")
    reply_user_id, reply_first_name, reply_last_name = _resolve_target_from_reply(message)

    username_token = None
    if args and args[0].startswith("@"):
        username_token = args.pop(0)

    if reply_user_id is None:
        if not username_token:
            await message.answer("Вкажи користувача або відповідай на його повідомлення.")
            return
        resolved = await _resolve_target_by_username(message, username_token)
        if not resolved:
            await message.answer("Не вдалося знайти користувача.")
            return
        target_user_id, target_first_name, target_last_name = resolved
    else:
        target_user_id = reply_user_id
        target_first_name = reply_first_name
        target_last_name = reply_last_name

    active_warnings = await list_active_warnings(target_user_id)
    history_warnings = await list_warning_history(target_user_id)

    target_mention = build_mention(target_user_id, target_first_name, target_last_name)
    lines = [f"Попередження для {target_mention}:"]

    if active_warnings:
        lines.append(f"Активні попередження: {len(active_warnings)}")
        for warning in active_warnings:
            lines.append(
                f"- {format_uk_date(warning.issued_at)} > {html.escape(warning.reason)}"
            )
    else:
        lines.append("Активних попереджень немає.")

    if history_warnings:
        lines.append("Повна історія:")
        for warning in history_warnings:
            status = warning_status_label(warning)
            lines.append(
                f"- {format_uk_date(warning.issued_at)} > {html.escape(warning.reason)} ({status})"
            )
    else:
        lines.append("Історія попереджень порожня.")

    await message.bot.send_message(
        ADMIN_LOG_CHAT_ID,
        "\n".join(lines),
        parse_mode="HTML",
    )


@router.message(Command("mywarns"))
async def mywarns_handler(message: Message) -> None:
    if not message.from_user:
        return

    active_warnings = await list_active_warnings(message.from_user.id)
    if not active_warnings:
        await message.answer(
            "У тебе немає ні попереджень, ні совісті дарма мене турбувати!"
        )
        return

    latest_expiration = max(warning.expires_at for warning in active_warnings)
    await message.answer(
        "\n".join(
            [
                f"Активних попереджень: {len(active_warnings)}",
                f"Найсвіжіше діє до: {format_uk_date(latest_expiration)}",
            ]
        )
    )
