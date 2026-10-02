"""Administrative controls for clan exit checks."""

from __future__ import annotations

import json
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.core.access import has_admin_level
from app.core.config import ADMIN_LOG_CHAT_ID
from app.dao import clan_exit as dao
from app.services import clan_exit as service

router = Router()


class Explanation(StatesGroup):
    text = State()


@router.callback_query(F.data.startswith("exit:"))
async def exit_action(query: CallbackQuery, state: FSMContext) -> None:
    if not query.message or query.message.chat.id != ADMIN_LOG_CHAT_ID or not query.data:
        await query.answer("Ця дія доступна лише в чаті адміністрації.", show_alert=True)
        return
    if not await has_admin_level(query.from_user.id, 2):
        await query.answer("Потрібна роль адміністратора або вище.", show_alert=True)
        return
    try:
        _, action, raw_id = query.data.split(":", 2)
        case = await dao.get(int(raw_id))
    except (ValueError, TypeError):
        await query.answer("Некоректна перевірка.", show_alert=True)
        return
    if not case or case["status"] != "open" or case["message_id"] != query.message.message_id:
        await query.answer("Ця картка вже неактивна.", show_alert=True)
        return
    check_id = case["id"]
    actor = query.from_user.id
    if action == "game":
        await dao.change(check_id, actor, "game_confirmed", game_by=actor, game_at=int(time.time()))
    elif action == "contact":
        await dao.change(check_id, actor, "contact_confirmed", contact_by=actor,
                         contact_at=int(time.time()), explanation_status="requested")
    elif action == "explain":
        await state.set_state(Explanation.text)
        await state.update_data(exit_check_id=check_id)
        await query.answer("Надішли короткий виклад пояснення сюди, у чат адміністрації.", show_alert=True)
        return
    elif action == "unavailable":
        if not case["contact_at"] or time.time() - case["contact_at"] < service.EXPLANATION_WAIT_SECONDS:
            await query.answer("Потрібен зафіксований запит і 24 години очікування.", show_alert=True)
            return
        await dao.change(check_id, actor, "explanation_unavailable",
                         explanation_status="unavailable", explanation_by=actor,
                         explanation_at=int(time.time()), explanation_text=None)
    elif action == "refresh":
        updated = await service.refresh_chats(query.bot, case, remove=True)
        await query.answer("Перевірено." if updated != case else "Головний чат зараз недоступний для перевірки.")
        return
    elif action == "return":
        if not await service.mark_returned(query.bot, case, actor):
            await query.answer("Повернення в головний чат не підтверджено.", show_alert=True)
        else:
            await query.answer("Повернення підтверджено.")
        return
    elif action == "later":
        await dao.change(check_id, actor, "postponed", chat_states=case["chat_states"])
        await query.answer("Перевірка лишається відкритою. Нагадування надійде через добу.")
        return
    elif action == "finish":
        if await service.mark_returned(query.bot, case, actor):
            await query.answer("Гравець повернувся. Перевірку закрито як повернення.", show_alert=True)
            return
        if await service.main_member(query.bot, case["user_id"]) is None:
            await query.answer("Не вдалося перевірити головний чат.", show_alert=True)
            return
        case = await service.refresh_chats(query.bot, case, remove=False)
        if (case["status"] != "open" or not case["game_at"]
            or case["explanation_status"] not in {"explained", "unavailable"}
            or not all(json.loads(case["chat_states"]).get(str(chat_id)) == "немає"
                       for chat_id in service.OTHER_CHATS)):
            await query.answer("Є незавершені кроки. Картку оновлено.", show_alert=True)
            return
        await dao.change(check_id, actor, "closed", status="closed",
                         closed_by=actor, closed_at=int(time.time()))
    else:
        await query.answer("Невідома дія.", show_alert=True)
        return
    updated = await dao.get(check_id)
    if updated:
        await service.publish(query.bot, updated)
    await query.answer("Збережено.")


@router.message(Explanation.text, F.chat.id == ADMIN_LOG_CHAT_ID)
async def record_explanation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    case = await dao.get(int(data.get("exit_check_id", 0)))
    if not message.from_user or not await has_admin_level(message.from_user.id, 2):
        await state.clear()
        return
    if not case or case["status"] != "open":
        await state.clear()
        await message.answer("Перевірка вже закрита.")
        return
    summary = (message.text or "").strip()
    if not summary or summary.startswith("/") or len(summary) > 500:
        await message.answer("Надішли короткий текст пояснення (до 500 символів).")
        return
    actor = message.from_user.id
    await dao.change(case["id"], actor, "explanation_recorded",
                     explanation_status="explained", explanation_text=summary,
                     explanation_by=actor, explanation_at=int(time.time()))
    await state.clear()
    await service.publish(message.bot, await dao.get(case["id"]))
    await message.answer("Пояснення записано в картці перевірки.")
