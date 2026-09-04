"""Administrative `/event` panel and event creation flow."""

from __future__ import annotations

import logging
from datetime import date

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.core.access import can_manage_events, get_effective_admin_level
from app.core.dates import today_kyiv
from app.dao import events as events_dao
from app.dao import event_lifecycle as lifecycle_dao
from app.dao import event_reviews as reviews_dao
from app.handlers.events.keyboards import (
    admin_menu_keyboard,
    back_to_form_keyboard,
    calendar_keyboard,
    cancellable_events_keyboard,
    cancel_confirm_keyboard,
    delete_draft_keyboard,
    draft_form_keyboard,
    editable_events_keyboard,
    edit_form_keyboard,
    edit_preview_keyboard,
    existing_draft_keyboard,
    missing_events_keyboard,
    public_event_keyboard,
    publish_confirmation_keyboard,
    recover_publication_keyboard,
    reminder_audience_keyboard,
    reminder_events_keyboard,
    review_events_keyboard,
    type_keyboard,
)
from app.handlers.events.states import EventDraftInput, EventReviewInput
from app.services import event_service
from app.services.event_notifications import send_manual_reminder
from app.services.event_render import (
    render_admin_menu,
    render_calendar_title,
    render_draft_form,
    render_public_card,
    render_text_prompt,
)


router = Router()
logger = logging.getLogger(__name__)

ACCESS_DENIED = (
    "Керування подіями доступне лише адміністрації рівня 3–4 в адмін-чаті."
)
MENU_LOCKED = "Це меню належить іншому адміністратору. Використайте власну команду /event."
STALE_PANEL = "Ця панель застаріла. Використайте актуальну команду /event."
DRAFT_EXPIRED = "Чернетку видалено після 48 годин без дій. Створіть нову подію."
INPUT_DELETE_WARNING = (
    "Дані збережено, але бот не зміг видалити ваше повідомлення. "
    "Перевірте право «Видалення повідомлень»."
)
PUBLICATION_LOCKED = (
    "Не вдалося підтвердити результат попередньої публікації. "
    "Автоматичного повтору не буде, щоб не створити дубль."
)


async def _show_menu(message: Message, admin_id: int, notice: str | None = None) -> None:
    counts = await events_dao.event_menu_counts(event_service.now_timestamp())
    await message.edit_text(
        render_admin_menu(counts, notice),
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard(admin_id, missing_count=counts["missing"]),
    )


async def _check_callback(callback: CallbackQuery, admin_id: int) -> bool:
    if not callback.from_user or callback.from_user.id != admin_id:
        await callback.answer(MENU_LOCKED, show_alert=True)
        return False
    if not callback.message or not await can_manage_events(
        callback.from_user.id,
        callback.message.chat.id,
    ):
        await callback.answer(ACCESS_DENIED, show_alert=True)
        return False
    return True


async def _load_panel_draft(
    callback: CallbackQuery,
    admin_id: int,
) -> dict | None:
    draft, expired = await event_service.load_draft(admin_id)
    if draft is None:
        notice = DRAFT_EXPIRED if expired else "Активної чернетки немає."
        await _show_menu(callback.message, admin_id, notice)
        await callback.answer()
        return None
    if draft.get("target_event_status") in {"publishing", "publication_unknown"}:
        await callback.message.edit_text(
            f"⚠️ {PUBLICATION_LOCKED}",
            reply_markup=admin_menu_keyboard(admin_id),
        )
        await callback.answer("Публікацію заблоковано від повтору.", show_alert=True)
        return None
    if draft.get("draft_kind") == "edit" and draft.get(
        "target_event_status"
    ) not in {"published", "registration_closed", "started", "awaiting_review"}:
        await events_dao.delete_draft(admin_id)
        await _show_menu(
            callback.message,
            admin_id,
            "Стан події змінився. Редагування припинено.",
        )
        await callback.answer("Чернетка редагування застаріла.", show_alert=True)
        return None
    if (
        int(draft["menu_chat_id"]) != callback.message.chat.id
        or int(draft["menu_message_id"] or 0) != callback.message.message_id
    ):
        await callback.answer(STALE_PANEL, show_alert=True)
        return None
    return draft


async def _show_form(
    callback: CallbackQuery,
    admin_id: int,
    draft: dict,
    notice: str | None = None,
) -> None:
    payload = draft["payload"]
    is_edit = draft.get("draft_kind") == "edit"
    await callback.message.edit_text(
        render_draft_form(payload, notice, edit=is_edit),
        parse_mode="HTML",
        reply_markup=(
            edit_form_keyboard(
                admin_id,
                has_description=bool(payload.get("description")),
            )
            if is_edit
            else draft_form_keyboard(
                admin_id,
                has_description=bool(payload.get("description")),
            )
        ),
    )


@router.message(Command("event"))
async def event_menu_handler(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await can_manage_events(
        message.from_user.id,
        message.chat.id,
    ):
        await message.answer(ACCESS_DENIED)
        return
    await state.clear()
    await event_service.refresh_public_cards(
        message.bot,
        reply_markup_factory=public_event_keyboard,
    )
    counts = await events_dao.event_menu_counts(event_service.now_timestamp())
    await message.answer(
        render_admin_menu(counts),
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard(
            message.from_user.id,
            missing_count=counts["missing"],
        ),
    )


@router.callback_query(F.data.startswith("ev:a:"))
async def event_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.message:
        return
    parts = callback.data.split(":", 3)
    if len(parts) != 4 or not parts[2].isdigit():
        await callback.answer("Некоректна дія.", show_alert=True)
        return
    admin_id = int(parts[2])
    action = parts[3]
    if not await _check_callback(callback, admin_id):
        return

    if action == "noop":
        await callback.answer()
        return

    if action == "menu":
        await state.clear()
        await _show_menu(callback.message, admin_id)
        await callback.answer()
        return

    if action == "create":
        draft, expired = await event_service.load_draft(admin_id)
        if draft:
            if draft.get("target_event_status") in {
                "publishing",
                "publication_unknown",
            }:
                await callback.message.edit_text(
                    f"⚠️ {PUBLICATION_LOCKED}",
                    reply_markup=admin_menu_keyboard(admin_id),
                )
                await callback.answer(
                    "Публікацію заблоковано від повтору.",
                    show_alert=True,
                )
                return
            await event_service.touch_draft_menu(
                admin_id,
                menu_chat_id=callback.message.chat.id,
                menu_message_id=callback.message.message_id,
            )
            await callback.message.edit_text(
                "📝 У вас уже є активна чернетка. Продовжити її чи видалити?",
                reply_markup=existing_draft_keyboard(admin_id),
            )
            await callback.answer()
            return
        draft = await event_service.create_draft(
            admin_id,
            menu_chat_id=callback.message.chat.id,
            menu_message_id=callback.message.message_id,
        )
        await _show_form(callback, admin_id, draft, DRAFT_EXPIRED if expired else None)
        await callback.answer("Нову чернетку створено.")
        return

    if action == "edit":
        existing, _ = await event_service.load_draft(admin_id)
        if existing:
            await event_service.touch_draft_menu(
                admin_id,
                menu_chat_id=callback.message.chat.id,
                menu_message_id=callback.message.message_id,
            )
            await callback.message.edit_text(
                "📝 У вас уже є активна чернетка. Продовжити її чи видалити?",
                reply_markup=existing_draft_keyboard(admin_id),
            )
            await callback.answer()
            return
        level = await get_effective_admin_level(admin_id)
        current = event_service.now_timestamp()
        events = await events_dao.list_editable_events(
            now=current,
            include_started=level >= 4,
        )
        if not events:
            await _show_menu(
                callback.message,
                admin_id,
                "Немає подій, доступних для редагування.",
            )
        else:
            await callback.message.edit_text(
                "✏️ <b>Редагування події</b>\n\nОберіть подію.",
                parse_mode="HTML",
                reply_markup=editable_events_keyboard(admin_id, events),
            )
        await callback.answer()
        return

    if action.startswith("edit_event_"):
        try:
            existing, _ = await event_service.load_draft(admin_id)
            if existing:
                await callback.answer(
                    "Спочатку завершіть або видаліть активну чернетку.",
                    show_alert=True,
                )
                return
            event_id = int(action.removeprefix("edit_event_"))
            draft = await event_service.create_edit_draft(
                admin_id,
                event_id,
                admin_level=await get_effective_admin_level(admin_id),
                menu_chat_id=callback.message.chat.id,
                menu_message_id=callback.message.message_id,
            )
        except (ValueError, events_dao.EventEditPermissionError):
            await callback.answer(
                "Цю подію вже не можна редагувати.",
                show_alert=True,
            )
            return
        await _show_form(callback, admin_id, draft)
        await callback.answer()
        return

    if action == "missing":
        missing_events = await events_dao.list_missing_events()
        if not missing_events:
            await _show_menu(
                callback.message,
                admin_id,
                "Втрачених публікацій більше немає.",
            )
        else:
            await callback.message.edit_text(
                "⚠️ <b>Втрачені публікації</b>\n\n"
                "Оберіть подію для повторної публікації або скасування.",
                parse_mode="HTML",
                reply_markup=missing_events_keyboard(admin_id, missing_events),
            )
        await callback.answer()
        return

    if action.startswith("recover_"):
        try:
            event_id = int(action.removeprefix("recover_"))
        except ValueError:
            await callback.answer("Некоректна подія.", show_alert=True)
            return
        card = await events_dao.get_event_card(event_id)
        if not card or not card.get("publication_missing"):
            await callback.answer("Ця публікація вже актуальна.", show_alert=True)
            return
        await callback.message.edit_text(
            f'⚠️ Повідомлення активної події «{card["title"]}» не знайдено '
            "в головному чаті.",
            reply_markup=recover_publication_keyboard(admin_id, event_id),
        )
        await callback.answer()
        return

    if action.startswith("republish_"):
        try:
            event_id = int(action.removeprefix("republish_"))
            result = await event_service.republish_event(
                callback.bot,
                event_id,
                admin_id,
                reply_markup_factory=public_event_keyboard,
            )
        except (ValueError, events_dao.PublicationStateError):
            await callback.answer("Повторна публікація вже не потрібна.", show_alert=True)
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            await callback.answer(
                "Не вдалося опублікувати картку. Перевірте права бота.",
                show_alert=True,
            )
            return
        if result.status == "published":
            await _show_menu(
                callback.message,
                admin_id,
                f'Подію «{result.title}» опубліковано повторно.',
            )
            await callback.answer("Публікацію відновлено.")
        else:
            await callback.message.edit_text(
                f"⚠️ {PUBLICATION_LOCKED}",
                reply_markup=admin_menu_keyboard(admin_id),
            )
            await callback.answer("Результат доставки невідомий.", show_alert=True)
        return

    if action.startswith("cancel_missing_") or action.startswith("cancel_event_"):
        marker = "cancel_missing_" if action.startswith("cancel_missing_") else "cancel_event_"
        try:
            event_id = int(action.removeprefix(marker))
        except ValueError:
            await callback.answer("Некоректна подія.", show_alert=True)
            return
        await state.set_state(EventReviewInput.cancellation_reason)
        await state.update_data(
            event_id=event_id,
            admin_id=admin_id,
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
        )
        await callback.message.edit_text(
            "Надішліть коротку причину скасування одним повідомленням — "
            "від 3 до 300 символів."
        )
        await callback.answer()
        return

    if action.startswith("cancel_confirm_"):
        data = await state.get_data()
        try:
            event_id = int(action.removeprefix("cancel_confirm_"))
        except ValueError:
            await callback.answer("Некоректна подія.", show_alert=True)
            return
        if int(data.get("event_id", 0)) != event_id or not data.get("reason"):
            await callback.answer("Підтвердження застаріло.", show_alert=True)
            return
        result = await reviews_dao.request_or_cancel(
            event_id,
            actor_id=admin_id,
            reason=str(data["reason"]),
            now=event_service.now_timestamp(),
        )
        if result.code != "cancelled":
            await callback.answer("Подію вже не можна скасувати.", show_alert=True)
            return
        await state.clear()
        from app.services.event_reviews import publish_cancellation

        await publish_cancellation(callback.bot, event_id)
        await _show_menu(
            callback.message,
            admin_id,
            f'Подію «{result.title}» скасовано. Відповіді та результати '
            "не впливатимуть на рейтинг.",
        )
        await callback.answer("Подію скасовано.")
        return

    if action == "remind":
        events = await lifecycle_dao.list_reminder_events(event_service.now_timestamp())
        if not events:
            await _show_menu(
                callback.message,
                admin_id,
                "Немає активних подій для нагадування.",
            )
        else:
            await callback.message.edit_text(
                "🔔 <b>Ручне нагадування</b>\n\nОберіть подію.",
                parse_mode="HTML",
                reply_markup=reminder_events_keyboard(admin_id, events),
            )
        await callback.answer()
        return

    if action.startswith("remind_send_"):
        try:
            raw_event_id, audience = action.removeprefix("remind_send_").rsplit(
                "_", 1
            )
            event_id = int(raw_event_id)
            result = await send_manual_reminder(
                callback.bot,
                event_id,
                audience=audience,
                actor_id=admin_id,
                now=event_service.now_timestamp(),
            )
        except ValueError:
            await callback.answer("Некоректна дія.", show_alert=True)
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            await callback.answer(
                "Не вдалося надіслати нагадування. Перевірте картку та права бота.",
                show_alert=True,
            )
            return
        if result.code == "sent":
            await _show_menu(
                callback.message,
                admin_id,
                f'Нагадування про подію «{result.title}» надіслано.',
            )
            await callback.answer("Нагадування надіслано.")
        elif result.code == "cooldown":
            minutes = max(1, (result.retry_after + 59) // 60)
            await callback.answer(
                f"Повторне нагадування буде доступне через {minutes} хв.",
                show_alert=True,
            )
        elif result.code == "empty":
            await callback.answer(
                "У вибраній групі немає користувачів.",
                show_alert=True,
            )
        elif result.code == "unknown":
            await callback.answer(
                "Результат доставки невідомий. Автоматичного повтору не буде.",
                show_alert=True,
            )
        else:
            await callback.answer(
                "Нагадування для цієї події вже недоступне.",
                show_alert=True,
            )
        return

    if action.startswith("remind_"):
        try:
            event_id = int(action.removeprefix("remind_"))
        except ValueError:
            await callback.answer("Некоректна подія.", show_alert=True)
            return
        event = await events_dao.get_event_card(event_id)
        if not event or event["status"] not in {"published", "registration_closed"}:
            await callback.answer("Подія вже недоступна.", show_alert=True)
            return
        await callback.message.edit_text(
            f'🔔 Кому нагадати про подію «{event["title"]}»?',
            reply_markup=reminder_audience_keyboard(admin_id, event_id),
        )
        await callback.answer()
        return

    if action == "cancel":
        await state.clear()
        events = await reviews_dao.list_cancellable_events()
        if not events:
            await _show_menu(callback.message, admin_id, "Немає подій для скасування.")
        else:
            await callback.message.edit_text(
                "🚫 <b>Скасування події</b>\n\nОберіть подію.",
                parse_mode="HTML",
                reply_markup=cancellable_events_keyboard(admin_id, events),
            )
        await callback.answer()
        return

    if action == "reviews" or action.startswith("reviews_page_"):
        await state.clear()
        try:
            page = 0 if action == "reviews" else max(
                0, int(action.removeprefix("reviews_page_"))
            )
        except ValueError:
            await callback.answer("Некоректна сторінка.", show_alert=True)
            return
        page_size = 10
        total = await reviews_dao.count_reviews(include_completed=True)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages - 1)
        events = await reviews_dao.list_reviews(
            include_completed=True,
            limit=page_size,
            offset=page * page_size,
        )
        if not events:
            await _show_menu(callback.message, admin_id, "Перевірок ще немає.")
        else:
            await callback.message.edit_text(
                "📋 <b>Перевірки присутності</b>\n\n"
                "Незавершені показані першими; завершені доступні для корекції.",
                parse_mode="HTML",
                reply_markup=review_events_keyboard(
                    events,
                    back_admin_id=admin_id,
                    page=page,
                    pages=pages,
                ),
            )
        await callback.answer()
        return

    draft = await _load_panel_draft(callback, admin_id)
    if draft is None:
        return

    if action == "continue":
        await state.clear()
        await _show_form(callback, admin_id, draft)
    elif action == "delete_ask":
        await callback.message.edit_text(
            "🗑 Видалити цю чернетку? Відновити введені дані буде неможливо.",
            reply_markup=delete_draft_keyboard(admin_id),
        )
    elif action == "delete_yes":
        try:
            await events_dao.delete_draft(admin_id)
        except events_dao.PublicationStateError:
            await callback.answer(
                "Чернетка вже має зарезервовану публікацію і не може бути видалена.",
                show_alert=True,
            )
            return
        await state.clear()
        await _show_menu(callback.message, admin_id, "Чернетку видалено.")
    elif action == "field_title":
        await state.set_state(EventDraftInput.title)
        await state.update_data(
            admin_id=admin_id,
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
            field="title",
        )
        await callback.message.edit_text(
            render_text_prompt("title", draft["payload"]),
            parse_mode="HTML",
            reply_markup=back_to_form_keyboard(admin_id),
        )
    elif action == "field_time":
        await state.set_state(EventDraftInput.time)
        await state.update_data(
            admin_id=admin_id,
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
            field="time",
        )
        await callback.message.edit_text(
            render_text_prompt("time", draft["payload"]),
            parse_mode="HTML",
            reply_markup=back_to_form_keyboard(admin_id),
        )
    elif action == "field_description":
        await state.set_state(EventDraftInput.description)
        await state.update_data(
            admin_id=admin_id,
            panel_chat_id=callback.message.chat.id,
            panel_message_id=callback.message.message_id,
            field="description",
        )
        await callback.message.edit_text(
            render_text_prompt("description", draft["payload"]),
            parse_mode="HTML",
            reply_markup=back_to_form_keyboard(admin_id),
        )
    elif action == "clear_description":
        draft = await event_service.save_draft_field(
            admin_id,
            "description",
            None,
            menu_chat_id=callback.message.chat.id,
            menu_message_id=callback.message.message_id,
        )
        await _show_form(callback, admin_id, draft)
        await callback.answer("Опис прибрано.")
        return
    elif action == "field_type":
        await callback.message.edit_text(
            "🏷 <b>Тип події</b>\n\nОберіть інформаційну позначку події.",
            parse_mode="HTML",
            reply_markup=type_keyboard(admin_id),
        )
    elif action.startswith("type_"):
        draft = await event_service.save_draft_field(
            admin_id,
            "event_type",
            action.removeprefix("type_"),
            menu_chat_id=callback.message.chat.id,
            menu_message_id=callback.message.message_id,
        )
        await _show_form(callback, admin_id, draft)
        await callback.answer("Тип події збережено.")
        return
    elif action == "field_date":
        selected = draft["payload"].get("date")
        month = date.fromisoformat(selected) if selected else today_kyiv()
        await callback.message.edit_text(
            render_calendar_title(month.replace(day=1)),
            parse_mode="HTML",
            reply_markup=calendar_keyboard(admin_id, month.year, month.month),
        )
    elif action.startswith("cal_"):
        try:
            _, raw_year, raw_month = action.split("_", 2)
            month = date(int(raw_year), int(raw_month), 1)
        except ValueError:
            await callback.answer("Некоректний місяць.", show_alert=True)
            return
        await callback.message.edit_text(
            render_calendar_title(month),
            parse_mode="HTML",
            reply_markup=calendar_keyboard(admin_id, month.year, month.month),
        )
    elif action.startswith("date_"):
        draft = await event_service.save_draft_field(
            admin_id,
            "date",
            action.removeprefix("date_"),
            menu_chat_id=callback.message.chat.id,
            menu_message_id=callback.message.message_id,
        )
        await _show_form(callback, admin_id, draft)
        await callback.answer("Дату події збережено.")
        return
    elif action in {"preview", "prepare_publish"}:
        if action == "prepare_publish" and draft.get("draft_kind") == "edit":
            await callback.answer("Некоректна дія.", show_alert=True)
            return
        try:
            validated = await event_service.validate_draft(draft)
        except event_service.EventValidationError as exc:
            await _show_form(callback, admin_id, draft, exc.message)
            await callback.answer("Перевірте дані форми.", show_alert=True)
            return
        if draft.get("draft_kind") == "edit" and action == "preview":
            keyboard = edit_preview_keyboard(admin_id)
        else:
            keyboard = (
                publish_confirmation_keyboard(admin_id)
                if action == "prepare_publish"
                else back_to_form_keyboard(admin_id)
            )
        await callback.message.edit_text(
            render_public_card(event_service.preview_event(validated), preview=True),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    elif action == "save_changes":
        if draft.get("draft_kind") != "edit":
            await callback.answer("Це не чернетка редагування.", show_alert=True)
            return
        try:
            result = await event_service.apply_edit_draft(
                callback.bot,
                admin_id,
                admin_level=await get_effective_admin_level(admin_id),
                reply_markup_factory=public_event_keyboard,
            )
        except event_service.EventValidationError as exc:
            await _show_form(callback, admin_id, draft, exc.message)
            await callback.answer("Зміни не збережено.", show_alert=True)
            return
        except events_dao.EventVersionError:
            await events_dao.delete_draft(admin_id)
            await _show_menu(
                callback.message,
                admin_id,
                "Подію вже змінив інший адміністратор. Відкрийте її повторно.",
            )
            await callback.answer("Чернетка застаріла.", show_alert=True)
            return
        except events_dao.EventEditPermissionError:
            await callback.answer(
                "Подію вже не можна редагувати з вашим рівнем доступу.",
                show_alert=True,
            )
            return
        await state.clear()
        notice = (
            f'Подію «{result["title"]}» перенесено. Відповіді скинуто.'
            if result["rescheduled"]
            else f'Зміни події «{result["title"]}» збережено.'
        )
        await _show_menu(callback.message, admin_id, notice)
        await callback.answer("Зміни збережено.")
        return
    elif action == "publish":
        try:
            result = await event_service.publish_draft(
                callback.bot,
                admin_id,
                reply_markup_factory=public_event_keyboard,
            )
        except event_service.EventValidationError as exc:
            await _show_form(callback, admin_id, draft, exc.message)
            await callback.answer("Публікацію зупинено.", show_alert=True)
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            refreshed, _ = await event_service.load_draft(admin_id)
            if refreshed:
                await _show_form(
                    callback,
                    admin_id,
                    refreshed,
                    "Не вдалося опублікувати подію. Перевірте права бота в головному чаті.",
                )
            await callback.answer("Публікація не виконана.", show_alert=True)
            return

        await state.clear()
        if result.status == "published":
            await callback.message.edit_text(
                f'✅ Подію «{result.title}» опубліковано в головному чаті JokerRecon.',
                reply_markup=admin_menu_keyboard(admin_id),
            )
        elif result.status == "publication_unknown":
            await callback.message.edit_text(
                "⚠️ Не вдалося підтвердити результат публікації. "
                "Автоматичного повтору не буде, щоб не створити дубль.",
                reply_markup=admin_menu_keyboard(admin_id),
            )
        else:
            await callback.answer("Публікація вже обробляється.", show_alert=True)
            return
    else:
        await callback.answer("Невідома дія.", show_alert=True)
        return

    await callback.answer()


async def _delete_input(message: Message) -> bool:
    try:
        await message.delete()
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def _handle_text_field(message: Message, state: FSMContext, field: str) -> None:
    data = await state.get_data()
    if (
        not message.from_user
        or int(data.get("admin_id", 0)) != message.from_user.id
        or not await can_manage_events(message.from_user.id, message.chat.id)
    ):
        await state.clear()
        return
    panel_chat_id = int(data["panel_chat_id"])
    panel_message_id = int(data["panel_message_id"])
    deleted = await _delete_input(message)
    try:
        draft = await event_service.save_draft_field(
            message.from_user.id,
            field,
            message.text or "",
            menu_chat_id=panel_chat_id,
            menu_message_id=panel_message_id,
        )
    except event_service.EventValidationError as exc:
        await message.bot.edit_message_text(
            render_text_prompt(field, {}) + f"\n\n⚠️ {exc.message}",
            chat_id=panel_chat_id,
            message_id=panel_message_id,
            parse_mode="HTML",
            reply_markup=back_to_form_keyboard(message.from_user.id),
        )
        return
    except events_dao.DraftNotFoundError:
        await state.clear()
        await message.bot.edit_message_text(
            DRAFT_EXPIRED,
            chat_id=panel_chat_id,
            message_id=panel_message_id,
            reply_markup=admin_menu_keyboard(message.from_user.id),
        )
        return
    except events_dao.PublicationStateError:
        await state.clear()
        await message.bot.edit_message_text(
            "Стан події змінився. Редагування припинено.",
            chat_id=panel_chat_id,
            message_id=panel_message_id,
            reply_markup=admin_menu_keyboard(message.from_user.id),
        )
        return

    await state.clear()
    notice = None if deleted else INPUT_DELETE_WARNING
    await message.bot.edit_message_text(
        render_draft_form(
            draft["payload"],
            notice,
            edit=draft.get("draft_kind") == "edit",
        ),
        chat_id=panel_chat_id,
        message_id=panel_message_id,
        parse_mode="HTML",
        reply_markup=(
            edit_form_keyboard(
                message.from_user.id,
                has_description=bool(draft["payload"].get("description")),
            )
            if draft.get("draft_kind") == "edit"
            else draft_form_keyboard(
                message.from_user.id,
                has_description=bool(draft["payload"].get("description")),
            )
        ),
    )


@router.message(EventDraftInput.title)
async def event_title_input(message: Message, state: FSMContext) -> None:
    await _handle_text_field(message, state, "title")


@router.message(EventDraftInput.time)
async def event_time_input(message: Message, state: FSMContext) -> None:
    await _handle_text_field(message, state, "time")


@router.message(EventDraftInput.description)
async def event_description_input(message: Message, state: FSMContext) -> None:
    await _handle_text_field(message, state, "description")


@router.message(EventReviewInput.cancellation_reason)
async def event_cancellation_reason_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if (
        not message.from_user
        or message.from_user.id != int(data.get("admin_id", 0))
        or message.chat.id != int(data.get("panel_chat_id", 0))
        or not await can_manage_events(message.from_user.id, message.chat.id)
    ):
        return
    reason = (message.text or "").strip()
    deleted = await _delete_input(message)
    if not 3 <= len(reason) <= 300:
        await message.bot.edit_message_text(
            "Причина повинна містити від 3 до 300 символів.",
            chat_id=int(data["panel_chat_id"]),
            message_id=int(data["panel_message_id"]),
        )
        return
    result = await reviews_dao.request_or_cancel(
        int(data["event_id"]),
        actor_id=message.from_user.id,
        reason=reason,
        now=event_service.now_timestamp(),
    )
    if result.code != "confirm":
        await state.clear()
        await message.bot.edit_message_text(
            "Подію вже не можна скасувати.",
            chat_id=int(data["panel_chat_id"]),
            message_id=int(data["panel_message_id"]),
        )
        return
    await state.update_data(reason=reason)
    notice = "\n\n" + INPUT_DELETE_WARNING if not deleted else ""
    await message.bot.edit_message_text(
        f'⚠️ Скасувати подію «{result.title}»?\nПричина: {reason}{notice}',
        chat_id=int(data["panel_chat_id"]),
        message_id=int(data["panel_message_id"]),
        reply_markup=cancel_confirm_keyboard(message.from_user.id, int(data["event_id"])),
    )
