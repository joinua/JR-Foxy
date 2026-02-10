"""Вписуємо які чати має слухати бот"""

import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set in .env")

BOT_OWNER_ID_RAW = os.getenv("BOT_OWNER_ID")

if not BOT_OWNER_ID_RAW:
    raise RuntimeError("BOT_OWNER_ID not set in .env")

try:
    BOT_OWNER_ID = int(BOT_OWNER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("BOT_OWNER_ID must be an integer") from exc

ALLOWED_CHATS = {
    -1003112818024: "Адміністрація JR",
    -1002551613807: "JokerRecon (головний чат)",
    -1003570487991: "Родина JR",
    -1003696580089: "Офіцери JR",
}

WRONG_CHAT_TEXT = (
    "Я не пристосована до цього чату.\n"
    "Звернися до мого володаря @AlexRoshe і він розробить тобі подружку за адекватну ціну.\n"
    "А я тільки належу до клану JokerRecon"
)

MAIN_CHAT_ID = -1002551613807

ADMIN_LOG_CHAT_ID_RAW = os.getenv("ADMIN_LOG_CHAT_ID")

if not ADMIN_LOG_CHAT_ID_RAW:
    raise RuntimeError("ADMIN_LOG_CHAT_ID not set in .env")

try:
    ADMIN_LOG_CHAT_ID = int(ADMIN_LOG_CHAT_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_LOG_CHAT_ID must be an integer") from exc

FAMILY_CHAT_ID_RAW = os.getenv("FAMILY_CHAT_ID")

if not FAMILY_CHAT_ID_RAW:
    raise RuntimeError("FAMILY_CHAT_ID not set in .env")

try:
    FAMILY_CHAT_ID = int(FAMILY_CHAT_ID_RAW)
except ValueError as exc:
    raise RuntimeError("FAMILY_CHAT_ID must be an integer") from exc
