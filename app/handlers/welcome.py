import time

from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.core.db import ensure_clan_member, get_chat_setting, set_chat_setting
from app.core.config import MAIN_CHAT_ID, BOT_OWNER_ID
from app.core.db import get_admin_level
from aiogram.filters import Command

router = Router()

RULES_URL = "https://teletype.in/@jokerrecon/OfRules"

DEFAULT_WELCOME_HTML = (
    "{mention}, вітаю в клані!\n"
    "Сьогодні твій перший день в клані і тобі слід познайомитися з наступною інформацією:\n"
    " - Ти потрапив(-ла) в основний чат, тут спілкуємося виключно про гру. Тут і оголошення і дуже корисна інформація. "
    "За можливістю чат не глушимо, надоїдати не будемо. А для спілкування у нас є чат <b>Родини</b>, покликання знайдеш нижче;\n"
    " - Вступаючи у клан ти уже погодився(-лася) з правилами клану. Тепер треба <b>ОБОВ'ЯЗКОВО</b> прочитати те, з чим ти погодився(-лася), "
    "щоб не виникали потім \"нюанси\". Натисни на кнопку внизу і ретельно прочитай!;\n"
    " - Щоб бути справді частиною клану треба поставити тег клану в початок свого ніку. Просто натисни один раз і він скопіюється: "
    "<code>JRツ</code>\n"
    "Але обережно, не у всіх відображається цей смайлик. Чому? Питай в древніх старожилів цього чату...;\n"
    " - Обовʼязково реагуємо (за можливості) на повідомлення де тебе згадали, ти можеш бути потрібен(-бна) комусь тут і зараз;\n"
    " - Пояснення назви клану можеш прочитати в кінці правил. Там і легенда, і саме пояснення;\n"
    " - Зі своїми функціями і чим я можу бути тобі корисна, дізнаєшся згодом.\n"
    "\n"
    "<b>Корисні посилання:</b>\n"
    "📱 <a href=\"https://t.me/+KbU5wv6mII9mNWM6\">Родина JR</a> (офтоп спілкування)\n"
    "📱 <a href=\"https://www.tiktok.com/@jr__ua?_t=ZM-8v9U9QDbKw1&_r=1\">TikTok</a>\n"
    "\n"
    "<b>Гарної та приємної гри!</b>"
)


def mention_html(user) -> str:
    # клікабельний тег без @username
    name = (user.full_name or "боєць").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


@router.message(F.chat.id == MAIN_CHAT_ID, F.new_chat_members)
async def on_new_members(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Правила клану", url=RULES_URL)]
    ])

    joined_at = int(time.time())

    for user in message.new_chat_members:
        # 1) фіксуємо дату першого входу (тільки якщо юзера ще нема)
        await ensure_clan_member(user.id, joined_at)

        # 2) вітання + кнопка з посиланням
        custom = await get_chat_setting(MAIN_CHAT_ID, "welcome_html")
        template = custom or DEFAULT_WELCOME_HTML
        text = template.format(mention=mention_html(user))

        await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

@router.message(F.chat.id == MAIN_CHAT_ID, Command("uploadwelcome"))
async def upload_welcome(message: Message):
    # Доступ: власник або адмін 3-4
    uid = message.from_user.id if message.from_user else 0
    level = await get_admin_level(uid)

    if uid != BOT_OWNER_ID and level < 3:
        return  # мовчки, щоб не світити команду

    # Беремо HTML-текст (зберігає форматування)
    raw = message.html_text or message.text or ""

    # Очікуємо формат:
    # /uploadwelcome
    # ТУТ НОВИЙ ТЕКСТ...
    parts = raw.split("\n", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Формат:\n"
            "/uploadwelcome\n"
            "Твій новий текст з нового рядка (форматування збережеться)."
        )
        return

    new_template = parts[1].strip()

    # Важливо: шаблон має містити {mention}, інакше не згадаємо юзера
    if "{mention}" not in new_template:
        await message.answer("Додай у текст плейсхолдер {mention} (щоб я могла згадати новачка).")
        return

    await set_chat_setting(MAIN_CHAT_ID, "welcome_html", new_template)
    await message.answer("Вітальне повідомлення оновлено.")