"""Administrative controls for clan departure verification."""

from __future__ import annotations

import json
import logging
import time
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.core.access import has_admin_level
from app.core.config import ADMIN_LOG_CHAT_ID
from app.core.db import cancel_pending_tasks
from app.dao import clan_exit as dao
from app.services import clan_exit as service

router = Router()
logger = logging.getLogger(__name__)

ACTIONS = {
    "game", "contact", "explain", "unavailable", "refresh", "later",
    "defer12", "defer24", "defer72", "cancel", "finish",
}


class Explanation(StatesGroup):
    text = State()


async def _answer(query: CallbackQuery, text: str, *, show_alert: bool = False) -> None:
    try:
        await service.telegram_call(query.answer(text, show_alert=show_alert))
    except Exception:
        logger.exception("Cannot answer exit callback", extra={"callback_id": query.id})


async def _notify(message: Message, text: str) -> None:
    try:
        await service.telegram_call(message.answer(text, parse_mode=None))
    except Exception:
        logger.exception("Cannot send exit action feedback")


@router.callback_query(F.data.startswith("exit:"))
async def exit_action(query: CallbackQuery, state: FSMContext) -> None:
    if not query.message or query.message.chat.id != ADMIN_LOG_CHAT_ID or not query.data:
        await _answer(query, "Ця дія доступна лише в чаті адміністрації.", show_alert=True)
        return
    try:
        if not await has_admin_level(query.from_user.id, 2):
            await _answer(query, "Потрібна роль адміністратора або вище.", show_alert=True)
            return
        try:
            _, action, raw_id = query.data.split(":", 2)
            check_id = int(raw_id)
        except (ValueError, TypeError):
            await _answer(query, "Некоректна перевірка.", show_alert=True)
            return
        if action not in ACTIONS:
            await _answer(query, "Невідома дія.", show_alert=True)
            return
        case = await dao.get(check_id)
        if (not case or case["message_id"] != query.message.message_id
                or (case["status"] != "open" and action != "refresh")):
            await _answer(query, "Ця картка вже неактивна.", show_alert=True)
            return
        if action == "unavailable" and (
                case["explanation_status"] != "requested" or not case["contact_at"]
                or time.time() - case["contact_at"] < service.EXPLANATION_WAIT_SECONDS):
            await _answer(query, "Потрібен чинний запит і 24 години очікування.", show_alert=True)
            return
        if ((action == "game" and case["game_at"])
                or (action == "contact" and case["explanation_status"] != "not_requested")
                or (action == "explain"
                    and case["explanation_status"] not in {"not_requested", "requested"})):
            await _answer(query, "Цю дію вже виконано або її стан змінився.", show_alert=True)
            return
    except Exception:
        logger.exception("Cannot load exit case", extra={"callback_data": query.data})
        await _answer(query, "Не вдалося відкрити перевірку. Спробуй ще раз.", show_alert=True)
        return

    await _answer(
        query, "Надішли короткий виклад пояснення сюди, у чат адміністрації."
        if action == "explain"
        else ("Перевіряю…" if action in {"refresh", "finish"} else "Обробляю…"),
        show_alert=action == "explain",
    )
    try:
        await dao.remember_actor(query.from_user)
        await _apply_action(query, state, action, case)
    except service.CardPublishError:
        logger.exception("Cannot update exit card", extra={"case_id": check_id, "action": action})
        await _notify(query.message,
                      f"⚠️ Перевірка #{check_id}: дані збережено, але картку не вдалося оновити. "
                      "Натисни «Перевірити чати», щоб повторити оновлення.")
    except Exception:
        logger.exception("Exit action failed", extra={"case_id": check_id, "action": action})
        await _notify(query.message,
                      f"⚠️ Не вдалося завершити дію для перевірки #{check_id}. Спробуй ще раз.")


async def _apply_action(query: CallbackQuery, state: FSMContext, action: str, case: dict) -> None:
    check_id = case["id"]
    actor = query.from_user.id
    now = int(time.time())
    changed: bool | None = None

    if action == "game":
        changed = await dao.change(check_id, actor, "game_confirmed",
                                   game_by=actor, game_at=now)
    elif action == "contact":
        changed = await dao.change(check_id, actor, "contact_confirmed",
                                   contact_by=actor, contact_at=now,
                                   explanation_status="requested")
    elif action == "explain":
        await state.set_state(Explanation.text)
        await state.update_data(exit_check_id=check_id)
        return
    elif action == "unavailable":
        changed = await dao.change(check_id, actor, "explanation_unavailable",
                                   explanation_status="unavailable", explanation_by=actor,
                                   explanation_at=now, explanation_text=None)
    elif action == "later":
        await service.telegram_call(query.bot.edit_message_reply_markup(
            chat_id=ADMIN_LOG_CHAT_ID, message_id=case["message_id"],
            reply_markup=service.defer_keyboard(case),
        ))
        return
    elif action == "cancel":
        await service.telegram_call(query.bot.edit_message_reply_markup(
            chat_id=ADMIN_LOG_CHAT_ID, message_id=case["message_id"],
            reply_markup=service.keyboard(case),
        ))
        return
    elif action.startswith("defer"):
        hours = int(action.removeprefix("defer"))
        changed = await dao.change(check_id, actor, "postponed",
                                   deferred_by=actor, deferred_until=now + hours * 3600)
        if changed:
            await service.schedule_deferred_reminder(await dao.get(check_id))
    elif action == "refresh":
        if case["status"] != "open":
            # Re-publish a card if the previous Telegram edit failed after closure.
            await service.publish(query.bot, case)
            return
        await service.refresh_chats(query.bot, case, remove=True, actor_id=actor)
        return
    elif action == "finish":
        case = await service.refresh_chats(query.bot, case, remove=False, actor_id=actor)
        if case["status"] != "open":
            return
        states = json.loads(case["chat_states"])
        if (states.get(str(service.MAIN_CHAT_ID)) != "немає" or not case["game_at"]
                or case["explanation_status"] not in {"explained", "unavailable"}
                or not all(states.get(str(chat_id)) == "немає"
                           for chat_id in service.OTHER_CHATS)):
            await _notify(query.message, f"Перевірка #{check_id}: є незавершені кроки. Картку оновлено.")
            return
        changed = await dao.change(check_id, actor, "closed", status="closed",
                                   closed_by=actor, closed_at=now)
        if changed:
            await cancel_pending_tasks(service.EXIT_REMINDER_TASK, user_id=case["user_id"])
            await cancel_pending_tasks(service.EXIT_CLEANUP_TASK, user_id=case["user_id"])

    if changed is False:
        await _notify(query.message, "Цю дію вже виконано або перевірка змінила статус.")
    updated = await dao.get(check_id)
    if updated:
        await service.publish(query.bot, updated)
    if action.startswith("defer") and changed:
        await _notify(query.message, f"Перевірку #{check_id} відкладено до "
                      f"{service._date(updated['deferred_until'])}.")


@router.message(Explanation.text, F.chat.id == ADMIN_LOG_CHAT_ID)
async def record_explanation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    case = await dao.get(int(data.get("exit_check_id", 0)))
    if not message.from_user or not await has_admin_level(message.from_user.id, 2):
        await state.clear()
        return
    if (not case or case["status"] != "open"
            or case["explanation_status"] not in {"not_requested", "requested"}):
        await state.clear()
        await _notify(message, "Пояснення вже зафіксовано або перевірку закрито.")
        return
    summary = (message.text or "").strip()
    if not summary or summary.startswith("/") or len(summary) > 500:
        await _notify(message, "Надішли короткий текст пояснення (до 500 символів).")
        return
    actor = message.from_user.id
    await dao.remember_actor(message.from_user)
    changed = await dao.change(case["id"], actor, "explanation_recorded",
                               explanation_status="explained", explanation_text=summary,
                               explanation_by=actor, explanation_at=int(time.time()))
    await state.clear()
    if not changed:
        await _notify(message, "Пояснення вже збережено іншим адміністратором.")
        return
    try:
        await service.publish(message.bot, await dao.get(case["id"]))
    except service.CardPublishError:
        logger.exception("Cannot publish recorded explanation", extra={"case_id": case["id"]})
        await _notify(message, f"⚠️ Пояснення для перевірки #{case['id']} збережено, "
                      "але картку не вдалося оновити. Натисни «Перевірити чати».")
        return
    await _notify(message, "Пояснення записано в картці перевірки.")


@router.message(Command("exitpending"), F.chat.id == ADMIN_LOG_CHAT_ID)
async def pending_exits(message: Message) -> None:
    if not message.from_user or not await has_admin_level(message.from_user.id, 2):
        await _notify(message, "Потрібна роль адміністратора або вище.")
        return
    cases = await dao.list_open()
    if not cases:
        await message.answer("✅ Відкритих перевірок виходу немає.")
        return
    lines = [f"📋 <b>Відкриті перевірки (показано {len(cases)}, максимум 30):</b>"]
    for case in cases:
        name = escape(case["nickname"] or case["telegram_name"])
        reason = []
        if not case["game_at"]:
            reason.append("гра")
        if case["explanation_status"] not in {"explained", "unavailable"}:
            reason.append("пояснення")
        states = json.loads(case["chat_states"])
        if states.get(str(service.MAIN_CHAT_ID)) != "немає" or not all(
                states.get(str(chat_id)) == "немає" for chat_id in service.OTHER_CHATS):
            reason.append("чати")
        if case.get("deferred_until") and case["deferred_until"] > int(time.time()):
            reason.append(f"відкладено до {service._date(case['deferred_until'])}")
        lines.append(f"• <b>#{case['id']}</b> {name} · {service._date(case['departed_at'])}"
                     f"\n  Очікує: {', '.join(reason) or 'завершення'}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("exithistory"), F.chat.id == ADMIN_LOG_CHAT_ID)
async def exit_history(message: Message) -> None:
    if not message.from_user or not await has_admin_level(message.from_user.id, 2):
        await _notify(message, "Потрібна роль адміністратора або вище.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await _notify(message, "Формат: /exithistory 123")
        return
    case_id = int(parts[1])
    if not await dao.get(case_id):
        await _notify(message, "Перевірку не знайдено.")
        return
    history = await dao.action_history(case_id)
    if not history:
        await _notify(message, "Історія дій порожня.")
        return
    names = await dao.actor_labels({int(item["actor_id"]) for item in history
                                    if item["actor_id"] is not None})
    labels = {
        "detected": "виявлено вихід",
        "chats_checked": "перевірено чати",
        "game_confirmed": "підтверджено вилучення з гри",
        "contact_confirmed": "зафіксовано запит",
        "explanation_recorded": "записано пояснення",
        "explanation_unavailable": "пояснення не отримано",
        "postponed": "відкладено",
        "reminder_sent": "надіслано нагадування",
        "closed": "перевірку завершено",
        "returned": "повернення підтверджено",
    }
    lines = [f"📜 <b>Історія перевірки #{case_id}</b> (останні 20):"]
    for item in history:
        reviewer = (service._actor_mention(item["actor_id"], names)
                    if item["actor_id"] is not None else "автоматично")
        lines.append(f"• {service._date(item['created_at'])} · "
                     f"{escape(labels.get(item['action'], item['action']))} — {reviewer}")
    await message.answer("\n".join(lines), parse_mode="HTML")
