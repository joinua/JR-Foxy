"""HTML renderers for event administration and public cards."""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Any

from app.core.dates import format_ua_datetime
from app.core.event_types import MAX_EVENT_PARTICIPANTS


EVENT_TYPE_LABELS = {
    "clan": "кланова",
    "interclan": "міжкланова",
    "public": "публічна",
}

MONTHS_UA = {
    1: "Січень",
    2: "Лютий",
    3: "Березень",
    4: "Квітень",
    5: "Травень",
    6: "Червень",
    7: "Липень",
    8: "Серпень",
    9: "Вересень",
    10: "Жовтень",
    11: "Листопад",
    12: "Грудень",
}


def render_admin_menu(counts: dict[str, int], notice: str | None = None) -> str:
    lines = [
        "📅 <b>Керування подіями</b>",
        "",
        f"Майбутніх подій: {int(counts.get('upcoming', 0))}",
        f"Очікують перевірки: {int(counts.get('review', 0))}",
    ]
    if counts.get("missing"):
        lines.append(f"Втрачено публікацій: {int(counts['missing'])}")
    if notice:
        lines.extend(("", escape(notice)))
    return "\n".join(lines)


def render_draft_form(
    payload: dict[str, Any],
    notice: str | None = None,
    *,
    edit: bool = False,
) -> str:
    event_type = EVENT_TYPE_LABELS.get(str(payload.get("event_type") or ""))
    raw_date = payload.get("date")
    try:
        displayed_date = date.fromisoformat(str(raw_date)).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        displayed_date = "⚠️ не вибрана"
    lines = [
        "📝 <b>Редагування події</b>" if edit else "📝 <b>Створення події</b>",
        "",
        f"Назва: {escape(str(payload.get('title') or '⚠️ не вказана'))}",
        f"Тип: {event_type or '⚠️ не вибрано'}",
        f"Дата: {displayed_date}",
        f"Час: {escape(str(payload.get('time') or '⚠️ не вказаний'))}",
        f"Опис: {escape(str(payload.get('description') or 'не додано'))}",
    ]
    if notice:
        lines.extend(("", escape(notice)))
    return "\n".join(lines)


def render_text_prompt(field: str, payload: dict[str, Any]) -> str:
    prompts = {
        "title": (
            "📝 <b>Назва події</b>\n\n"
            "Надішліть назву одним повідомленням — від 3 до 100 символів."
        ),
        "time": (
            "🕒 <b>Час події</b>\n\n"
            "Надішліть час за Києвом у форматі ГГ:ХХ, наприклад 21:00."
        ),
        "description": (
            "📄 <b>Опис події</b>\n\n"
            "Надішліть опис одним повідомленням — до 1000 символів."
        ),
    }
    current = payload.get(field)
    text = prompts[field]
    if current:
        text += f"\n\nПоточне значення: {escape(str(current))}"
    return text


def render_calendar_title(selected_month: date) -> str:
    return (
        "📅 <b>Дата події</b>\n\n"
        f"{MONTHS_UA[selected_month.month]} {selected_month.year}\n"
        "Оберіть день за київським часом."
    )


def render_public_card(event: dict[str, Any], *, preview: bool = False) -> str:
    title = escape(str(event["title"]))
    status = str(event.get("status") or "published")
    if status == "cancelled":
        return "\n".join(
            (
                f'❌ <b>Подію «{title}» скасовано</b>',
                "",
                f"Дата та час: {format_ua_datetime(int(event['starts_at_utc']))}",
                f"Причина: {escape(str(event.get('cancel_reason') or 'не вказана'))}",
            )
        )
    if status == "annulled":
        return "\n".join(
            (
                f'⛔ <b>Подію «{title}» анульовано</b>',
                "",
                f"Дата та час: {format_ua_datetime(int(event['starts_at_utc']))}",
                f"Причина: {escape(str(event.get('annul_reason') or 'не вказана'))}",
            )
        )
    event_type = EVENT_TYPE_LABELS.get(str(event["event_type"]), "невідомий")
    description = str(event.get("description") or "").strip()
    participants = event.get("participants") or []

    heading = "👁 <b>Попередній перегляд</b>\n\n" if preview else ""
    lines = [
        f'{heading}📅 <b>Подія «{title}»</b>',
        "",
        f"Тип: {event_type}",
        f"Дата та час: {format_ua_datetime(int(event['starts_at_utc']))}",
    ]
    if description:
        lines.append(f"Опис: {escape(description)}")
    if status == "completed":
        counts = event.get("result_counts") or {}
        lines.extend(
            (
                "Статус: ✅ перевірку завершено",
                "",
                "Підсумки:",
                f"✅ Присутні: {int(counts.get('present', 0))}",
                f"❌ Не з’явилися: {int(counts.get('no_show', 0))}",
                f"🕒 Пізня відмова: {int(counts.get('late_decline', 0))}",
                f"➖ Не враховано: {int(counts.get('excluded', 0))}",
            )
        )
        return "\n".join(lines)
    if status == "registration_closed":
        lines.append("Статус: реєстрацію завершено")
    elif status in {"started", "awaiting_review"}:
        lines.append("Статус: подія розпочалася")
    lines.extend(
        [
            "Крайній безпечний термін реєстрації до: "
            f"{format_ua_datetime(int(event['safe_until_utc']), include_weekday=False)}",
            "Реєстрація закривається: "
            f"{format_ua_datetime(int(event['registration_closes_at_utc']), include_weekday=False)}",
            "",
            f"Учасники: {len(participants)}/{MAX_EVENT_PARTICIPANTS}",
        ]
    )
    for index, participant in enumerate(participants, start=1):
        nickname = escape(str(participant.get("nickname") or participant["user_id"]))
        lines.append(
            f'{index}. <a href="tg://user?id={int(participant["user_id"])}">'
            f"{nickname}</a>"
        )
    lines.extend(
        [
            "",
            f"Думають: {int(event.get('thinking_count', 0))}",
            f"Відмовилися від участі: {int(event.get('declined_count', 0))}",
        ]
    )
    return "\n".join(lines)
