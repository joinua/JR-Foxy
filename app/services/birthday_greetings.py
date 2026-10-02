"""Durable birthday drafts and guarded automatic publication in Kyiv time."""
import asyncio
import logging
import time
from datetime import date, datetime, time as wall_time, timedelta
from html import escape

import aiosqlite
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core import db
from app.core.config import ADMIN_LOG_CHAT_ID, FAMILY_CHAT_ID, MAIN_CHAT_ID
from app.core.dates import KYIV_TZ, to_kyiv_datetime
from app.services import profile_service

TASK = "birthday_greetings"
TERMINAL = {"sent", "cancelled", "skipped", "expired", "uncertain", "sending"}
logger = logging.getLogger(__name__)
_locks = {}


def lock(notification_id):
    return _locks.setdefault(notification_id, asyncio.Lock())


async def load(notification_id):
    async with aiosqlite.connect(db.DB_PATH) as connection:
        connection.row_factory = aiosqlite.Row
        row = await (await connection.execute(
            "SELECT * FROM birthday_pre_notifications WHERE id=?", (notification_id,),
        )).fetchone()
    return dict(row) if row else None


async def update(notification_id, **values):
    allowed = {"greeting_html", "greeting_status", "allow_main", "family_membership",
               "greeting_chat_id", "greeting_message_id", "greeting_sent_at",
               "evening_notified", "issue_notified", "responsible_user_id", "responsible_name"}
    if not values or not set(values) <= allowed:
        raise ValueError("Invalid birthday update")
    async with aiosqlite.connect(db.DB_PATH) as connection:
        await connection.execute(
            "UPDATE birthday_pre_notifications SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
            (*values.values(), notification_id),
        )
        await connection.commit()


async def membership(bot, chat_id, user_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest as exc:
        if any(reason in str(exc).lower() for reason in ("member not found", "user not found", "user not participant")):
            return False
        return None
    except Exception:
        return None
    return member.status in {"member", "administrator", "creator"} or (
        member.status == "restricted" and bool(getattr(member, "is_member", False))
    )


def editable(row, now=None):
    return row["greeting_status"] not in TERMINAL and date.fromisoformat(row["birthday_date"]) >= to_kyiv_datetime(now).date()


def keyboard(row):
    nid = row["id"]
    def button(text, action):
        return InlineKeyboardButton(text=text, callback_data=f"bdg:{action}:{nid}")
    if not editable(row):
        return None
    rows = []
    if row.get("responsible_user_id"):
        rows.append([button("✏️ Виправити привітання" if row.get("greeting_html") else "📄 Вставити текст привітання", "text")])
        if row.get("greeting_html"):
            rows.append([button("👁 Переглянути привітання", "preview"), button("🗑️ Видалити привітання", "delete")])
        if row.get("family_membership") == 0:
            rows.append([button("✅ Привітати в головному чаті", "main"), button("🚫 Не публікувати автоматично", "skip")])
        rows.append([button("🔄 Перевірити чати знову", "refresh")])
    if not row.get("responsible_user_id"):
        rows.append([InlineKeyboardButton(text="Я візьмусь", callback_data=f"bdpre:claim:{nid}")])
    rows.append([button("🙋 Взяти відповідальність (3+)", "takeover")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def message_link(chat_id, message_id):
    return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}" if str(chat_id).startswith("-100") else None


async def refresh_card(bot, notification_id):
    from app.services.birthday_reminders import render_birthday_pre_message
    row = await load(notification_id)
    if not row or not row.get("message_id"):
        return
    profile = await profile_service.get_profile(row["user_id"])
    if not profile:
        return
    text = render_birthday_pre_message(
        profile, date.fromisoformat(row["birthday_date"]),
        responsible_user_id=row.get("responsible_user_id"), responsible_name=row.get("responsible_name"),
    )
    if date.fromisoformat(row["birthday_date"]) <= to_kyiv_datetime().date():
        text = text.replace("ЗАВТРА 🎉", f"🎂 {row['birthday_date']}")
    status = row["greeting_status"]
    if status == "sent":
        text += "\n\n✅ Опубліковано: " + datetime.fromtimestamp(row["greeting_sent_at"], KYIV_TZ).strftime("%d.%m о %H:%M")
        link = message_link(row["greeting_chat_id"], row["greeting_message_id"])
        if link:
            text += f' · <a href="{link}">Відкрити привітання</a>'
    elif status in TERMINAL:
        labels = {"cancelled": "Скасовано: учасник залишив клан або змінив дату народження.",
                  "skipped": "Автоматичну публікацію скасовано відповідальним.",
                  "expired": "День народження минув. Автоматичну публікацію закрито.",
                  "uncertain": "Результат надсилання невідомий. Перевірте чат вручну; автоматичного повтору не буде.",
                  "sending": "Привітання надсилається."}
        text += "\n\n" + labels[status]
    else:
        destination = "Родина JR" if row.get("family_membership") == 1 else (
            "Головний чат (за дозволом)" if row.get("allow_main") else "Потрібна перевірка чатів / вибір адміністратора"
        )
        text += f"\n\nТекст: {'✅ підготовлено' if row.get('greeting_html') else '⏳ не підготовлено'}"
        text += f"\nПублікація: {destination} · {row['birthday_date']} о 00:01 за Києвом"
        if row.get("issue_notified"):
            text += "\n⚠️ " + escape(row["issue_notified"])
    try:
        await bot.edit_message_text(text, chat_id=ADMIN_LOG_CHAT_ID, message_id=row["message_id"],
                                    parse_mode="HTML", reply_markup=keyboard(row), disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Birthday card update failed: %s", exc)


async def check_destination(bot, notification_id):
    row = await load(notification_id)
    present = await membership(bot, FAMILY_CHAT_ID, row["user_id"])
    await update(notification_id, family_membership=present)
    return present


async def notify_issue(bot, row, reason):
    if row.get("issue_notified") == reason:
        return
    await bot.send_message(ADMIN_LOG_CHAT_ID,
        f"🎂 Привітання для {row['user_id']} ({row['birthday_date']}) не надіслано: {reason}")
    await update(row["id"], issue_notified=reason)
    await refresh_card(bot, row["id"])


async def reserve_publication(nid, target):
    async with aiosqlite.connect(db.DB_PATH) as connection:
        cursor = await connection.execute(
            "UPDATE birthday_pre_notifications SET greeting_status='sending', greeting_chat_id=? WHERE id=? AND greeting_status='draft'",
            (target, nid),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def was_published(user_id, birthday_date):
    async with aiosqlite.connect(db.DB_PATH) as connection:
        row = await (await connection.execute(
            "SELECT 1 FROM birthday_pre_notifications WHERE user_id=? AND birthday_date=? AND greeting_status='sent'",
            (user_id, birthday_date),
        )).fetchone()
        return row is not None


async def process_one(bot, nid, now):
    async with lock(nid):
        row = await load(nid)
        birthday = date.fromisoformat(row["birthday_date"])
        today = now.date()
        if row["greeting_status"] == "sending":
            # A crash may have happened after Telegram accepted the message.
            await update(nid, greeting_status="uncertain")
            await refresh_card(bot, nid)
            return
        if row["greeting_status"] in TERMINAL:
            return
        if birthday < today:
            await update(nid, greeting_status="expired")
            await refresh_card(bot, nid)
            return
        if birthday == today + timedelta(days=1) and now.hour >= 21 and not row["evening_notified"] and not row.get("greeting_html"):
            responsible = row.get("responsible_user_id")
            mention = f'<a href="tg://user?id={responsible}">Відповідальний</a>' if responsible else "Відповідального ще немає"
            await bot.send_message(ADMIN_LOG_CHAT_ID,
                f"🎂 Завтра день народження {row['user_id']}. Текст привітання ще не готовий. {mention}.", parse_mode="HTML")
            await update(nid, evening_notified=1)
        due = datetime.combine(birthday, wall_time(0, 1), KYIV_TZ)
        if now < due:
            return
        profile = await profile_service.get_profile(row["user_id"])
        if not profile or profile.get("status", "active") != "active" or profile.get("archived_at") or profile.get("deleted_at") or str(profile.get("birthday", ""))[5:] != birthday.strftime("%m-%d"):
            await update(nid, greeting_status="cancelled")
            await refresh_card(bot, nid)
            return
        main = await membership(bot, MAIN_CHAT_ID, row["user_id"])
        if main is False:
            await update(nid, greeting_status="cancelled")
            await refresh_card(bot, nid)
            return
        if main is None:
            await notify_issue(bot, row, "не вдалося перевірити членство в головному чаті")
            return
        if not row.get("greeting_html"):
            await notify_issue(bot, row, "текст не підготовлено")
            return
        family = await check_destination(bot, nid)
        if family is None:
            await notify_issue(bot, row, "не вдалося перевірити членство в Родині")
            return
        if family is False and not row["allow_main"]:
            await notify_issue(bot, row, "учасника немає в Родині; потрібен дозвіл на головний чат")
            return
        target = FAMILY_CHAT_ID if family else MAIN_CHAT_ID
        if not await reserve_publication(nid, target):
            return
        try:
            sent = await bot.send_message(target, row["greeting_html"], parse_mode="HTML", disable_web_page_preview=True)
        except (TelegramBadRequest, TelegramForbiddenError):
            await update(nid, greeting_status="draft")
            await notify_issue(bot, row, "Telegram відхилив публікацію; перевірте текст і права бота")
            return
        except Exception:
            await update(nid, greeting_status="uncertain")
            await refresh_card(bot, nid)
            return
        await update(nid, greeting_status="sent", greeting_message_id=sent.message_id,
                     greeting_sent_at=int(time.time()), issue_notified=None)
        from app.services.birthday_reminders import BIRTHDAY_REMIND_TASK
        async with aiosqlite.connect(db.DB_PATH) as connection:
            await connection.execute(
                "UPDATE birthday_notifications SET status='completed', remind_at=NULL WHERE user_id=? AND birthday_date=?",
                (row["user_id"], row["birthday_date"]),
            )
            await connection.commit()
        await db.cancel_pending_tasks(BIRTHDAY_REMIND_TASK, user_id=row["user_id"])
        await refresh_card(bot, nid)


async def run_greetings(bot):
    now = to_kyiv_datetime()
    async with aiosqlite.connect(db.DB_PATH) as connection:
        rows = await (await connection.execute(
            "SELECT id FROM birthday_pre_notifications WHERE greeting_status IN ('draft','sending')"
        )).fetchall()
    for (nid,) in rows:
        try:
            await process_one(bot, nid, now)
        except Exception:
            logger.exception("Birthday greeting processing failed", extra={"notification_id": nid})


async def recover_cards(bot):
    today = to_kyiv_datetime().date().isoformat()
    async with aiosqlite.connect(db.DB_PATH) as connection:
        rows = await (await connection.execute(
            "SELECT id FROM birthday_pre_notifications WHERE birthday_date>=? AND responsible_user_id IS NOT NULL AND greeting_status='draft'",
            (today,),
        )).fetchall()
    for (nid,) in rows:
        try:
            async with lock(nid):
                await check_destination(bot, nid)
                await refresh_card(bot, nid)
        except Exception:
            logger.exception("Could not recover birthday card", extra={"notification_id": nid})
