import asyncio
import random
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.db import get_call_members

router = Router()

async def safe_delete(msg: Message) -> None:
    try:
        await msg.delete()
    except Exception:
        # нема прав / вже видалено / інше — мовчки ігноруємо
        pass


def reply_target_id(message: Message) -> int | None:
    # якщо команда написана реплаєм — відповідаємо на те повідомлення
    if message.reply_to_message:
        return message.reply_to_message.message_id
    return None


# Текст "заклику" — тільки емодзі
EMOJI_POOL = ["🦊", "⚡️", "🔥", "🎯", "💀", "🧨", "🔪", "🛡️", "🎮", "👑", "🚨", "🔔", "💣", "🏴‍☠️", "🕶️"]

def random_emoji_line() -> str:
    # 5 емодзі, кожного разу інші, без повторів в рядку
    picks = random.sample(EMOJI_POOL, k=5)
    return "".join(picks)

def random_emoji_one() -> str:
    return random.choice(EMOJI_POOL)


def build_mentions(rows: list[tuple[int, str | None]]) -> list[str]:
    # кожен елемент = емодзі, в яке зашитий user_id
    mentions = []
    for user_id, _username in rows:
        mentions.append(f'<a href="tg://user?id={user_id}">{random_emoji_one()}</a>')
    return mentions

def chunk(lst: list[str], n: int) -> list[list[str]]:
    return [lst[i:i+n] for i in range(0, len(lst), n)]

async def ensure_group(message: Message) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Ця команда працює тільки в групових чатах.")
        return False
    return True

async def bot_can_delete(message: Message) -> bool:
    me = await message.bot.get_me()
    member = await message.bot.get_chat_member(message.chat.id, me.id)
    # У aiogram повертається об'єкт ChatMember*, у адміністратора є поле can_delete_messages
    return getattr(member, "can_delete_messages", False) or member.status == "creator"


@router.message(Command("call"))
async def call_handler(message: Message):
    rt_id = reply_target_id(message)
    if not await ensure_group(message):
        return

    rows = await get_call_members()
    if not rows:
        await message.answer("Нема кого кликати. Нехай люди напишуть хоч одне повідомлення в чаті 🙂")
        return

    mentions = build_mentions(rows)
    packs = chunk(mentions, 5)

    for pack in packs:
        text = " ".join(pack)
        await message.answer(text, parse_mode="HTML", reply_to_message_id=rt_id, disable_web_page_preview=True)
        await safe_delete(message)


@router.message(Command("scall"))
async def scall_handler(message: Message):
    rt_id = reply_target_id(message)
    if not await ensure_group(message):
        return

    # перевіряємо право бота видаляти повідомлення
    if not await bot_can_delete(message):
        await message.answer("Для /scall мені треба право адміна з дозволом: Delete messages.")
        return

    rows = await get_call_members()
    if not rows:
        await message.answer("Нема кого кликати. Нехай люди напишуть хоч одне повідомлення в чаті 🙂")
        return

    mentions = build_mentions(rows)
    packs = chunk(mentions, 5)

sent_messages = []
for pack in packs:
    text = " ".join(pack)
    m = await message.answer(
        text,
        parse_mode="HTML",
        reply_to_message_id=rt_id,
        disable_web_page_preview=True
    )
    sent_messages.append(m)

await safe_delete(message)

    # авто-видалення через 5 хв
    await asyncio.sleep(300)

    for m in sent_messages:
        try:
            await m.delete()
        except Exception:
            # якщо щось пішло не так (видалили руками/нема прав/тощо) — просто ігноруємо
            pass
