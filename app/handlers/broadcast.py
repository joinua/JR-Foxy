"""Хендлери для розсилки оголошень адміністраторами."""

from html import escape

from aiogram import F, Router
from aiogram.types import Message, MessageEntity

from app.core.config import ADMIN_LOG_CHAT_ID, FAMILY_CHAT_ID, MAIN_CHAT_ID
from app.core.db import get_admin_level

router = Router()


def mention_html(user_id: int, first_name: str | None, last_name: str | None) -> str:
    """Повертає безпечний HTML mention користувача."""

    name = " ".join(part for part in (first_name, last_name) if part).strip()
    if not name:
        name = "Без імені"

    escaped_name = escape(name)
    return f'<a href="tg://user?id={user_id}">{escaped_name}</a>'


@router.message(F.chat.type == "private", F.text.regexp(r"^!send(\s|$)"))
async def broadcast_handler(message: Message) -> None:
    """Відправляє оголошення у службові чати сімейства."""

    if not message.from_user:
        await message.answer(
            "Недостатньо прав. Команда доступна адміністраторам з рівнем доступу 3+."
        )
        return

    level = await get_admin_level(message.from_user.id)
    if level < 3:
        await message.answer(
            "Недостатньо прав. Команда доступна адміністраторам з рівнем доступу 3+."
        )
        return

    text = message.text or ""
    payload_start = len("!send")
    while payload_start < len(text) and text[payload_start].isspace():
        payload_start += 1

    payload_text = text[payload_start:]
    if not payload_text.strip():
        await message.answer("Порожнє оголошення. Додай текст після !send.")
        return

    adjusted_entities: list[MessageEntity] = []
    for entity in message.entities or []:
        start = entity.offset
        end = entity.offset + entity.length

        if end <= payload_start:
            continue
        if start < payload_start < end:
            continue

        adjusted_entities.append(
            entity.model_copy(update={"offset": start - payload_start})
        )

    main_error: str | None = None
    family_error: str | None = None

    try:
        await message.bot.send_message(
            chat_id=MAIN_CHAT_ID,
            text=payload_text,
            entities=adjusted_entities or None,
        )
    except Exception as exc:  # noqa: BLE001
        main_error = str(exc)

    try:
        await message.bot.send_message(
            chat_id=FAMILY_CHAT_ID,
            text=payload_text,
            entities=adjusted_entities or None,
        )
    except Exception as exc:  # noqa: BLE001
        family_error = str(exc)

    if not main_error and not family_error:
        user_status = "Оголошення надіслано в обидва чати."
        log_status = "успіх"
    elif main_error and family_error:
        user_status = f"Не вдалося надіслати оголошення: {main_error}"
        log_status = "невдача"
    else:
        if main_error:
            failed_chat_name = "MAIN_CHAT_ID"
            failed_error = main_error
        else:
            failed_chat_name = "FAMILY_CHAT_ID"
            failed_error = family_error or "Невідома помилка"

        user_status = (
            f"Оголошення частково надіслано. Помилка для {failed_chat_name}: {failed_error}"
        )
        log_status = "частково"

    await message.answer(user_status)

    mention = mention_html(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.last_name,
    )
    await message.bot.send_message(
        chat_id=ADMIN_LOG_CHAT_ID,
        text=(
            f"Адміністратор {mention} зробив оголошення для чатів сімейства JokerRecon\n"
            f"Статус: {log_status}"
        ),
        parse_mode="HTML",
    )
