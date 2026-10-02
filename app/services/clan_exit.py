"""Exit verification across JR chats, with persistent administrative actions."""

from __future__ import annotations

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
from app.core.db import schedule_task
from app.dao import clan_exit as dao
from app.handlers.profile.utils import format_duration

logger = logging.getLogger(__name__)
KYIV = ZoneInfo("Europe/Kyiv")
EXIT_CLEANUP_TASK = "clan_exit_cleanup"
EXIT_REMINDER_TASK = "clan_exit_reminder"
EXPLANATION_WAIT_SECONDS = 24 * 60 * 60

OTHER_CHATS = {
    FAMILY_CHAT_ID: "Родина",
    **{chat_id: "Офіцери" for chat_id in OFFICER_CHAT_IDS},
    **{chat_id: "Адміністрація" for chat_id in ADMIN_CHAT_IDS},
    INVITE_CHAT_ID: "Приймальня",
}
OTHER_CHATS.pop(MAIN_CHAT_ID, None)


async def main_member(bot, user_id: int) -> bool | None:
    try:
        member = await bot.get_chat_member(MAIN_CHAT_ID, user_id)
    except Exception:
        logger.exception("Cannot verify main chat membership", extra={"user_id": user_id})
        return None
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


def card(case: dict) -> str:
    status = case["status"]
    heading = {
        "open": "🚪 Помічено вихід з клану",
        "returned": "↩️ Гравець повернувся в головний чат",
        "closed": "✅ Вихід з клану перевірено",
    }[status]
    states = json.loads(case["chat_states"])
    chat_lines = "; ".join(
        f"{name} — {escape(states.get(str(chat_id), 'очікує перевірки'))}"
        for chat_id, name in OTHER_CHATS.items()
    )
    explanation = {
        "not_requested": "не запитували",
        "requested": f"запит надіслано · {_date(case['contact_at'])}",
        "explained": f"пояснено · {_date(case['explanation_at'])}",
        "unavailable": f"пояснення не отримано · {_date(case['explanation_at'])}",
    }[case["explanation_status"]]
    game = (
        f"вилучено · підтвердив {case['game_by']} · {_date(case['game_at'])}"
        if case["game_at"] else "очікує перевірки"
    )
    label = escape(case["nickname"] or case["telegram_name"] or str(case["user_id"]))
    lines = [
        f"<b>{heading}</b>",
        f"<b>Гравець:</b> {label} · <a href=\"tg://user?id={case['user_id']}\">профіль Telegram</a>",
        f"<b>Причина виходу з чату:</b> {escape(case['departure_kind'])}",
        f"<b>У клані:</b> {_duration(case)}",
        f"<b>Інші чати:</b> {chat_lines}",
        f"<b>У грі:</b> {game}",
        f"<b>Власне пояснення виходу:</b> {explanation}",
    ]
    if case["explanation_text"]:
        lines.append(f"<b>Короткий виклад:</b> {escape(case['explanation_text'])}")
    if status == "closed":
        lines.append(f"<b>Підсумок:</b> учасник вийшов із клану; перевірку завершив {case['closed_by']} · {_date(case['closed_at'])}")
    elif status == "returned":
        lines.append("<b>Підсумок:</b> повернення підтверджено в головному чаті; перевірку закрито.")
    return "\n".join(lines)


def keyboard(case: dict) -> InlineKeyboardMarkup | None:
    if case["status"] != "open":
        return None
    check_id = case["id"]
    def button(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label, callback_data=f"exit:{action}:{check_id}")
    rows = [
        [button("✅ Вилучено з гри", "game"), button("⏳ Перевірити пізніше", "later")],
        [button("🔄 Перевірити чати", "refresh"), button("↩️ Перевірити повернення", "return")],
        [InlineKeyboardButton(text="💬 Написати гравцю", url=f"tg://user?id={case['user_id']}")],
        [button("📨 Запит надіслано", "contact"), button("📝 Пояснення надано", "explain")],
    ]
    if case["contact_at"] and time.time() - case["contact_at"] >= EXPLANATION_WAIT_SECONDS:
        rows.append([button("📭 Пояснення не отримано", "unavailable")])
    if (case["game_at"] and case["explanation_status"] in {"explained", "unavailable"}
            and all(json.loads(case["chat_states"]).get(str(chat_id)) == "немає"
                    for chat_id in OTHER_CHATS)):
        rows.append([button("🏁 Завершити перевірку", "finish")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def publish(bot, case: dict) -> None:
    if case["message_id"]:
        try:
            await bot.edit_message_text(
                card(case), ADMIN_LOG_CHAT_ID, case["message_id"],
                parse_mode="HTML", reply_markup=keyboard(case),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        return
    message = await bot.send_message(
        ADMIN_LOG_CHAT_ID, card(case), parse_mode="HTML", reply_markup=keyboard(case),
    )
    await dao.change(case["id"], None, "message_published", message_id=message.message_id)


async def record_exit(bot, user, kind: str, profile: dict | None) -> None:
    if user.is_bot:
        return
    case, created = await dao.create(
        user.id, kind, (profile or {}).get("game_nickname"), user.full_name or str(user.id),
        (profile or {}).get("join_date"),
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
    await dao.change(case["id"], actor_id, "returned", status="returned",
                     closed_by=actor_id, closed_at=int(time.time()))
    await publish(bot, await dao.get(case["id"]))
    return True


async def refresh_chats(bot, case: dict, *, remove: bool) -> dict:
    if await mark_returned(bot, case):
        return await dao.get(case["id"])
    membership = await main_member(bot, case["user_id"])
    if membership is None:
        # Keep previous observations, but do not remove anybody without a confirmed exit.
        return case
    states = {}
    for chat_id in OTHER_CHATS:
        try:
            member = await bot.get_chat_member(chat_id, case["user_id"])
            status = member.status
            if status in {"left", "kicked"} or (
                status == "restricted" and not getattr(member, "is_member", False)
            ):
                states[str(chat_id)] = "немає"
            elif status in {"administrator", "creator"} or case["user_id"] == BOT_OWNER_ID:
                states[str(chat_id)] = "адміністратор — вилучити вручну"
            elif remove:
                # unbanChatMember also removes a current member and permits later rejoining.
                if await main_member(bot, case["user_id"]) is not False:
                    return case
                await bot.unban_chat_member(chat_id, case["user_id"])
                confirmed = await bot.get_chat_member(chat_id, case["user_id"])
                states[str(chat_id)] = "немає" if confirmed.status in {"left", "kicked"} else "вилучення не підтверджено"
            else:
                states[str(chat_id)] = "присутній"
        except Exception:
            logger.exception("Exit chat check failed", extra={"case_id": case["id"], "chat_id": chat_id})
            states[str(chat_id)] = "не вдалося перевірити — вручну"
    await dao.change(case["id"], None, "chats_checked", chat_states=json.dumps(states, ensure_ascii=False))
    updated = await dao.get(case["id"])
    await publish(bot, updated)
    return updated


async def run_cleanup(bot, check_id: int) -> None:
    case = await dao.get(check_id)
    if case and case["status"] == "open":
        await refresh_chats(bot, case, remove=True)


async def run_reminder(bot, check_id: int, number: int) -> None:
    case = await dao.get(check_id)
    if not case or case["status"] != "open":
        return
    await publish(bot, case)  # Reveal the no-response action after the waiting period.
    await bot.send_message(
        ADMIN_LOG_CHAT_ID,
        f"⏳ Перевірка виходу #{check_id} досі відкрита: {escape(case['nickname'] or case['telegram_name'])}.",
    )
    await schedule_task(
        EXIT_REMINDER_TASK, int(time.time()) + 86400, user_id=case["user_id"],
        payload_json=json.dumps({"id": check_id, "number": number + 1}),
        dedupe_key=f"exit:reminder:{check_id}:{number + 1}",
    )


async def recover_open_cards(bot) -> None:
    """Finish notification/scheduling interrupted by a restart."""
    for case in await dao.open_cases():
        if case["message_id"]:
            continue
        try:
            await publish(bot, case)
            await schedule_task(
                EXIT_CLEANUP_TASK, int(time.time()) + 30, user_id=case["user_id"],
                payload_json=str(case["id"]), dedupe_key=f"exit:cleanup:{case['id']}",
            )
            await schedule_task(
                EXIT_REMINDER_TASK, int(time.time()) + 86400, user_id=case["user_id"],
                payload_json=json.dumps({"id": case["id"], "number": 1}),
                dedupe_key=f"exit:reminder:{case['id']}:1",
            )
        except Exception:
            logger.exception("Could not recover exit card", extra={"case_id": case["id"]})
