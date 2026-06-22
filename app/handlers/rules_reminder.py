"""Нагадує про правила клану, коли їх згадують у дозволених чатах.""""

import logging
import re

from aiogram import Router
from aiogram.filters import Filter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.config import FAMILY_CHAT_ID, MAIN_CHAT_ID, RULES_URL
from app.core.db import RULES_URL_KEY, get_chat_setting, reserve_rules_reminder
from app.handlers.talktop import record_family_activity

router = Router()
logger = logging.getLogger(__name__)

FAMILY_COOLDOWN_SECONDS = 3 * 24 * 60 * 60
MAIN_COOLDOWN_SECONDS = 5 * 24 * 60 * 60

RULES_REMINDER_TEXT = (
    "📜 Оу, хтось згадав про правила клану! Це чудово 😏\n\n"
    "Тисни кнопку нижче та освіжи їх у пам'яті. "
    "Бо правила знають усі... майже 😄"
)

RULES_WORD_PATTERN = re.compile(
    r"(?<![\w])(?:правила(?:м|ми|х)?|правил(?:о|у|ом|і)?)(?![\w])",
    re.IGNORECASE,
)


class RulesMentionFilter(Filter):
    """Пропускає лише згадки слова «правила» в головному чаті та Родині."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.id not in {MAIN_CHAT_ID, FAMILY_CHAT_ID}:
            return False
        text = message.text or message.caption or ""
        return bool(RULES_WORD_PATTERN.search(text))


def _cooldown_for_chat(chat_id: int) -> int:
    if chat_id == FAMILY_CHAT_ID:
        return FAMILY_COOLDOWN_SECONDS
    return MAIN_COOLDOWN_SECONDS


@router.message(RulesMentionFilter())
async def remind_about_rules(message: Message) -> None:
    """Надсилає кнопку правил, якщо для чату завершився кулдаун."""

    user = message.from_user
    if not user or user.is_bot:
        return

    if message.chat.id == FAMILY_CHAT_ID:
        await record_family_activity(message)

    if not await reserve_rules_reminder(
        message.chat.id,
        _cooldown_for_chat(message.chat.id),
    ):
        return

    custom_rules_url = await get_chat_setting(MAIN_CHAT_ID, RULES_URL_KEY)
    rules_url = (custom_rules_url or RULES_URL).strip()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📖 Правила клану", url=rules_url)]
        ]
    )

    await message.answer(
        RULES_REMINDER_TEXT,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    logger.info(
        "rules reminder sent",
        extra={"chat_id": message.chat.id, "user_id": user.id},
    )
