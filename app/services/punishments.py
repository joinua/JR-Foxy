"""Перевірка покарань, що виводяться з поточного стану попереджень."""

from __future__ import annotations

from aiogram import Bot
import logging


logger = logging.getLogger(__name__)


async def enforce_warning_ban(
    bot: Bot,
    chat_id: int,
    user_id: int,
    active_count: int,
    *,
    admin_id: int,
    warning_id: int,
) -> bool:
    """Накладає автобан при досягненні ліміту активних попереджень.

    Важливо: автоматичний розбан навмисно не виконується, щоби не
    перекреслити рішення адміністрації та не створити «тихий» стан.

    Параметри:
        bot: Екземпляр бота для виклику Telegram API.
        chat_id: Ідентифікатор чату, де треба застосувати бан.
        user_id: Кого банимо.
        active_count: Поточна кількість активних попереджень.
        admin_id: Хто видав останнє попередження.
        warning_id: Ідентифікатор останнього попередження.

    Повертає:
        True, якщо було ініційовано автобан, інакше False.
    """

    if active_count < 3:
        return False

    try:
        await bot.ban_chat_member(chat_id, user_id)
    except Exception:
        # Помилка Telegram не повинна ламати модерацію; повертаємо сигнал,
        # що автобан фактично не відбувся.
        return False

    logger.info(
        "autoban triggered",
        extra={
            "warning_id": warning_id,
            "user_id": user_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
            "active_count": active_count,
        },
    )
    return True
