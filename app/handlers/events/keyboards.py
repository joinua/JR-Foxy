"""Inline keyboards for event administration and public cards."""

from __future__ import annotations

import calendar
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.event_render import MONTHS_UA


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def admin_menu_keyboard(admin_id: int, *, missing_count: int = 0) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    rows = [
            [_button("➕ Створити нову подію", prefix + "create")],
            [_button("✏️ Редагувати існуючу подію", prefix + "edit")],
            [_button("🚫 Скасувати подію", prefix + "cancel")],
            [_button("🔔 Надіслати нагадування", prefix + "remind")],
            [_button("📋 Незавершені перевірки", prefix + "reviews")],
    ]
    if missing_count:
        rows.append(
            [_button(f"⚠️ Втрачені публікації: {missing_count}", prefix + "missing")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def existing_draft_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("▶️ Продовжити", prefix + "continue")],
            [_button("🗑 Видалити й створити нову", prefix + "delete_ask")],
            [_button("↩️ Назад", prefix + "menu")],
        ]
    )


def delete_draft_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("✅ Так, видалити", prefix + "delete_yes")],
            [_button("↩️ Ні, повернутися", prefix + "continue")],
        ]
    )


def draft_form_keyboard(admin_id: int, *, has_description: bool) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    rows = [
        [
            _button("📝 Назва", prefix + "field_title"),
            _button("🏷 Тип", prefix + "field_type"),
        ],
        [
            _button("📅 Дата", prefix + "field_date"),
            _button("🕒 Час", prefix + "field_time"),
        ],
        [_button("📄 Опис", prefix + "field_description")],
    ]
    if has_description:
        rows.append([_button("🧹 Прибрати опис", prefix + "clear_description")])
    rows.extend(
        [
            [_button("👁 Попередній перегляд", prefix + "preview")],
            [_button("✅ До публікації", prefix + "prepare_publish")],
            [
                _button("🗑 Видалити чернетку", prefix + "delete_ask"),
                _button("↩️ Назад", prefix + "menu"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_form_keyboard(admin_id: int, *, has_description: bool) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    rows = [
        [
            _button("📝 Назва", prefix + "field_title"),
            _button("🏷 Тип", prefix + "field_type"),
        ],
        [
            _button("📅 Дата", prefix + "field_date"),
            _button("🕒 Час", prefix + "field_time"),
        ],
        [_button("📄 Опис", prefix + "field_description")],
    ]
    if has_description:
        rows.append([_button("🧹 Прибрати опис", prefix + "clear_description")])
    rows.extend(
        [
            [_button("👁 Попередній перегляд", prefix + "preview")],
            [_button("💾 Зберегти всі зміни", prefix + "save_changes")],
            [
                _button("🗑 Скасувати редагування", prefix + "delete_ask"),
                _button("↩️ Назад", prefix + "menu"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_preview_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("💾 Зберегти всі зміни", prefix + "save_changes")],
            [_button("↩️ До редагування", prefix + "continue")],
        ]
    )


def back_to_form_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("↩️ До форми", f"ev:a:{admin_id}:continue")],
        ]
    )


def publish_confirmation_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("📢 Опублікувати в JokerRecon", prefix + "publish")],
            [_button("↩️ До редагування", prefix + "continue")],
        ]
    )


def type_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:type_"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Кланова", prefix + "clan")],
            [_button("Міжкланова", prefix + "interclan")],
            [_button("Публічна", prefix + "public")],
            [_button("↩️ До форми", f"ev:a:{admin_id}:continue")],
        ]
    )


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    return divmod(index, 12)[0], divmod(index, 12)[1] + 1


def calendar_keyboard(admin_id: int, year: int, month: int) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    rows: list[list[InlineKeyboardButton]] = [
        [_button(f"{MONTHS_UA[month]} {year}", prefix + "noop")],
        [_button(day, prefix + "noop") for day in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")],
    ]
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        rows.append(
            [
                _button(
                    str(day) if day else " ",
                    prefix + (f"date_{year:04d}-{month:02d}-{day:02d}" if day else "noop"),
                )
                for day in week
            ]
        )
    previous_year, previous_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)
    rows.append(
        [
            _button("‹", prefix + f"cal_{previous_year}_{previous_month}"),
            _button("↩️ До форми", prefix + "continue"),
            _button("›", prefix + f"cal_{next_year}_{next_month}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def public_event_keyboard(event_id: int) -> InlineKeyboardMarkup:
    prefix = f"ev:r:{event_id}:"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("✅ Я — учасник", prefix + "going"),
                _button("❌ Відмовляюся", prefix + "declined"),
            ],
            [_button("🤔 Думаю", prefix + "thinking")],
        ]
    )


def missing_events_keyboard(admin_id: int, events: list[dict]) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    rows = [
        [
            _button(
                f"⚠️ {str(event['title'])[:40]}",
                prefix + f"recover_{int(event['id'])}",
            )
        ]
        for event in events
    ]
    rows.append([_button("↩️ Назад", prefix + "menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recover_publication_keyboard(admin_id: int, event_id: int) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("📢 Опублікувати повторно", prefix + f"republish_{event_id}")],
            [_button("🚫 Скасувати подію", prefix + f"cancel_missing_{event_id}")],
            [_button("↩️ Назад", prefix + "missing")],
        ]
    )


def editable_events_keyboard(admin_id: int, events: list[dict]) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    rows = [
        [_button(f"✏️ {str(event['title'])[:40]}", prefix + f"edit_event_{event['id']}")]
        for event in events
    ]
    rows.append([_button("↩️ Назад", prefix + "menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_events_keyboard(admin_id: int, events: list[dict]) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    rows = [
        [_button(f"🔔 {str(event['title'])[:40]}", prefix + f"remind_{event['id']}")]
        for event in events
    ]
    rows.append([_button("↩️ Назад", prefix + "menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_audience_keyboard(admin_id: int, event_id: int) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:remind_send_{event_id}_"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("✅ Учасникам", prefix + "going")],
            [_button("🤔 Тим, хто думає", prefix + "thinking")],
            [_button("👥 Обом групам", prefix + "both")],
            [_button("↩️ Назад", f"ev:a:{admin_id}:remind")],
        ]
    )


def review_events_keyboard(
    events: list[dict],
    *,
    back_admin_id: int,
    page: int = 0,
    pages: int = 1,
) -> InlineKeyboardMarkup:
    rows = []
    for event in events:
        icon = "📋" if event["status"] == "awaiting_review" else "✅"
        rows.append(
            [_button(f"{icon} {str(event['title'])[:40]}", f"ev:v:{event['id']}:open:0")]
        )
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_button("‹", f"ev:a:{back_admin_id}:reviews_page_{page - 1}"))
        nav.append(_button(f"{page + 1}/{pages}", f"ev:a:{back_admin_id}:noop"))
        if page + 1 < pages:
            nav.append(_button("›", f"ev:a:{back_admin_id}:reviews_page_{page + 1}"))
        rows.append(nav)
    rows.append([_button("↩️ Назад", f"ev:a:{back_admin_id}:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def review_keyboard(review: dict) -> InlineKeyboardMarkup:
    event_id = int(review["id"])
    page = int(review["page"])
    prefix = f"ev:v:{event_id}:"
    rows: list[list[InlineKeyboardButton]] = []
    symbols = {"present": "✅", "no_show": "❌", "late_decline": "🕒", "excluded": "➖", None: "▫️"}
    for offset, player in enumerate(review["players"], start=page * 10 + 1):
        user_id = int(player["user_id"])
        current = symbols.get(player.get("result"), "▫️")
        if review["status"] == "awaiting_review":
            rows.append(
                [
                    _button(f"{offset} ✅", prefix + f"set:{user_id}:present:{page}"),
                    _button(f"{offset} ❌", prefix + f"set:{user_id}:no_show:{page}"),
                    _button(f"{offset} 🕒", prefix + f"set:{user_id}:late_decline:{page}"),
                    _button(f"{offset} ➖", prefix + f"exclude:{user_id}:{page}"),
                ]
            )
        else:
            rows.append(
                [_button(f"{offset} {current} — змінити", prefix + f"correct:{user_id}:{page}")]
            )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_button("‹", prefix + f"open:{page - 1}"))
    nav.append(_button(f"{page + 1}/{review['pages']}", prefix + "noop"))
    if page + 1 < int(review["pages"]):
        nav.append(_button("›", prefix + f"open:{page + 1}"))
    rows.append(nav)
    if review["status"] == "awaiting_review":
        rows.append([_button("✅ Завершити перевірку", prefix + f"finalize:{page}")])
    elif review["status"] == "completed":
        rows.append([_button("⛔ Анулювати подію", prefix + "annul")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def correction_result_keyboard(event_id: int, user_id: int, page: int) -> InlineKeyboardMarkup:
    prefix = f"ev:v:{event_id}:correct_result:{user_id}:"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("✅ Присутній", prefix + f"present:{page}"),
                _button("❌ Неявка", prefix + f"no_show:{page}"),
            ],
            [
                _button("🕒 Пізня відмова", prefix + f"late_decline:{page}"),
                _button("➖ Не враховувати", prefix + f"excluded:{page}"),
            ],
            [_button("↩️ Назад", f"ev:v:{event_id}:open:{page}")],
        ]
    )


def cancellable_events_keyboard(admin_id: int, events: list[dict]) -> InlineKeyboardMarkup:
    prefix = f"ev:a:{admin_id}:"
    rows = [
        [_button(f"🚫 {str(event['title'])[:40]}", prefix + f"cancel_event_{event['id']}")]
        for event in events
    ]
    rows.append([_button("↩️ Назад", prefix + "menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_confirm_keyboard(admin_id: int, event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("⚠️ Підтвердити скасування", f"ev:a:{admin_id}:cancel_confirm_{event_id}")],
            [_button("↩️ Назад", f"ev:a:{admin_id}:cancel")],
        ]
    )
