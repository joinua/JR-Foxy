"""Handler for /predict command (daily personal predictions)."""

import json
import random
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message

from app.handlers.predictions import predictions

router = Router()

FAMILY_JR_CHAT_ID = -1003570487991
CONST_TEXT = "Приходи за новим передбаченням завтра!"

# Файл збереження (простий JSON)
STORE_PATH = Path("app/data/predict_store.json")


def _today_str() -> str:
    return datetime.now().date().isoformat()


def _load_store() -> dict:
    try:
        if not STORE_PATH.exists():
            return {}
        with STORE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, JSONDecodeError):
        # файл відсутній, битий або немає доступу - починаємо з чистого
        return {}



def _save_store(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STORE_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@router.message(Command("predict"))
async def predict_cmd(message: Message) -> None:
    """Пишемо в рідному чаті та видаляємо команду"""

    # Дозволено лише в чаті "Родина JR"
    if message.chat.id != FAMILY_JR_CHAT_ID:
        return

    # Видаляємо повідомлення з командою
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


    user = message.from_user
    if not user:
        return

    mention = user.mention_html()
    user_id = str(user.id)
    today = _today_str()

    store = _load_store()
    entry = store.get(user_id)

    # Якщо вже є передбачення на сьогодні — віддаємо його
    if isinstance(entry, dict) and entry.get("date") == today and entry.get("prediction"):
        pick = entry["prediction"]
    else:
        # Інакше — генеруємо нове і зберігаємо
        pick = random.choice(predictions)
        store[user_id] = {
            "date": today,
            "prediction": pick,
        }
        _save_store(store)

    # Акуратне завершення рядка (без дубля знаків)
    pick_clean = pick.rstrip()
    if pick_clean and pick_clean[-1] in ".!?…":
        pick_out = pick_clean
    else:
        pick_out = pick_clean + "."

    await message.answer(
        f"Передбачення для {mention},\n"
        f"{pick_out}\n\n"
        f"{CONST_TEXT}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
