"""Обробники команд попереджень із тонким шаром бізнес-логіки."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html
import logging
import re

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message

from app.core.config import ADMIN_LOG_CHAT_ID
from app.core.dates import format_ua_date
from app.core.db import get_admin_level
from app.services.punishments import enforce_warning_ban
from app.services.warnings import (
    WarningRecord,
    build_mention,
    create_warning,
    list_active_warnings,
    list_warning_history,
    revoke_latest_warning,
    warning_status_label,
)

logger = logging.getLogger(__name__)
router = Router()

_COMMAND_RE = re.compile(
    r"^[!/]"  # префікс команди
    r"(?P<name>\w+)"  # назва команди
    r"(?:@[A-Za-z0-9_]+)?"  # опційно bot username
    r"(?:\s+(?P<args>.*))?$"  # аргументи
)


@dataclass(frozen=True)
class TargetUser:
    """Результат резолву цілі для модерації.

    Атрибути:
        user_id: Ідентифікатор цілі в Telegram.
        first_name: Ім'я цілі.
        last_name: Прізвище цілі.
        username_snapshot: Username на момент резолву (для snapshot-ів/логів).
        is_bot: Ознака, що ціль є ботом.
    """

    user_id: int
    first_name: str | None
    last_name: str | None
    username_snapshot: str | None
    is_bot: bool


def _chat_title(message: Message) -> str:
    """Повертає безпечну для відображення назву чату."""

    chat = message.chat
    return (
        chat.title
        or getattr(chat, "full_name", None)
        or chat.username
        or str(chat.id)
    )


def _parse_command_args(message: Message, expected: str) -> list[str]:
    """Парсить аргументи команди для префіксів `!` та `/`.

    Параметри:
        message: Вхідне повідомлення з командою.
        expected: Очікувана назва команди без префікса.

    Повертає:
        Список токенів аргументів (без самої команди).
    """

    text = (message.text or "").strip()
    match = _COMMAND_RE.match(text)
    if not match or match.group("name").lower() != expected:
        return []

    args_text = (match.group("args") or "").strip()
    return args_text.split() if args_text else []


async def _safe_delete_message(message: Message) -> None:
    """Безпечно видаляє повідомлення, ігноруючи помилки прав доступу."""

    try:
        await message.bot.delete_message(message.chat.id, message.message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return


async def _delete_message_later(message: Message, delay_seconds: int) -> None:
    """Планує видалення повідомлення через заданий час."""

    await asyncio.sleep(delay_seconds)
    await _safe_delete_message(message)


async def _answer_with_optional_ttl(
    message: Message,
    text: str,
    *,
    ttl_seconds: int | None = None,
) -> Message:
    """Відповідає адміну та, за потреби, видаляє відповідь із затримкою.

    Параметри:
        message: Повідомлення з командою.
        text: Текст відповіді.
        ttl_seconds: Якщо задано, відповідь буде видалено через цей час.

    Повертає:
        Надіслане повідомлення-відповідь.
    """

    response = await message.answer(text, parse_mode="HTML")
    if ttl_seconds is not None:
        asyncio.create_task(_delete_message_later(response, ttl_seconds))
    return response


async def _delete_command_on_success(message: Message) -> None:
    """Видаляє повідомлення-команду після успішної модераційної дії."""

    await _safe_delete_message(message)


async def _require_admin_level(
    message: Message,
    min_level: int,
    error_text: str,
    *,
    ttl_seconds: int | None = None,
) -> int | None:
    """Перевіряє рівень доступу адміністратора.

    Параметри:
        message: Повідомлення з командою.
        min_level: Мінімально необхідний рівень.
        error_text: Текст помилки у випадку недостатніх прав.
        ttl_seconds: Час автознищення повідомлення про помилку в секундах.

    Повертає:
        Рівень адміністратора, якщо перевірка пройдена, інакше None.
    """

    if not message.from_user:
        return None

    level = await get_admin_level(message.from_user.id)
    if level < min_level:
        await _answer_with_optional_ttl(message, error_text, ttl_seconds=ttl_seconds)
        return None

    return level


def _target_display_first_name(target: TargetUser) -> str | None:
    """Повертає найкраще доступне ім'я для mention цілі."""

    if target.first_name:
        return target.first_name
    if target.username_snapshot:
        return f"@{target.username_snapshot}"
    return None


def _target_mention(target: TargetUser) -> str:
    """Будує mention цілі з пріоритетом імені, а далі username snapshot."""

    return build_mention(
        target.user_id,
        _target_display_first_name(target),
        target.last_name,
    )


async def _resolve_target_user(
    message: Message,
    args: list[str],
) -> tuple[TargetUser | None, str | None]:
    """Резолвить ціль через reply або @username (reply має пріоритет).

    Параметри:
        message: Повідомлення з командою.
        args: Список аргументів; може бути змінений (видалення @username).

    Повертає:
        Кортеж (ціль або None, код помилки або None).
        Коди помилок: `no_target`, `username_not_found`.
    """

    if message.reply_to_message and message.reply_to_message.from_user:
        if args and args[0].startswith("@"):
            args.pop(0)
        user = message.reply_to_message.from_user
        return (
            TargetUser(
                user_id=user.id,
                first_name=user.first_name,
                last_name=user.last_name,
                username_snapshot=user.username,
                is_bot=bool(user.is_bot),
            ),
            None,
        )

    if not args or not args[0].startswith("@"):
        return None, "no_target"

    username_token = args.pop(0)
    try:
        chat = await message.bot.get_chat(username_token)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None, "username_not_found"

    if getattr(chat, "type", None) != "private":
        return None, "username_not_found"

    return (
        TargetUser(
            user_id=chat.id,
            first_name=getattr(chat, "first_name", None),
            last_name=getattr(chat, "last_name", None),
            username_snapshot=getattr(chat, "username", None),
            is_bot=bool(getattr(chat, "is_bot", False)),
        ),
        None,
    )


def _escape_reason(reason: str) -> str:
    """Екранує причину попередження для HTML-повідомлень."""

    return html.escape(reason)


def _warning_line(warning: WarningRecord) -> str:
    """Формує рядок попередження у вигляді «дата > причина»."""

    return f"{format_ua_date(warning.issued_at)} > {_escape_reason(warning.reason)}"


async def _send_warn_log(
    message: Message,
    *,
    target: TargetUser,
    admin_level: int,
    admin_mention: str,
    user_mention: str,
    reason: str,
    expires_at: int,
    active_count: int,
) -> None:
    """Надсилає деталізований WARN-запис до лог-чату."""

    chat_title = html.escape(_chat_title(message))
    await message.bot.send_message(
        ADMIN_LOG_CHAT_ID,
        "\n".join(
            [
                f"WARN видано: {user_mention} ({target.user_id})",
                f"Хто: {admin_mention} (lvl {admin_level})",
                f"Де: {chat_title} ({message.chat.id})",
                f"Причина: {_escape_reason(reason)}",
                f"Діє до: {format_ua_date(expires_at)}",
                f"Активних варнів тепер: {active_count}",
            ]
        ),
        parse_mode="HTML",
    )


async def _send_autoban_notifications(
    message: Message,
    *,
    target: TargetUser,
    user_mention: str,
    admin_mention: str,
    warning: WarningRecord,
    active_count: int,
) -> None:
    """Надсилає повідомлення про автобан у лог-чат та публічно."""

    chat_title = html.escape(_chat_title(message))
    await message.bot.send_message(
        ADMIN_LOG_CHAT_ID,
        "\n".join(
            [
                f"Автобан: {user_mention} ({target.user_id})",
                "Причина: 3 активних попередження",
                f"Коли: {format_ua_date(warning.issued_at)}",
                f"Остання причина: {_escape_reason(warning.reason)}",
                f"Хто видав: {admin_mention} ({warning.issued_by})",
                f"Чат: {chat_title} ({message.chat.id})",
                f"Активних попереджень: {active_count}",
                f"warning_id: {warning.id}",
            ]
        ),
        parse_mode="HTML",
    )

    await message.answer(
        "\n".join(
            [
                f"{user_mention} ({target.user_id}) покидає нас через"
                "систематичні порушення правил клану.",
                "Адміністрація вживає заходів з його відлучення від кланової інфраструктури.",
                "Не порушуйте!",
            ]
        ),
        parse_mode="HTML",
    )

    logger.info(
        "autoban notifications sent",
        extra={
            "user_id": target.user_id,
            "admin_id": message.from_user.id if message.from_user else 0,
            "chat_id": message.chat.id,
        },
    )


@router.message(Command("warn"))
@router.message(F.text.regexp(r"^!warn(?:\s|$)"))
async def warn_handler(message: Message) -> None:
    """Видає попередження та, за потреби, тригерить автобан."""

    if not message.from_user:
        return

    admin_level = await _require_admin_level(
        message,
        3,
        "Недостатньо прав. Команда доступна адміністраторам з рівнем доступу 3+.",
        ttl_seconds=60,
    )
    if admin_level is None:
        return

    args = _parse_command_args(message, "warn")
    target, error_code = await _resolve_target_user(message, args)

    if error_code == "no_target":
        await _answer_with_optional_ttl(
            message,
            "Вкажи гравця через @username або використай команду у відповідь на його повідомлення.",
            ttl_seconds=60,
        )
        return
    if error_code == "username_not_found":
        await _answer_with_optional_ttl(
            message,
            "Не вдалося знайти користувача. Використай reply на його повідомлення.",
        )
        return
    if not target:
        return

    if target.user_id == message.from_user.id:
        await _answer_with_optional_ttl(
            message,
            "У нас БДСМ не прийнято публічно висвітлювати. Лупцювати себе тут не вдасться.",
        )
        return

    if target.is_bot or target.user_id == message.bot.id:
        await _answer_with_optional_ttl(
            message,
            "Відколи бот у тебе порушник правил?"
            "Боту не можна видати попередження, а я тобі можу ахаха",
        )
        return

    reason = " ".join(args).strip()
    if not reason:
        await _answer_with_optional_ttl(
            message,
            "Додай причину попередження. Приклад: /warn @username флуд",
        )
        return

    warning, active_count = await create_warning(
        user_id=target.user_id,
        chat_id=message.chat.id,
        reason=reason,
        issued_by=message.from_user.id,
        issued_by_level=admin_level,
        user_username_snapshot=target.username_snapshot,
        admin_username_snapshot=message.from_user.username,
    )

    user_mention = _target_mention(target)
    admin_mention = build_mention(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    await message.answer(
        "\n".join(
            [
                f"Учасник клану {user_mention} - отримав попередження!",
                f"Адміністратор, що виніс попередження: {admin_mention}",
                f"Причина: {_escape_reason(reason)}",
                f"Діє до: {format_ua_date(warning.expires_at)}",
            ]
        ),
        parse_mode="HTML",
    )

    await _send_warn_log(
        message,
        target=target,
        admin_level=admin_level,
        admin_mention=admin_mention,
        user_mention=user_mention,
        reason=reason,
        expires_at=warning.expires_at,
        active_count=active_count,
    )

    if active_count >= 3:
        ban_triggered = await enforce_warning_ban(
            message.bot,
            message.chat.id,
            target.user_id,
            active_count,
            admin_id=message.from_user.id,
            warning_id=warning.id,
        )
        if ban_triggered:
            await _send_autoban_notifications(
                message,
                target=target,
                user_mention=user_mention,
                admin_mention=admin_mention,
                warning=warning,
                active_count=active_count,
            )

    await _delete_command_on_success(message)


@router.message(Command("unwarn"))
@router.message(F.text.regexp(r"^!unwarn(?:\s|$)"))
async def unwarn_handler(message: Message) -> None:
    """Скасовує останнє активне попередження без видалення історії."""

    if not message.from_user:
        return

    admin_level = await _require_admin_level(
        message,
        3,
        "Недостатньо прав. Команда доступна адміністраторам з рівнем доступу 3+.",
        ttl_seconds=60,
    )
    if admin_level is None:
        return

    args = _parse_command_args(message, "unwarn")
    target, error_code = await _resolve_target_user(message, args)

    if error_code == "no_target":
        await _answer_with_optional_ttl(
            message,
            "Вкажи гравця через @username або використай reply на його повідомлення.",
        )
        return
    if error_code == "username_not_found":
        await _answer_with_optional_ttl(
            message,
            "Не вдалося знайти користувача. Використай reply на його повідомлення.",
        )
        return
    if not target:
        return

    if target.user_id == message.from_user.id:
        await _answer_with_optional_ttl(
            message,
            "Тільки за рішенням суду. Дай номер ухвали і тоді я скасую твоє попередження.",
        )
        return

    if target.is_bot or target.user_id == message.bot.id:
        await _answer_with_optional_ttl(message, "До бота попередження не застосовуються.")
        return

    revoked_warning, active_count = await revoke_latest_warning(
        user_id=target.user_id,
        revoked_by=message.from_user.id,
    )

    user_mention = _target_mention(target)
    admin_mention = build_mention(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    if not revoked_warning:
        await _answer_with_optional_ttl(
            message,
            f"{user_mention} не має активних попереджень. Нічого скасовувати.",
        )
        return

    await message.answer(
        f"Попередження для {user_mention} скасовано.",
        parse_mode="HTML",
    )

    issued_at_line = (
        f"{format_ua_date(revoked_warning.issued_at)}"
        f" > {_escape_reason(revoked_warning.reason)}"
    )
    chat_title = html.escape(_chat_title(message))
    await message.bot.send_message(
        ADMIN_LOG_CHAT_ID,
        "\n".join(
            [
                f"WARN скасовано: {user_mention} ({target.user_id})",
                f"Хто: {admin_mention} (lvl {admin_level})",
                f"Скасовано варн: {issued_at_line}",
                f"Активних тепер: {active_count}",
                f"Де: {chat_title} ({message.chat.id})",
            ]
        ),
        parse_mode="HTML",
    )

    await _delete_command_on_success(message)


@router.message(Command("winfo"))
@router.message(F.text.regexp(r"^!winfo(?:\s|$)"))
async def winfo_handler(message: Message) -> None:
    """Формує звіт про активні попередження та повну історію в лог-чат."""

    if await _require_admin_level(
        message,
        3,
        "Недостатньо прав. Команда доступна адміністраторам з рівнем доступу 3+.",
        ttl_seconds=60,
    ) is None:
        return

    args = _parse_command_args(message, "winfo")
    target, error_code = await _resolve_target_user(message, args)

    if error_code == "no_target":
        await _answer_with_optional_ttl(
            message,
            "Вкажи гравця через @username або використай reply на його повідомлення.",
            ttl_seconds=60,
        )
        return
    if error_code == "username_not_found":
        await _answer_with_optional_ttl(
            message,
            "Не вдалося знайти користувача. Використай reply на його повідомлення.",
            ttl_seconds=60,
        )
        return
    if not target:
        return

    active_warnings = await list_active_warnings(target.user_id)
    history_warnings = await list_warning_history(
        target.user_id,
        include_revoked=True,
    )

    user_mention = _target_mention(target)
    lines: list[str] = []

    if active_warnings:
        lines.append(
            f"Гравець {user_mention} зараз має активні *{len(active_warnings)} попередження:"
        )
        lines.extend(_warning_line(warning) for warning in active_warnings)
    else:
        lines.append(f"Гравець {user_mention} не має активних попереджень.")

    if history_warnings:
        lines.append("")
        lines.append("Загалом у гравця були такі попередження:")
        lines.extend(
            (
                f"{format_ua_date(w.issued_at)} > {_escape_reason(w.reason)} "
                f"[{warning_status_label(w)}]"
            )
            for w in history_warnings
        )
    else:
        lines.append("")
        lines.append("Загалом, грацець не отримав по шапці жодного разу")

    await message.bot.send_message(
        ADMIN_LOG_CHAT_ID,
        "\n".join(lines),
        parse_mode="HTML",
    )

    await _delete_command_on_success(message)


@router.message(Command("mywarns"))
async def mywarns_handler(message: Message) -> None:
    """Показує користувачу короткий підсумок його активних попереджень."""

    if not message.from_user:
        return

    logger.info(
        "mywarns handler hit",
        extra={"user_id": message.from_user.id, "chat_id": message.chat.id},
    )

    active_warnings = await list_active_warnings(message.from_user.id)
    if not active_warnings:
        await message.answer(
            "У тебе немає ні попереджень, ні совісті дарма мене турбувати!"
        )
        return

    # Сервіс повертає активні попередження від найновішого до найстарішого.
    latest_expires_at = active_warnings[0].expires_at
    await message.answer(
        "\n".join(
            [
                f"У тебе є активних *{len(active_warnings)} попереджень",
                f"Дійсні до: {format_ua_date(latest_expires_at)}",
            ]
        ),
        parse_mode="HTML",
    )
