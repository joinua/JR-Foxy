import asyncio
import random
from typing import Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.db import get_call_members

router = Router()

# ===== Налаштування =====
CHUNK_SIZE = 5
SCALL_TTL_SECONDS = 300  # 5 хв

EMOJI_POOL = [
    "🦊",
    "⚡️",
    "🔥",
    "🎯",
    "💀",
    "🧨",
    "🔪",
    "🛡️",
    "🎮",
    "👑",
    "🚨",
    "🔔",
    "💣",
    "🏴‍☠️",
    "🕶️",
]

MemberRow = tuple[int, str | None]


# ===== Хелпери =====
def reply_target_id(message: Message) -> Optional[int]:
    """Якщо команда написана реплаєм — відповідаємо на те повідомлення."""
    return message.reply_to_message.message_id if message.reply_to_message else None


async def safe_delete(msg: Message) -> None:
    """Безпечно видалити повідомлення (без трейсбеків)."""
    try:
        await msg.delete()
    except Exception:
        pass


def random_emoji_one() -> str:
    return random.choice(EMOJI_POOL)


def build_mentions(rows: list[MemberRow]) -> list[str]:
    """HTML mention: емодзі з tg://user?id=..."""
    return [f'<a href="tg://user?id={user_id}">{random_emoji_one()}</a>' for user_id, _ in rows]


def chunk(lst: list[str], n: int) -> list[list[str]]:
    return [lst[i:i + n] for i in range(0, len(lst), n)]


async def ensure_group(message: Message) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Ця команда працює тільки в групових чатах.")
        return False
    return True


async def bot_can_delete_messages(message: Message) -> bool:
    """Перевірка, чи бот має право delete messages у цьому чаті."""
    me = await message.bot.get_me()
    member = await message.bot.get_chat_member(message.chat.id, me.id)
    return getattr(member, "can_delete_messages", False) or member.status == "creator"


async def require_level_2_plus(message: Message) -> bool:
    """
    Заготовка під адмін-рівні 2–4.
    Зараз fallback: дозволяємо лише адмінам/креатору чату.
    Потім заміниш на SQLite admin_levels без пошуку по всьому коду.
    """
    # TODO: коли буде готово — реалізуй get_admin_level(user_id) у app.core.db і увімкни тут.
    # try:
    #     from app.core.db import get_admin_level
    #     level = await get_admin_level(message.from_user.id)
    #     if int(level) >= 2:
    #         return True
    #     await message.answer("Недостатньо прав. Потрібен рівень 2+.")
    #     return False
    # except Exception:
    #     pass

    try:
        cm = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        if cm.status in ("administrator", "creator"):
            return True
    except Exception:
        pass

    await message.answer("Недостатньо прав. Потрібен модератор/адмін (level 2+).")
    return False


async def send_call_messages(
    message: Message,
    rows: list[MemberRow],
    rt_id: Optional[int],
) -> list[Message]:
    """Надсилає серію повідомлень з меншнами, повертає список відправлених меседжів."""
    mentions = build_mentions(rows)
    packs = chunk(mentions, CHUNK_SIZE)

    sent: list[Message] = []
    for pack in packs:
        text = " ".join(pack)
        m = await message.answer(
            text,
            parse_mode="HTML",
            reply_to_message_id=rt_id,
            disable_web_page_preview=True,
        )
        sent.append(m)

    return sent


# ===== Команди =====
@router.message(Command("call"))
async def call_handler(message: Message) -> None:
    rt_id = reply_target_id(message)

    if not await ensure_group(message):
        return

    # доступ level 2–4 (поки fallback на адмінів чату)
    if not await require_level_2_plus(message):
        return

    rows = await get_call_members()
    if not rows:
        await message.answer("Нема кого кликати. Нехай люди напишуть хоч одне повідомлення в чаті 🙂")
        await safe_delete(message)  # команду все одно прибираємо
        return

    await send_call_messages(message, rows, rt_id)

    # Видаляємо саме повідомлення з /call (один раз, в кінці)
    await safe_delete(message)


@router.message(Command("scall"))
async def scall_handler(message: Message) -> None:
    rt_id = reply_target_id(message)

    if not await ensure_group(message):
        return

    # доступ level 2–4 (поки fallback на адмінів чату)
    if not await require_level_2_plus(message):
        return

    # для /scall бот має мати delete messages
    if not await bot_can_delete_messages(message):
        await message.answer("Для /scall мені треба право адміна з дозволом: Delete messages.")
        return

    rows = await get_call_members()
    if not rows:
        await message.answer("Нема кого кликати. Нехай люди напишуть хоч одне повідомлення в чаті 🙂")
        await safe_delete(message)
        return

    sent_messages = await send_call_messages(message, rows, rt_id)

    # прибираємо команду одразу після відправки всіх паків (не всередині циклу)
    await safe_delete(message)

    # авто-видалення через TTL
    await asyncio.sleep(SCALL_TTL_SECONDS)

    for m in sent_messages:
        try:
            await m.delete()
        except Exception:
            pass
# кінець