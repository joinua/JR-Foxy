import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.core import db
from app.core.event_types import EVENT_DRAFT_TTL_SECONDS
from app.dao import events as events_dao
from app.handlers.events import admin as event_admin
from app.handlers.events.keyboards import calendar_keyboard
from app.handlers.events.keyboards import public_event_keyboard
from app.services import event_service
from app.services.event_render import render_public_card


class EventCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "events.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        await db.init_db()
        self.now = int(
            datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc).timestamp()
        )

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def _complete_draft(self, admin_id: int = 1) -> dict:
        await event_service.create_draft(
            admin_id,
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.now,
        )
        values = {
            "title": "  Тренування   КБ  ",
            "event_type": "clan",
            "date": "2026-09-06",
            "time": "21:00",
            "description": "  Командна взаємодія.  ",
        }
        draft = None
        for field, value in values.items():
            draft = await event_service.save_draft_field(
                admin_id,
                field,
                value,
                menu_chat_id=-200,
                menu_message_id=10,
                now=self.now,
            )
        assert draft is not None
        return draft

    async def test_draft_persists_normalized_fields_and_sliding_expiry(self):
        draft = await self._complete_draft()

        self.assertEqual(draft["payload"]["title"], "Тренування КБ")
        self.assertEqual(
            draft["payload"]["description"],
            "Командна взаємодія.",
        )
        self.assertEqual(draft["expires_at"], self.now + EVENT_DRAFT_TTL_SECONDS)

        loaded, expired = await event_service.load_draft(1, now=self.now + 1)
        self.assertFalse(expired)
        self.assertEqual(loaded["payload"], draft["payload"])

    async def test_expired_draft_is_deleted(self):
        await event_service.create_draft(
            1,
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.now,
        )

        draft, expired = await event_service.load_draft(
            1,
            now=self.now + EVENT_DRAFT_TTL_SECONDS,
        )

        self.assertIsNone(draft)
        self.assertTrue(expired)

    async def test_periodic_cleanup_keeps_unknown_publication_but_removes_plain_draft(self):
        await event_service.create_draft(
            1,
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.now,
        )
        unknown_draft = await self._complete_draft(2)
        validated = await event_service.validate_draft(unknown_draft, now=self.now)
        reservation = await events_dao.reserve_draft_publication(
            2,
            title=validated.title,
            event_type=validated.event_type,
            description=validated.description,
            starts_at_utc=validated.starts_at_utc,
            safe_until_utc=validated.safe_until_utc,
            registration_closes_at_utc=validated.registration_closes_at_utc,
            main_chat_id=-100,
            now=self.now,
        )
        await events_dao.mark_publication_unknown(
            reservation.event_id,
            actor_id=2,
            error="network",
            now=self.now + 1,
        )

        deleted = await events_dao.cleanup_expired_drafts(
            now=self.now + EVENT_DRAFT_TTL_SECONDS,
        )

        self.assertEqual(deleted, 1)
        plain, _ = await event_service.load_draft(
            1,
            now=self.now + EVENT_DRAFT_TTL_SECONDS,
        )
        unknown, unknown_expired = await event_service.load_draft(
            2,
            now=self.now + EVENT_DRAFT_TTL_SECONDS + 1,
        )
        self.assertIsNone(plain)
        self.assertIsNotNone(unknown)
        self.assertFalse(unknown_expired)
        card = await events_dao.get_event_card(reservation.event_id)
        self.assertEqual(card["status"], "publication_unknown")

    async def test_validation_builds_exact_event_boundaries(self):
        draft = await self._complete_draft()

        validated = await event_service.validate_draft(draft, now=self.now)

        self.assertEqual(validated.title, "Тренування КБ")
        self.assertEqual(
            validated.starts_at_utc,
            int(datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc).timestamp()),
        )
        self.assertEqual(validated.safe_until_utc, validated.starts_at_utc - 7200)
        self.assertEqual(
            validated.registration_closes_at_utc,
            validated.starts_at_utc - 3600,
        )

    async def test_validation_rejects_missing_fields_and_short_lead_time(self):
        draft = await event_service.create_draft(
            1,
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.now,
        )
        with self.assertRaisesRegex(
            event_service.EventValidationError,
            "Заповніть назву",
        ):
            await event_service.validate_draft(draft, now=self.now)

        await event_service.save_draft_field(
            1,
            "title",
            "Швидка подія",
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.now,
        )
        await event_service.save_draft_field(
            1,
            "event_type",
            "clan",
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.now,
        )
        await event_service.save_draft_field(
            1,
            "date",
            "2026-09-05",
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.now,
        )
        draft = await event_service.save_draft_field(
            1,
            "time",
            "10:00",
            menu_chat_id=-200,
            menu_message_id=10,
            now=self.now,
        )

        with self.assertRaisesRegex(
            event_service.EventValidationError,
            "щонайменше за 24 години",
        ):
            await event_service.validate_draft(draft, now=self.now)

    async def test_concurrent_drafts_cannot_reserve_same_start(self):
        first = await self._complete_draft(1)
        second = await self._complete_draft(2)
        validated = await event_service.validate_draft(first, now=self.now)

        async def reserve(admin_id: int):
            try:
                return await events_dao.reserve_draft_publication(
                    admin_id,
                    title=validated.title,
                    event_type=validated.event_type,
                    description=validated.description,
                    starts_at_utc=validated.starts_at_utc,
                    safe_until_utc=validated.safe_until_utc,
                    registration_closes_at_utc=validated.registration_closes_at_utc,
                    main_chat_id=-100,
                    now=self.now,
                )
            except events_dao.EventConflictError as exc:
                return exc

        results = await asyncio.gather(reserve(1), reserve(2))

        self.assertEqual(
            sum(isinstance(item, events_dao.EventConflictError) for item in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(item, events_dao.PublicationReservation) for item in results),
            1,
        )

    async def test_repeated_reservation_does_not_request_second_send(self):
        draft = await self._complete_draft()
        validated = await event_service.validate_draft(draft, now=self.now)
        kwargs = dict(
            title=validated.title,
            event_type=validated.event_type,
            description=validated.description,
            starts_at_utc=validated.starts_at_utc,
            safe_until_utc=validated.safe_until_utc,
            registration_closes_at_utc=validated.registration_closes_at_utc,
            main_chat_id=-100,
            now=self.now,
        )

        first = await events_dao.reserve_draft_publication(1, **kwargs)
        second = await events_dao.reserve_draft_publication(1, **kwargs)

        self.assertTrue(first.should_send)
        self.assertFalse(second.should_send)
        self.assertEqual(first.event_id, second.event_id)

    async def test_successful_publication_commits_message_and_removes_draft(self):
        await self._complete_draft()
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=321))
        )

        result = await event_service.publish_draft(
            bot,
            1,
            reply_markup_factory=public_event_keyboard,
            now=self.now,
        )

        self.assertEqual(result.status, "published")
        bot.send_message.assert_awaited_once()
        draft, _ = await event_service.load_draft(1, now=self.now)
        self.assertIsNone(draft)
        card = await events_dao.get_event_card(result.event_id)
        self.assertEqual(card["status"], "published")
        self.assertEqual(card["publication"]["message_id"], 321)

    async def test_unknown_delivery_is_never_automatically_retried(self):
        await self._complete_draft()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("network")))

        first = await event_service.publish_draft(
            bot,
            1,
            reply_markup_factory=public_event_keyboard,
            now=self.now,
        )
        second = await event_service.publish_draft(
            bot,
            1,
            reply_markup_factory=public_event_keyboard,
            now=self.now + 1,
        )

        self.assertEqual(first.status, "publication_unknown")
        self.assertEqual(second.status, "publication_unknown")
        bot.send_message.assert_awaited_once()

    async def test_definite_send_failure_keeps_draft_and_allows_safe_retry(self):
        await self._complete_draft()
        error = TelegramBadRequest(
            SendMessage(chat_id=-100, text="test"),
            "Bad Request: chat not found",
        )
        failing_bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))

        with self.assertRaises(TelegramBadRequest):
            await event_service.publish_draft(
                failing_bot,
                1,
                reply_markup_factory=public_event_keyboard,
                now=self.now,
            )

        draft, _ = await event_service.load_draft(1, now=self.now + 1)
        self.assertEqual(draft["target_event_status"], "draft")
        retry_bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=777))
        )
        result = await event_service.publish_draft(
            retry_bot,
            1,
            reply_markup_factory=public_event_keyboard,
            now=self.now + 1,
        )

        self.assertEqual(result.status, "published")
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute("SELECT COUNT(*) FROM events")
            self.assertEqual((await cursor.fetchone())[0], 1)

    async def test_startup_reconciliation_marks_interrupted_send_unknown(self):
        draft = await self._complete_draft()
        validated = await event_service.validate_draft(draft, now=self.now)
        reservation = await events_dao.reserve_draft_publication(
            1,
            title=validated.title,
            event_type=validated.event_type,
            description=validated.description,
            starts_at_utc=validated.starts_at_utc,
            safe_until_utc=validated.safe_until_utc,
            registration_closes_at_utc=validated.registration_closes_at_utc,
            main_chat_id=-100,
            now=self.now,
        )

        self.assertEqual(await event_service.reconcile_startup(now=self.now + 10), 1)
        card = await events_dao.get_event_card(reservation.event_id)
        self.assertEqual(card["status"], "publication_unknown")

    async def test_deleted_card_is_detected_on_admin_refresh(self):
        await self._complete_draft()
        publishing_bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=321))
        )
        result = await event_service.publish_draft(
            publishing_bot,
            1,
            reply_markup_factory=public_event_keyboard,
            now=self.now,
        )
        error = TelegramBadRequest(
            EditMessageText(chat_id=-100, message_id=321, text="test"),
            "Bad Request: message to edit not found",
        )
        refresh_bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=error))

        missing = await event_service.refresh_public_cards(
            refresh_bot,
            reply_markup_factory=public_event_keyboard,
        )

        self.assertEqual(missing, 1)
        card = await events_dao.get_event_card(result.event_id)
        self.assertEqual(card["publication_missing"], 1)
        self.assertIsNotNone(card["publication"]["message_id"])

    async def test_republication_replaces_only_current_publication(self):
        await self._complete_draft()
        first_bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=321))
        )
        published = await event_service.publish_draft(
            first_bot,
            1,
            reply_markup_factory=public_event_keyboard,
            now=self.now,
        )
        await events_dao.mark_publication_missing(
            published.event_id,
            now=self.now + 1,
        )
        second_bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=654))
        )

        result = await event_service.republish_event(
            second_bot,
            published.event_id,
            1,
            reply_markup_factory=public_event_keyboard,
        )

        self.assertEqual(result.status, "published")
        card = await events_dao.get_event_card(published.event_id)
        self.assertEqual(card["publication_missing"], 0)
        self.assertEqual(card["publication"]["message_id"], 654)
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute(
                """
                SELECT message_id, is_current
                FROM event_publications
                WHERE event_id=? ORDER BY id
                """,
                (published.event_id,),
            )
            self.assertEqual(await cursor.fetchall(), [(321, 0), (654, 1)])

    async def test_republication_reservation_prevents_duplicate_send(self):
        await self._complete_draft()
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=321))
        )
        published = await event_service.publish_draft(
            bot,
            1,
            reply_markup_factory=public_event_keyboard,
            now=self.now,
        )
        await events_dao.mark_publication_missing(
            published.event_id,
            now=self.now + 1,
        )

        first = await events_dao.reserve_republication(
            published.event_id,
            1,
            now=self.now + 2,
        )
        second = await events_dao.reserve_republication(
            published.event_id,
            1,
            now=self.now + 3,
        )

        self.assertTrue(first.should_send)
        self.assertFalse(second.should_send)
        self.assertEqual(first.publication_id, second.publication_id)

    async def test_explicit_start_conflict_has_existing_title(self):
        draft = await self._complete_draft(1)
        validated = await event_service.validate_draft(draft, now=self.now)
        await events_dao.reserve_draft_publication(
            1,
            title=validated.title,
            event_type=validated.event_type,
            description=validated.description,
            starts_at_utc=validated.starts_at_utc,
            safe_until_utc=validated.safe_until_utc,
            registration_closes_at_utc=validated.registration_closes_at_utc,
            main_chat_id=-100,
            now=self.now,
        )
        second = await self._complete_draft(2)

        with self.assertRaisesRegex(
            event_service.EventValidationError,
            "Тренування КБ",
        ):
            await event_service.validate_draft(second, now=self.now)

    async def test_database_unique_index_remains_last_conflict_guard(self):
        starts_at = 2_000_000_000
        async with aiosqlite.connect(self.db_path) as connection:
            values = (
                "Подія",
                starts_at,
                starts_at - 7200,
                starts_at - 3600,
            )
            await connection.execute(
                """
                INSERT INTO events (
                    title, event_type, starts_at_utc, safe_until_utc,
                    registration_closes_at_utc, status,
                    created_by, created_at, updated_at
                ) VALUES (?, 'clan', ?, ?, ?, 'published', 1, 1, 1)
                """,
                values,
            )
            await connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                await connection.execute(
                    """
                    INSERT INTO events (
                        title, event_type, starts_at_utc, safe_until_utc,
                        registration_closes_at_utc, status,
                        created_by, created_at, updated_at
                    ) VALUES (?, 'clan', ?, ?, ?, 'publishing', 2, 1, 1)
                    """,
                    values,
                )

    def test_public_renderer_escapes_user_content(self):
        card = {
            "title": "<Подія>",
            "event_type": "public",
            "description": "<b>не HTML</b>",
            "starts_at_utc": 1_789_071_600,
            "safe_until_utc": 1_789_064_400,
            "registration_closes_at_utc": 1_789_068_000,
            "participants": [
                {"user_id": 20, "nickname": "JRঐ<Name>"},
            ],
            "thinking_count": 2,
            "declined_count": 1,
        }

        rendered = render_public_card(card)

        self.assertIn("&lt;Подія&gt;", rendered)
        self.assertIn("&lt;b&gt;не HTML&lt;/b&gt;", rendered)
        self.assertIn("JRঐ&lt;Name&gt;", rendered)
        self.assertNotIn("<Подія>", rendered)


class EventCreationHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_command_is_rejected_outside_authorized_context(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=-999),
            answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())
        with patch.object(event_admin, "can_manage_events", AsyncMock(return_value=False)):
            await event_admin.event_menu_handler(message, state)

        message.answer.assert_awaited_once_with(event_admin.ACCESS_DENIED)
        state.clear.assert_not_awaited()

    async def test_foreign_administrator_cannot_use_panel(self):
        callback = SimpleNamespace(
            data="ev:a:2:create",
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=-200)),
            answer=AsyncMock(),
        )
        state = SimpleNamespace()

        await event_admin.event_admin_callback(callback, state)

        callback.answer.assert_awaited_once_with(
            event_admin.MENU_LOCKED,
            show_alert=True,
        )

    async def test_text_input_is_deleted_and_original_panel_is_edited(self):
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=-200),
            text="  Нова   подія  ",
            delete=AsyncMock(),
            bot=bot,
        )
        state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={
                    "admin_id": 1,
                    "panel_chat_id": -200,
                    "panel_message_id": 55,
                }
            ),
            clear=AsyncMock(),
        )
        saved = {
            "payload": {"title": "Нова подія"},
        }
        with (
            patch.object(event_admin, "can_manage_events", AsyncMock(return_value=True)),
            patch.object(
                event_admin.event_service,
                "save_draft_field",
                AsyncMock(return_value=saved),
            ) as save_field,
        ):
            await event_admin.event_title_input(message, state)

        message.delete.assert_awaited_once()
        save_field.assert_awaited_once()
        state.clear.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once()
        kwargs = bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -200)
        self.assertEqual(kwargs["message_id"], 55)

    def test_calendar_callback_data_fits_telegram_limit(self):
        keyboard = calendar_keyboard(9_999_999_999, 2030, 12)
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertTrue(callbacks)
        self.assertLessEqual(max(len(value.encode("utf-8")) for value in callbacks), 64)
