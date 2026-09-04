"""Attendance review orchestration and Telegram rendering."""

from __future__ import annotations

import logging
from html import escape

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.core.config import ADMIN_LOG_CHAT_ID
from app.core.db import schedule_task
from app.dao import event_reviews as reviews_dao
from app.dao import events as events_dao
from app.handlers.events.keyboards import review_keyboard
from app.services import event_service


logger = logging.getLogger(__name__)

RESULT_LABELS = {
    "present": "✅ присутній",
    "no_show": "❌ не з’явився",
    "late_decline": "🕒 пізня відмова",
    "excluded": "➖ не враховувати",
    None: "не оцінено",
}


def render_review(review: dict) -> str:
    status = str(review["status"])
    lines = [
        f'📋 <b>Перевірка присутності — «{escape(str(review["title"]))}»</b>',
        "",
    ]
    if status == "completed":
        lines.append("Статус: ✅ завершено")
    else:
        lines.append("Оберіть фактичний результат для кожного гравця.")
    lines.extend(("", f'Оцінено {review["done"]} із {review["total"]}:'))
    start = int(review["page"]) * 10
    for offset, player in enumerate(review["players"], start=start + 1):
        nickname = escape(str(player["nickname"]))
        label = RESULT_LABELS.get(player.get("result"), "не оцінено")
        lines.append(
            f'{offset}. <a href="tg://user?id={int(player["user_id"])}">'
            f"{nickname}</a> — {label}"
        )
        if player.get("result") == "excluded" and player.get("exclusion_reason"):
            lines.append(f'   Причина: {escape(str(player["exclusion_reason"]))}')
    counts = review["counts"]
    lines.extend(
        (
            "",
            "Підсумок: "
            f"✅ {counts.get('present', 0)} · "
            f"❌ {counts.get('no_show', 0)} · "
            f"🕒 {counts.get('late_decline', 0)} · "
            f"➖ {counts.get('excluded', 0)}",
        )
    )
    return "\n".join(lines)


async def create_and_send_review(bot, event_id: int, *, expected_at: int, now: int) -> str:
    transition = await reviews_dao.create_review(
        event_id,
        expected_at=expected_at,
        now=now,
    )
    if transition.code not in {"created", "already"}:
        if transition.code == "cancelled":
            from app.handlers.events.keyboards import public_event_keyboard

            await event_service.refresh_event_card(
                bot, event_id, reply_markup_factory=public_event_keyboard
            )
        return transition.code
    review_created_at = int(transition.finalized_at or now)
    await _schedule_review_reminder(event_id, review_created_at + 6 * 3600)
    if not await reviews_dao.reserve_admin_delivery(
        event_id,
        kind="review_create",
        scheduled_at=expected_at,
        now=now,
    ):
        return "duplicate"
    review = await reviews_dao.get_review(event_id)
    if not review or review["status"] != "awaiting_review":
        await reviews_dao.finish_admin_delivery(
            event_id, kind="review_create", scheduled_at=expected_at,
            now=now, error="review is no longer active",
        )
        return "stale"
    try:
        sent = await bot.send_message(
            ADMIN_LOG_CHAT_ID,
            render_review(review),
            parse_mode="HTML",
            reply_markup=review_keyboard(review),
            disable_web_page_preview=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await reviews_dao.finish_admin_delivery(
            event_id, kind="review_create", scheduled_at=expected_at,
            now=now, error=str(exc),
        )
        return "failed"
    except Exception as exc:
        logger.exception("event review delivery result is unknown", extra={"event_id": event_id})
        await reviews_dao.finish_admin_delivery(
            event_id, kind="review_create", scheduled_at=expected_at,
            now=now, error=f"unknown: {exc}",
        )
        return "unknown"
    await reviews_dao.finish_admin_delivery(
        event_id, kind="review_create", scheduled_at=expected_at,
        now=now, message_id=sent.message_id,
    )
    return "sent"


async def _schedule_review_reminder(event_id: int, scheduled_at: int) -> None:
    from app.services.event_jobs import EVENT_REVIEW_REMINDER_TASK, _payload

    await schedule_task(
        task_type=EVENT_REVIEW_REMINDER_TASK,
        run_at=scheduled_at,
        user_id=event_id,
        payload_json=_payload(event_id, scheduled_at),
        dedupe_key=f"event:{event_id}:review_reminder:{scheduled_at}",
    )


async def send_review_reminder(bot, event_id: int, *, expected_at: int, now: int) -> str:
    review = await reviews_dao.get_review(event_id)
    if not review or review["status"] != "awaiting_review":
        return "skipped"
    if not await reviews_dao.reserve_admin_delivery(
        event_id, kind="review_reminder", scheduled_at=expected_at, now=now
    ):
        latest = await reviews_dao.get_review(event_id)
        if latest and latest["status"] == "awaiting_review":
            await _schedule_review_reminder(event_id, now + 24 * 3600)
        return "duplicate"
    try:
        sent = await bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f'⚠️ Перевірку події «{escape(str(review["title"]))}» ще не завершено. '
            f'Оцінено {review["done"]} із {review["total"]}.',
            parse_mode="HTML",
            reply_markup=review_keyboard(review),
        )
    except Exception as exc:
        logger.exception("event review reminder failed", extra={"event_id": event_id})
        await reviews_dao.finish_admin_delivery(
            event_id, kind="review_reminder", scheduled_at=expected_at,
            now=now, error=str(exc),
        )
    else:
        await reviews_dao.finish_admin_delivery(
            event_id, kind="review_reminder", scheduled_at=expected_at,
            now=now, message_id=sent.message_id,
        )
    latest = await reviews_dao.get_review(event_id)
    if latest and latest["status"] == "awaiting_review":
        await _schedule_review_reminder(event_id, now + 24 * 3600)
    return "sent"


async def refresh_review_message(message, event_id: int, page: int = 0) -> dict | None:
    review = await reviews_dao.get_review(event_id, page=page)
    if not review:
        return None
    await message.edit_text(
        render_review(review),
        parse_mode="HTML",
        reply_markup=review_keyboard(review),
        disable_web_page_preview=True,
    )
    return review


async def finalize_review(bot, event_id: int, actor_id: int, *, now: int):
    result = await reviews_dao.request_or_finalize(event_id, actor_id=actor_id, now=now)
    if result.code == "finalized":
        from app.handlers.events.keyboards import public_event_keyboard

        await event_service.refresh_event_card(
            bot, event_id, reply_markup_factory=public_event_keyboard
        )
    return result


async def publish_cancellation(bot, event_id: int) -> None:
    """Update the public card and post one separate cancellation notice."""

    from app.handlers.events.keyboards import public_event_keyboard

    await event_service.refresh_event_card(
        bot, event_id, reply_markup_factory=public_event_keyboard
    )
    card = await events_dao.get_event_card(event_id)
    if not card or not card.get("publication"):
        return
    publication = card["publication"]
    if not publication.get("message_id"):
        return
    reason = card.get("cancel_reason") or card.get("annul_reason") or "не вказана"
    verb = "анульовано" if card["status"] == "annulled" else "скасовано"
    try:
        await bot.send_message(
            int(publication["chat_id"]),
            f'⚠️ Подію «{escape(str(card["title"]))}» {verb}.\n'
            f'Причина: {escape(str(reason))}',
            parse_mode="HTML",
            reply_to_message_id=int(publication["message_id"]),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.exception(
            "event cancellation notice could not be delivered",
            extra={"event_id": event_id},
        )
