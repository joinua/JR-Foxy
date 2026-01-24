"""Сервіс попереджень із акцентом на аудит-лог та відтворюваність стану."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
import logging
import time

import aiosqlite

from app.core.db import DB_PATH


logger = logging.getLogger(__name__)

WARN_EXPIRY_DAYS = 30


@dataclass(frozen=True)
class WarningRecord:
    """Незмінний знімок запису про попередження.

    Поля `*_snapshot` потрібні лише для відображення в логах та історії.
    Джерелом істини для ідентичності користувача залишається `user_id`.
    """

    id: int
    user_id: int
    chat_id: int
    reason: str
    issued_at: int
    expires_at: int
    issued_by: int
    issued_by_level: int
    user_username_snapshot: str | None
    admin_username_snapshot: str | None
    is_revoked: bool
    revoked_at: int | None
    revoked_by: int | None


def _row_to_warning(row: tuple) -> WarningRecord:
    """Перетворює рядок БД у типізований `WarningRecord`."""

    return WarningRecord(
        id=int(row[0]),
        user_id=int(row[1]),
        chat_id=int(row[2]),
        reason=str(row[3]),
        issued_at=int(row[4]),
        expires_at=int(row[5]),
        issued_by=int(row[6]),
        issued_by_level=int(row[7]),
        user_username_snapshot=str(row[8]) if row[8] is not None else None,
        admin_username_snapshot=str(row[9]) if row[9] is not None else None,
        is_revoked=bool(row[10]),
        revoked_at=int(row[11]) if row[11] is not None else None,
        revoked_by=int(row[12]) if row[12] is not None else None,
    )


def build_mention(user_id: int, first_name: str | None, last_name: str | None) -> str:
    """Будує безпечний HTML-mention за `user_id`.

    Username не використовуємо як ідентифікатор, тому mention будуємо через
    `tg://user?id=...` і екрануємо відображуване ім'я.

    Параметри:
        user_id: Ідентифікатор користувача в Telegram.
        first_name: Ім'я користувача.
        last_name: Прізвище користувача.

    Повертає:
        HTML-посилання для mention.
    """

    name_parts = [part for part in (first_name or "", last_name or "") if part]
    display_name = " ".join(name_parts).strip() or str(user_id)
    return f'<a href="tg://user?id={user_id}">{html.escape(display_name)}</a>'


def _now_ts() -> int:
    """Повертає поточний час як UNIX-мітку в секундах."""

    return int(time.time())


def _expiry_ts(issued_at: int) -> int:
    """Розраховує час завершення дії попередження."""

    issued_dt = datetime.fromtimestamp(issued_at, tz=timezone.utc)
    return int((issued_dt + timedelta(days=WARN_EXPIRY_DAYS)).timestamp())


async def create_warning(
    user_id: int,
    chat_id: int,
    reason: str,
    issued_by: int,
    issued_by_level: int,
    user_username_snapshot: str | None,
    admin_username_snapshot: str | None,
) -> tuple[WarningRecord, int]:
    """Створює попередження та повертає його разом з активною кількістю.

    Вставку та підрахунок виконуємо в транзакції `BEGIN IMMEDIATE`, щоби
    уникнути перегонів між паралельними warn-командами.

    Параметри:
        user_id: Кому видано попередження.
        chat_id: Де видано попередження.
        reason: Причина попередження.
        issued_by: Хто видав попередження.
        issued_by_level: Рівень адміністратора на момент видачі.
        user_username_snapshot: Username користувача на момент видачі.
        admin_username_snapshot: Username адміністратора на момент видачі.

    Повертає:
        Кортеж (створений запис, кількість активних попереджень).
    """

    issued_at = _now_ts()
    expires_at = _expiry_ts(issued_at)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            INSERT INTO warnings (
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                user_username_snapshot,
                admin_username_snapshot,
                is_revoked,
                revoked_at,
                revoked_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
            """,
            (
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                user_username_snapshot,
                admin_username_snapshot,
            ),
        )
        cursor = await db.execute("SELECT last_insert_rowid()")
        warning_id = int((await cursor.fetchone())[0])
        active_count = await count_active_warnings(user_id, db=db, now=issued_at)
        await db.commit()

    logger.info(
        "warn issued",
        extra={
            "warning_id": warning_id,
            "user_id": user_id,
            "admin_id": issued_by,
            "chat_id": chat_id,
            "active_count": active_count,
        },
    )

    warning = WarningRecord(
        id=warning_id,
        user_id=user_id,
        chat_id=chat_id,
        reason=reason,
        issued_at=issued_at,
        expires_at=expires_at,
        issued_by=issued_by,
        issued_by_level=issued_by_level,
        user_username_snapshot=user_username_snapshot,
        admin_username_snapshot=admin_username_snapshot,
        is_revoked=False,
        revoked_at=None,
        revoked_by=None,
    )
    return warning, active_count


async def count_active_warnings(
    user_id: int,
    *,
    db: aiosqlite.Connection | None = None,
    now: int | None = None,
) -> int:
    """Рахує активні попередження як похідний стан.

    Активні попередження не зберігаються окремо, а обчислюються за умовою:
    `is_revoked = 0 AND expires_at > now`.

    Параметри:
        user_id: Ідентифікатор користувача.
        db: Опційне з'єднання для використання всередині транзакції.
        now: Опційний поточний час для узгоджених розрахунків.

    Повертає:
        Кількість активних попереджень.
    """

    now_ts = now if now is not None else _now_ts()
    if db is None:
        async with aiosqlite.connect(DB_PATH) as db_conn:
            return await count_active_warnings(user_id, db=db_conn, now=now_ts)

    cursor = await db.execute(
        """
        SELECT COUNT(*)
        FROM warnings
        WHERE user_id = ?
          AND is_revoked = 0
          AND expires_at > ?
        """,
        (user_id, now_ts),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def list_active_warnings(user_id: int) -> list[WarningRecord]:
    """Повертає активні попередження від найновішого до найстарішого.

    Параметри:
        user_id: Ідентифікатор користувача.

    Повертає:
        Список активних попереджень.
    """

    now_ts = _now_ts()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                user_username_snapshot,
                admin_username_snapshot,
                is_revoked,
                revoked_at,
                revoked_by
            FROM warnings
            WHERE user_id = ?
              AND is_revoked = 0
              AND expires_at > ?
            ORDER BY issued_at DESC, id DESC
            """,
            (user_id, now_ts),
        )
        rows = await cursor.fetchall()
        return [_row_to_warning(row) for row in rows]


async def get_latest_active_warning(user_id: int) -> WarningRecord | None:
    """Повертає останнє активне попередження або None, якщо його немає.

    Параметри:
        user_id: Ідентифікатор користувача.

    Повертає:
        Останнє активне попередження або None.
    """

    now_ts = _now_ts()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                user_username_snapshot,
                admin_username_snapshot,
                is_revoked,
                revoked_at,
                revoked_by
            FROM warnings
            WHERE user_id = ?
              AND is_revoked = 0
              AND expires_at > ?
            ORDER BY issued_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, now_ts),
        )
        row = await cursor.fetchone()
        return _row_to_warning(row) if row else None


async def list_warning_history(user_id: int) -> list[WarningRecord]:
    """Повертає повну історію попереджень без фільтрації за строком дії.

    Параметри:
        user_id: Ідентифікатор користувача.

    Повертає:
        Список усіх попереджень (активні, протерміновані, скасовані).
    """

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                user_username_snapshot,
                admin_username_snapshot,
                is_revoked,
                revoked_at,
                revoked_by
            FROM warnings
            WHERE user_id = ?
            ORDER BY issued_at DESC, id DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_warning(row) for row in rows]


async def revoke_latest_warning(
    user_id: int,
    revoked_by: int,
) -> tuple[WarningRecord | None, int]:
    """Скасовує останнє активне попередження та повертає його з підрахунком.

    Працюємо лише з активними попередженнями, бо історію не видаляємо.
    Вибірка обмежена умовою `expires_at > now AND is_revoked = 0`.

    Параметри:
        user_id: Кому скасовуємо попередження.
        revoked_by: Хто скасовує попередження.

    Повертає:
        Кортеж (скасований запис або None, кількість активних попереджень).
    """

    now_ts = _now_ts()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT
                id,
                user_id,
                chat_id,
                reason,
                issued_at,
                expires_at,
                issued_by,
                issued_by_level,
                user_username_snapshot,
                admin_username_snapshot,
                is_revoked,
                revoked_at,
                revoked_by
            FROM warnings
            WHERE user_id = ?
              AND is_revoked = 0
              AND expires_at > ?
            ORDER BY issued_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, now_ts),
        )
        row = await cursor.fetchone()
        if not row:
            active_count = await count_active_warnings(user_id, db=db, now=now_ts)
            await db.commit()
            return None, active_count

        warning = _row_to_warning(row)
        await db.execute(
            """
            UPDATE warnings
            SET is_revoked = 1,
                revoked_at = ?,
                revoked_by = ?
            WHERE id = ?
            """,
            (now_ts, revoked_by, warning.id),
        )
        active_count = await count_active_warnings(user_id, db=db, now=now_ts)
        await db.commit()

    logger.info(
        "warn revoked",
        extra={
            "warning_id": warning.id,
            "user_id": user_id,
            "admin_id": revoked_by,
            "chat_id": warning.chat_id,
            "active_count": active_count,
        },
    )

    revoked_warning = WarningRecord(
        id=warning.id,
        user_id=warning.user_id,
        chat_id=warning.chat_id,
        reason=warning.reason,
        issued_at=warning.issued_at,
        expires_at=warning.expires_at,
        issued_by=warning.issued_by,
        issued_by_level=warning.issued_by_level,
        user_username_snapshot=warning.user_username_snapshot,
        admin_username_snapshot=warning.admin_username_snapshot,
        is_revoked=True,
        revoked_at=now_ts,
        revoked_by=revoked_by,
    )
    return revoked_warning, active_count


def warning_status_label(warning: WarningRecord, now: int | None = None) -> str:
    """Повертає текстовий статус попередження для історії.

    Параметри:
        warning: Запис попередження.
        now: Опційний «поточний» час для узгоджених розрахунків.

    Повертає:
        Один із статусів: «активний», «протермінований», «скасований».
    """

    now_ts = now if now is not None else _now_ts()
    if warning.is_revoked:
        return "скасований"
    if warning.expires_at <= now_ts:
        return "протермінований"
    return "активний"
