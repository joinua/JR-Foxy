"""Exit verification across JR chats, with persistent administrative actions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.access import ADMIN_CHAT_IDS, OFFICER_CHAT_IDS
from app.core.config import (
    ADMIN_LOG_CHAT_ID, BOT_OWNER_ID, FAMILY_CHAT_ID, INVITE_CHAT_ID, MAIN_CHAT_ID,
)
from app.core.db import cancel_pending_tasks, schedule_task
from app.dao import clan_exit as dao
from app.handlers.profile.utils import format_duration

logger = logging.getLogger(__name__)
KYIV = ZoneInfo("Europe/Kyiv")
EXIT_CLEANUP_TASK = "clan_exit_cleanup"
EXIT_REMINDER_TASK = "clan_exit_reminder"
MAX_REMINDERS = 5
CLEANUP_RETRY_DELAYS = (60, 300, 900)
EXPLANATION_WAIT_SECONDS = 24 * 60 * 60
TELEGRAM_TIMEOUT_SECONDS = 10
CHECK_FAILED = "не вдалося перевірити — вручну"


class CardPublishError(RuntimeError):
    """The persisted case could not be displayed in Telegram."""


async def telegram_call(operation):
    return await asyncio.wait_for(operation, timeout=TELEGRAM_TIMEOUT_SECONDS)

OTHER_CHATS = {
    FAMILY_CHAT_ID: "Родина",
    **{chat_id: "Офіцери" for chat_id in OFFICER_CHAT_IDS},
    **{chat_id: "Адміністрація" for chat_id in ADMIN_CHAT_IDS},
    INVITE_CHAT_ID: "Приймальня",
}
OTHER_CHATS.pop(MAIN_CHAT_ID, None)


async def main_member(bot, user_id: int) -> bool | None:
    try:
        member = await telegram_call(bot.get_chat_member(MAIN_CHAT_ID, user_id))
    except Exception:
        logger.exception("Cannot verify main chat membership", extra={"user_id": user_id})
        return None
    return _is_member(member)


def _is_member(member) -> bool:
    return member.status in {"member", "administrator", "creator"} or (
        member.status == "restricted" and bool(getattr(member, "is_member", False))
    )


def _date(timestamp: int | None) -> str:
    return datetime.fromtimestamp(timestamp, KYIV).strftime("%d.%m %H:%M") if timestamp else "—"


def _duration(case: dict) -> str:
    if not case["join_date"]:
        return "дату вступу не вказано"
    try:
        start = datetime.fromisoformat(case["join_date"]).date()
        end = datetime.fromtimestamp(case["departed_at"], KYIV).date()
        return format_duration(start, end)
    except ValueError:
        return "дату вступу потрібно перевірити"


def _actor_mention(user_id: int | None, names: dict[int, str] | None = None) -> str:
    """A clickable reviewer name, never a raw numeric Telegram ID."""
    if user_id is None:
        return "невідомо"
    label = (names or {}).get(int(user_id)) or "адміністратор"
    return f'<a href="tg://user?id={int(user_id)}">{escape(label)}</a>'


def card(case: dict, actor_names: dict[int, str] | None = None) -> str:
    status = case["status"]
    heading = {
        "open": "🚪 Помічено вихід з головного чату",
        "returned": "↩️ Гравець повернувся в головний чат",
        "closed": "✅ Вихід з клану перевірено",
    }[status]
    states = json.loads(case["chat_states"])
    actor_names = actor_names or {}
    mention = _actor_mention
    nickname = escape(case["nickname"] or "не вказано")
    telegram_name = escape(case["telegram_name"] or str(case["user_id"]))
    username = (case.get("telegram_username") or "").lstrip("@")
    # Only link to a public username when it satisfies Telegram's URL format.
    if username and 5 <= len(username) <= 32 and username.replace("_", "").isalnum() and username.isascii():
        tg_link = f'<a href="https://t.me/{username}">@{escape(username)}</a>'
    else:
        tg_link = f'<a href="tg://user?id={int(case["user_id"])}">{telegram_name}</a>'
    departure = {
        "вийшов сам": "вийшов самостійно",
        "вилучений адміністратором": "вилучений адміністратором",
    }.get(case["departure_kind"], case["departure_kind"])

    lines = [
        f"<b>{heading} #{int(case['id'])}</b>",
        f"🎮 <b>Гравець:</b> {nickname}",
        f"👤 <b>Telegram:</b> {tg_link}",
        f"🧭 <b>Спосіб виходу:</b> {escape(departure)}",
        f"📅 <b>У клані:</b> {_duration(case)}",
    ]
    other_states = {chat_id: states.get(str(chat_id), "очікує перевірки")
                    for chat_id in OTHER_CHATS}
    if other_states and all(v == "немає" for v in other_states.values()):
        lines.append("✅ <b>Чати JR:</b> відсутній(-ня) у всіх")
    else:
        lines.append("👥 <b>Чати JR:</b>")
        for chat_id, value in other_states.items():
            if value == "немає":
                continue
            indicator = "⚠️" if value == CHECK_FAILED else "🔸"
            lines.append(f"  {indicator} {escape(OTHER_CHATS[chat_id])} — {escape(value)}")
        if other_states and any(v == "немає" for v in other_states.values()):
            lines.append(f"  ✅ Перевірено: {sum(v == 'немає' for v in other_states.values())} з {len(other_states)}")

    if case["game_at"]:
        game = (f"вилучено · підтвердив {mention(case['game_by'], actor_names)}"
                f" · {_date(case['game_at'])}")
    else:
        game = "очікує підтвердження"
    lines.append(f"🎯 <b>У грі:</b> {game}")

    explanation_status = case["explanation_status"]
    if explanation_status == "not_requested":
        explanation = "не запитували"
    elif explanation_status == "requested":
        explanation = (f"запит надіслано · {mention(case['contact_by'], actor_names)}"
                       f" · {_date(case['contact_at'])}")
    elif explanation_status == "explained":
        explanation = (f"пояснення записано · {mention(case['explanation_by'], actor_names)}"
                       f" · {_date(case['explanation_at'])}")
    else:
        explanation = (f"пояснення не отримано · {mention(case['explanation_by'], actor_names)}"
                       f" · {_date(case['explanation_at'])}")
    lines.append(f"💬 <b>Пояснення:</b> {explanation}")
    if case["explanation_text"]:
        lines.append(f"📝 <b>Короткий виклад:</b> {escape(case['explanation_text'])}")
    if states.get(str(MAIN_CHAT_ID)) == CHECK_FAILED:
        lines.append("⚠️ Головний чат недоступний для перевірки; вилучення призупинено.")
    if status == "open":
        if case.get("deferred_until") and case["deferred_until"] > int(time.time()):
            lines.append(f"⏳ <b>Відкладено до:</b> {_date(case['deferred_until'])}"
                         f" · {mention(case.get('deferred_by'), actor_names)}")
        lines.append("📋 <b>Статус:</b> очікує завершення перевірки")
    elif status == "closed":
        lines.append("✅ <b>Підсумок:</b> перевірку завершив "
                     f"{mention(case['closed_by'], actor_names)} · {_date(case['closed_at'])}")
    elif status == "returned":
        lines.append("↩️ <b>Підсумок:</b> повернення підтверджено; перевірку закрито.")
    return "\n".join(lines)


def defer_keyboard(case: dict) -> InlineKeyboardMarkup:
    """Replace the card keyboard with a compact postpone selection."""
    check_id = case["id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="12 год", callback_data=f"exit:defer12:{check_id}"),
         InlineKeyboardButton(text="24 год", callback_data=f"exit:defer24:{check_id}"),
         InlineKeyboardButton(text="3 доби", callback_data=f"exit:defer72:{check_id}")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"exit:cancel:{check_id}")],
    ])


def keyboard(case: dict) -> InlineKeyboardMarkup | None:
    if case["status"] != "open":
        return None
    check_id = case["id"]

    def button(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label, callback_data=f"exit:{action}:{check_id}")

    rows = []
    if not case["game_at"]:
        rows.append([button("✅ Вилучено з гри", "game"), button("⏳ Відкласти", "later")])
    else:
        rows.append([button("⏳ Відкласти", "later")])
    rows.append([button("🔄 Перевірити чати", "refresh")])
    rows.append([InlineKeyboardButton(text="💬 Написати гравцю",
                                      url=f"tg://user?id={case['user_id']}")])
    explanation_status = case["explanation_status"]
    if explanation_status == "not_requested":
        rows.append([button("📨 Запит надіслано", "contact"),
                     button("📝 Внести пояснення", "explain")])
    elif explanation_status == "requested":
        rows.append([button("📝 Внести пояснення", "explain")])
        if (case["contact_at"]
                and time.time() - case["contact_at"] >= EXPLANATION_WAIT_SECONDS):
            rows.append([button("📭 Пояснення не отримано", "unavailable")])
    states = json.loads(case["chat_states"])
    if (case["game_at"] and explanation_status in {"explained", "unavailable"}
            and states.get(str(MAIN_CHAT_ID)) == "немає"
            and all(states.get(str(chat_id)) == "немає" for chat_id in OTHER_CHATS)):
        rows.append([button("🏁 Завершити", "finish")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def publish(bot, case: dict) -> None:
    reviewers = {
        int(case[field]) for field in ("game_by", "explanation_by", "contact_by", "closed_by", "deferred_by")
        if case.get(field) is not None
    }
    actor_names = await dao.actor_labels(reviewers)
    try:
        if case["message_id"]:
            await telegram_call(bot.edit_message_text(
                text=card(case, actor_names), chat_id=ADMIN_LOG_CHAT_ID, message_id=case["message_id"],
                parse_mode="HTML", reply_markup=keyboard(case),
            ))
            return
        message = await telegram_call(bot.send_message(
            chat_id=ADMIN_LOG_CHAT_ID, text=card(case, actor_names), parse_mode="HTML", reply_markup=keyboard(case),
        ))
    except Exception as exc:
        if (case["message_id"] and isinstance(exc, TelegramBadRequest)
                and "message is not modified" in str(exc).lower()):
            return
        raise CardPublishError(f"Cannot publish exit card #{case['id']}") from exc
    await dao.attach_message(case["id"], message.message_id)


async def record_exit(bot, user, kind: str, profile: dict | None) -> None:
    if user.is_bot:
        return
    case, created = await dao.create(
        user.id, kind, (profile or {}).get("game_nickname"), user.full_name or str(user.id),
        (profile or {}).get("join_date"),
        user.username or (profile or {}).get("telegram_username"),
    )
    if not created:
        return
    await schedule_task(
        EXIT_CLEANUP_TASK, int(time.time()) + 30, user_id=user.id,
        payload_json=str(case["id"]), dedupe_key=f"exit:cleanup:{case['id']}",
    )
    await schedule_task(
        EXIT_REMINDER_TASK, int(time.time()) + 86400, user_id=user.id,
        payload_json=json.dumps({"id": case["id"], "number": 1}),
        dedupe_key=f"exit:reminder:{case['id']}:1",
    )
    await publish(bot, case)


async def mark_returned(bot, case: dict, actor_id: int | None = None) -> bool:
    if await main_member(bot, case["user_id"]) is not True:
        return False
    return await _record_returned(bot, case, actor_id)


async def _record_returned(bot, case: dict, actor_id: int | None = None) -> bool:
    await dao.change(case["id"], actor_id, "returned", status="returned",
                     closed_by=actor_id, closed_at=int(time.time()))
    updated = await dao.get(case["id"])
    if updated["status"] == "returned":
        await cancel_pending_tasks(EXIT_REMINDER_TASK, user_id=case["user_id"])
        await cancel_pending_tasks(EXIT_CLEANUP_TASK, user_id=case["user_id"])
    await publish(bot, updated)
    return updated["status"] == "returned"


async def refresh_chats(bot, case: dict, *, remove: bool, actor_id: int | None = None) -> dict:
    membership = await main_member(bot, case["user_id"])
    if membership is True:
        await _record_returned(bot, case, actor_id)
        return await dao.get(case["id"])
    if membership is None:
        # Stale observations must not permit closure or removal without a confirmed exit.
        states = {str(chat_id): CHECK_FAILED for chat_id in (MAIN_CHAT_ID, *OTHER_CHATS)}
    else:
        states = {str(MAIN_CHAT_ID): "немає"}
    for chat_id in OTHER_CHATS if membership is False else ():
        try:
            member = await telegram_call(bot.get_chat_member(chat_id, case["user_id"]))
            status = member.status
            if not _is_member(member):
                states[str(chat_id)] = "немає"
            elif status in {"administrator", "creator"} or case["user_id"] == BOT_OWNER_ID:
                states[str(chat_id)] = "адміністратор — вилучити вручну"
            elif remove:
                # unbanChatMember also removes a current member and permits later rejoining.
                await telegram_call(bot.unban_chat_member(chat_id, case["user_id"]))
                confirmed = await telegram_call(bot.get_chat_member(chat_id, case["user_id"]))
                states[str(chat_id)] = "немає" if not _is_member(confirmed) else "вилучення не підтверджено"
            else:
                states[str(chat_id)] = "присутній"
        except Exception:
            logger.exception("Exit chat check failed", extra={"case_id": case["id"], "chat_id": chat_id})
            states[str(chat_id)] = CHECK_FAILED
    await dao.change(case["id"], actor_id, "chats_checked", chat_states=json.dumps(states, ensure_ascii=False))
    updated = await dao.get(case["id"])
    await publish(bot, updated)
    return updated


async def run_cleanup(bot, check_id: int, attempt: int = 0) -> None:
    case = await dao.get(check_id)
    if not case or case["status"] != "open":
        return
    updated = await refresh_chats(bot, case, remove=True)
    if updated["status"] != "open":
        return
    states = json.loads(updated["chat_states"])
    if CHECK_FAILED not in states.values():
        return
    if attempt < len(CLEANUP_RETRY_DELAYS):
        await schedule_task(
            EXIT_CLEANUP_TASK,
            int(time.time()) + CLEANUP_RETRY_DELAYS[attempt],
            user_id=case["user_id"],
            payload_json=json.dumps({"id": check_id, "attempt": attempt + 1}),
            dedupe_key=f"exit:cleanup:{check_id}:retry:{attempt + 1}",
        )
    else:
        await telegram_call(bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f"⚠️ Перевірка виходу #{check_id}: Telegram не дозволив перевірити "
            "всі чати після повторних спроб. Потрібна ручна перевірка.",
        ))


async def schedule_deferred_reminder(case: dict) -> None:
    """Move pending reminders without losing the current reminder sequence."""
    await cancel_pending_tasks(EXIT_REMINDER_TASK, user_id=case["user_id"])
    when = max(int(time.time()) + 1, int(case["deferred_until"]))
    number = int(case["reminder_count"]) + 1
    await schedule_task(
        EXIT_REMINDER_TASK, when, user_id=case["user_id"],
        payload_json=json.dumps({"id": case["id"], "number": number}),
        dedupe_key=f"exit:reminder:{case['id']}:defer:{when}",
    )


async def run_reminder(bot, check_id: int, number: int) -> None:
    case = await dao.get(check_id)
    if not case or case["status"] != "open":
        return
    next_number = int(case["reminder_count"]) + 1
    if number != next_number or number > MAX_REMINDERS:
        return
    deferred_until = int(case.get("deferred_until") or 0)
    if deferred_until > int(time.time()):
        await schedule_task(
            EXIT_REMINDER_TASK, deferred_until, user_id=case["user_id"],
            payload_json=json.dumps({"id": check_id, "number": number}),
            dedupe_key=f"exit:reminder:{check_id}:defer:{deferred_until}",
        )
        return
    await publish(bot, case)
    await telegram_call(bot.send_message(
        ADMIN_LOG_CHAT_ID,
        f"⏳ Перевірка виходу #{check_id} досі відкрита: "
        f"{escape(case['nickname'] or case['telegram_name'])}. "
        f"Нагадування {number} із {MAX_REMINDERS}.",
    ))
    await dao.change(check_id, None, "reminder_sent", reminder_count=number,
                     deferred_until=None, deferred_by=None)
    if number < MAX_REMINDERS:
        await schedule_task(
            EXIT_REMINDER_TASK, int(time.time()) + 86400, user_id=case["user_id"],
            payload_json=json.dumps({"id": check_id, "number": number + 1}),
            dedupe_key=f"exit:reminder:{check_id}:{number + 1}",
        )
    else:
        await telegram_call(bot.send_message(
            ADMIN_LOG_CHAT_ID, f"⚠️ Перевірка #{check_id}: вичерпано "
            "5 добових нагадувань. Потрібне рішення адміністрації.",
        ))


async def recover_open_cards(bot) -> None:
    """Restore missing card publications without duplicating existing tasks."""
    for case in await dao.open_cases():
        if case["message_id"]:
            continue
        try:
            await publish(bot, case)
            await schedule_task(
                EXIT_CLEANUP_TASK, int(time.time()) + 30, user_id=case["user_id"],
                payload_json=str(case["id"]), dedupe_key=f"exit:cleanup:{case['id']}",
            )
            if int(case["reminder_count"]) < MAX_REMINDERS:
                await schedule_task(
                    EXIT_REMINDER_TASK,
                    max(int(time.time()) + 86400, int(case.get("deferred_until") or 0)),
                    user_id=case["user_id"],
                    payload_json=json.dumps({"id": case["id"],
                                             "number": int(case["reminder_count"]) + 1}),
                    dedupe_key=f"exit:reminder:{case['id']}:recovery:{int(time.time())}",
                )
        except Exception:
            logger.exception("Could not recover exit card", extra={"case_id": case["id"]})
