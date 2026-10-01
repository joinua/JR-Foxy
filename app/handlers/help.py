"""Role-aware help menus and the public Telegram command list."""

import asyncio
import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

from app.core.access import get_effective_admin_level

router = Router()
logger = logging.getLogger(__name__)

PUBLIC_COMMANDS = [
    BotCommand(command="help", description="Довідник з користування ботом"),
    BotCommand(command="helpprofile", description="Інструкція з профілю"),
    BotCommand(command="profile", description="Відкрити свій профіль"),
    BotCommand(command="reliability", description="Моя статистика подій"),
    BotCommand(command="predict", description="Щоденне передбачення"),
]

GROUP_HELP_LIFETIME_SECONDS = 5 * 60


def render_help(level: int) -> str:
    lines = [
        "<b>JR</b>ঐ<b>Foxy — довідник з користування ботом</b>",
        "/helpprofile — інструкція «як заповнити й переглянути профіль»",
        "/profile — відкрити свій профіль",
        "/reliability — переглянути свою статистику участі в подіях",
        "/predict — отримати щоденне передбачення",
    ]
    if level >= 1:
        lines += ["", "<b>Службові довідки</b>", "/help_officer — команди офіцера"]
    if level >= 2:
        lines.append("/help_moder — команди адміністратора")
    if level >= 3:
        lines.append("/help_admin — команди заступника й лідера")
    return "\n".join(lines)


OFFICER_HELP = "\n".join([
    "<b>Офіцер — команди рівня 1</b>",
    "/profileaudit — перевірити, кому потрібно заповнити профіль",
    "/nickname у відповідь на повідомлення — виправити ігровий нік гравця",
    "/uid у відповідь на повідомлення — внести або виправити UID",
    "/birthday у відповідь на повідомлення — внести або виправити дату народження",
    "Для редагування можна вказати @username замість відповіді на повідомлення.",
    "Власний профіль гравці заповнюють за інструкцією /helpprofile.",
])

MODER_HELP = "\n".join([
    "<b>Адміністратор — команди рівня 2</b>",
    "<b>Робота з людьми</b>",
    "/candidate — обробити кандидата в Приймальні",
    "/profileadmin — відкрити панель керування профілем",
    "/winfo — переглянути попередження гравця",
    "/reliability для іншого гравця — переглянути статистику в чаті адміністрації",
    "<b>Чати й налаштування</b>",
    "/call, /scall — скликати учасників",
    "/checkwelcome — переглянути чинне привітання",
    "/tiktok_status — перевірити стан сповіщень TikTok",
    "/talktop_status — перевірити стан рейтингу активності",
])

ADMIN_HELP = "\n".join([
    "<b>Заступник — команди рівня 3</b>",
    "/event — створювати й керувати подіями",
    "/send — підготувати публікацію з переглядом і вибором чатів",
    "/warn, /unwarn — видати або зняти попередження",
    "/joindate — змінити дату вступу до клану",
    "/setwelcome — змінити привітання",
    "/uploadrules — оновити правила",
    "/talktop_on, /talktop_off — увімкнути або вимкнути рейтинг активності",
])

LEADER_HELP = "\n".join([
    "<b>Лідер — додаткові команди рівня 4</b>",
    "/role — призначити або змінити роль",
    "/dela — відкликати права та скинути роль до «Боєць»",
    "/admlist — переглянути список посад і рівнів",
    "/silence_enable, /silence_disable — керувати хвилиною мовчання",
    "/tiktok_check — запустити перевірку TikTok",
    "/tiktok_enable, /tiktok_disable, /tiktok_set_thread — керувати публікаціями TikTok",
    "/chatid — дізнатися технічний ID чату",
])


async def _delete_later(message: Message) -> None:
    await asyncio.sleep(GROUP_HELP_LIFETIME_SECONDS)
    try:
        await message.delete()
    except TelegramBadRequest:
        logger.debug("Help message already deleted or unavailable")
    except Exception:
        logger.exception("Could not delete expired help message")


async def answer_help(message: Message, body: str) -> None:
    response = await message.answer(body, parse_mode="HTML")
    if message.chat.type in ("group", "supergroup"):
        asyncio.create_task(_delete_later(response))


async def _level(message: Message) -> int:
    return await get_effective_admin_level(message.from_user.id) if message.from_user else 0


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await answer_help(message, render_help(await _level(message)))


@router.message(Command("help_officer"))
async def help_officer_handler(message: Message) -> None:
    if await _level(message) < 1:
        await message.answer("Недостатньо прав. Потрібен рівень 1+.")
        return
    await answer_help(message, OFFICER_HELP)


@router.message(Command("help_moder"))
async def help_moder_handler(message: Message) -> None:
    if await _level(message) < 2:
        await message.answer("Недостатньо прав. Потрібен рівень 2+.")
        return
    await answer_help(message, MODER_HELP)


@router.message(Command("help_admin"))
async def help_admin_handler(message: Message) -> None:
    level = await _level(message)
    if level < 3:
        await message.answer("Недостатньо прав. Потрібен рівень 3+.")
        return
    await answer_help(message, ADMIN_HELP + ("\n\n" + LEADER_HELP if level >= 4 else ""))
