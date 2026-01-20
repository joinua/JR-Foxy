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
