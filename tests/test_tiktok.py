"""TikTok regression tests use temporary SQLite and local/stubbed transports."""

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.core import db
from app.dao import tiktok as history
from app.handlers import admin_tiktok

# Match the application's initialization order for existing profile services.
from app.handlers import profile
from app.services import db_scheduler as scheduler
from app.services import tiktok_watcher as tt


def video(n, date=None, guid=None):
    return tt.Video(
        f"tiktok:{n}",
        guid or f"guid-{n}",
        f"https://www.tiktok.com/@test/video/{n}",
        date,
    )


def rss(items=""):
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>Test</title><link>https://example.test</link>"
        f"<description>Test feed</description>{items}</channel></rss>"
    ).encode()


def item(n, date, guid=None):
    return (
        f"<item><guid>{guid or n}</guid><link>https://www.tiktok.com/@test/video/{n}</link>"
        f"<pubDate>{date}</pubDate></item>"
    )


class FeedParsingTests(unittest.TestCase):
    def test_unsorted_feed_reads_every_entry_in_date_order(self):
        data = rss(
            item(2, "Tue, 02 Jun 2026 12:00:00 GMT")
            + item(1, "Mon, 01 Jun 2026 12:00:00 GMT")
            + item(3, "Wed, 03 Jun 2026 12:00:00 GMT")
        )
        self.assertEqual(
            [v.video_id for v in tt.parse_feed(data)],
            ["tiktok:1", "tiktok:2", "tiktok:3"],
        )

    def test_same_video_with_changed_guid_is_one_item(self):
        data = rss(
            item(1, "Mon, 01 Jun 2026 12:00:00 GMT", "a")
            + item(1, "Mon, 01 Jun 2026 12:00:00 GMT", "b")
        )
        self.assertEqual(len(tt.parse_feed(data)), 1)

    def test_html_broken_xml_empty_and_bad_links_are_distinct(self):
        for data in (b"<html>Login required</html>", b"<rss><channel><item>"):
            with self.subTest(data=data), self.assertRaises(tt.FeedError):
                tt.parse_feed(data)
        self.assertEqual(tt.parse_feed(rss()), [])
        with self.assertRaisesRegex(tt.FeedError, "invalid_video_url"):
            tt.parse_feed(rss("<item><link>javascript:bad</link></item>"))


class FeedHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.body, self.status, self.delay = rss(), 200, 0

        async def response(request):
            await asyncio.sleep(self.delay)
            return web.Response(body=self.body, status=self.status)

        app = web.Application()
        app.router.add_get("/feed", response)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/feed"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_real_http_empty_feed(self):
        self.assertEqual(await tt.fetch_videos(self.url), [])

    async def test_real_http_failure_and_response_limit(self):
        self.status = 403
        with self.assertRaisesRegex(tt.FeedError, "http_403"):
            await tt.fetch_videos(self.url)
        self.status, self.body = 200, b"x" * 1025
        with patch.object(tt, "MAX_FEED_BYTES", 1024):
            with self.assertRaisesRegex(tt.FeedError, "feed_too_large"):
                await tt.fetch_videos(self.url)

    async def test_timeout_cancels_request_and_reports_timeout(self):
        self.delay = 0.1
        real_timeout = tt.aiohttp.ClientTimeout
        with patch.object(
            tt.aiohttp, "ClientTimeout", return_value=real_timeout(total=0.01)
        ):
            with self.assertRaisesRegex(tt.FeedError, "timeout"):
                await tt.fetch_videos(self.url)


class TikTokTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.patch_db = patch.object(db, "DB_PATH", self.path)
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        self.addCleanup(self.temp.cleanup)
        await db.init_db()
        self.chat = tt.settings.MAIN_CHAT_ID
        await db.set_chat_setting(self.chat, tt.TIKTOK_NOTIFY_ENABLED_KEY, "1")
        await db.set_chat_setting(
            self.chat, tt.TIKTOK_RSS_URL_KEY, "https://example.test/feed"
        )
        await db.set_chat_setting(self.chat, tt.TIKTOK_THREAD_ID_KEY, "39053")
        await db.set_chat_setting(self.chat, tt.TIKTOK_LAST_VIDEO_ID_KEY, "guid-1")
        self.fetch = AsyncMock(return_value=[video(1, 100)])
        self.patch_fetch = patch.object(tt, "fetch_videos", self.fetch)
        self.patch_fetch.start()
        self.addCleanup(self.patch_fetch.stop)
        self.bot = SimpleNamespace(send_message=AsyncMock())

    def main_calls(self):
        return [
            c
            for c in self.bot.send_message.call_args_list
            if c.kwargs.get("chat_id") == self.chat
        ]

    async def state(self):
        import json

        return json.loads(
            await db.get_chat_setting(self.chat, tt.TIKTOK_CHECK_STATE_KEY)
        )

    async def test_migration_preserves_legacy_and_sends_only_newer_in_any_position(
        self,
    ):
        self.fetch.return_value = [
            video(1, 100),
            video(3, 300),
            video(2, 200),
            video(0, 50),
        ]
        result = await tt.check_and_notify(self.bot)
        self.assertEqual((result.status, result.posted), ("posted", 2))
        links = [
            c.kwargs["reply_markup"].inline_keyboard[0][0].url
            for c in self.main_calls()
        ]
        self.assertEqual(links, [video(2).url, video(3).url])
        self.assertTrue(
            all(c.kwargs["message_thread_id"] == 39053 for c in self.main_calls())
        )
        result = await tt.check_and_notify(self.bot)
        self.assertEqual(result.status, "no_updates")
        self.assertEqual(len(self.main_calls()), 2)
        self.assertEqual((await self.state())["latest_published_at"], 300)

    async def test_missing_checkpoint_baselines_without_historical_flood(self):
        self.fetch.return_value = [video(2, 200), video(3, 300)]
        self.assertEqual((await tt.force_check(self.bot)).status, "initialized")
        self.assertEqual(self.main_calls(), [])
        self.fetch.return_value.append(video(4, 400))
        self.assertEqual((await tt.check_and_notify(self.bot)).posted, 1)

    async def test_undated_checkpoint_does_not_flood(self):
        self.fetch.return_value = [video(1), video(2, 200)]
        self.assertEqual((await tt.check_and_notify(self.bot)).status, "initialized")
        self.assertEqual(self.main_calls(), [])

    async def test_history_survives_feed_reordering_guid_changes_and_db_reopen(self):
        self.fetch.return_value = [video(1, 100), video(2, 200)]
        await tt.check_and_notify(self.bot)
        await db.init_db()
        self.fetch.return_value = [video(2, 200, "changed-guid"), video(1, 100)]
        self.assertEqual((await tt.check_and_notify(self.bot)).status, "no_updates")
        self.assertEqual(len(self.main_calls()), 1)

    async def test_disabled_auto_does_not_fetch_manual_is_explicit_override(self):
        await db.set_chat_setting(self.chat, tt.TIKTOK_NOTIFY_ENABLED_KEY, "0")
        self.fetch.return_value = [video(1, 100), video(2, 200)]
        auto = await tt.check_and_notify(self.bot)
        self.assertEqual(auto.status, "disabled")
        self.fetch.assert_not_awaited()
        forced = await tt.force_check(self.bot)
        self.assertFalse(forced.enabled)
        self.assertEqual(forced.posted, 1)

    async def test_telegram_failure_remains_retryable_and_not_no_updates(self):
        self.fetch.return_value = [video(1, 100), video(2, 200)]
        self.bot.send_message.side_effect = RuntimeError(
            "private URL must not enter status"
        )
        result = await tt.check_and_notify(self.bot)
        self.assertEqual(result.status, "telegram_error")
        self.assertEqual(await history.seen_ids(self.chat, ["tiktok:2"]), set())
        self.assertNotIn("private URL", str(await self.state()))
        self.bot.send_message.side_effect = None
        self.assertEqual((await tt.check_and_notify(self.bot)).posted, 1)

    async def test_partial_batch_only_retries_unsent_items(self):
        self.fetch.return_value = [video(1, 100), video(2, 200), video(3, 300)]

        async def send(*args, **kwargs):
            markup = kwargs.get("reply_markup")
            if markup and markup.inline_keyboard[0][0].url == video(3).url:
                raise RuntimeError("send failed")

        self.bot.send_message.side_effect = send
        first = await tt.check_and_notify(self.bot)
        self.assertEqual((first.status, first.posted), ("telegram_error", 1))
        self.bot.send_message.side_effect = None
        self.assertEqual((await tt.check_and_notify(self.bot)).posted, 1)
        links = [
            c.kwargs["reply_markup"].inline_keyboard[0][0].url
            for c in self.main_calls()
        ]
        self.assertEqual(links.count(video(2).url), 1)

    async def test_audit_failure_does_not_retry_successful_main_message(self):
        self.fetch.return_value = [video(1, 100), video(2, 200)]

        async def send(*args, **kwargs):
            if args:
                raise RuntimeError("admin chat unavailable")

        self.bot.send_message.side_effect = send
        self.assertEqual((await tt.check_and_notify(self.bot)).posted, 1)
        self.assertEqual((await tt.check_and_notify(self.bot)).status, "no_updates")
        self.assertEqual(len(self.main_calls()), 1)

    async def test_history_failure_reports_already_confirmed_delivery(self):
        self.fetch.return_value = [video(1, 100), video(2, 200), video(3, 300)]
        with patch.object(
            history,
            "mark_seen",
            AsyncMock(side_effect=sqlite3.OperationalError("locked")),
        ):
            result = await tt.check_and_notify(self.bot)
        self.assertEqual(
            (result.status, result.posted, result.remaining), ("storage_error", 1, 1)
        )
        self.assertEqual(len(self.main_calls()), 1)

    async def test_feed_errors_are_visible_and_do_not_initialize_history(self):
        self.fetch.side_effect = tt.FeedError("http_503")
        result = await tt.check_and_notify(self.bot)
        self.assertEqual((result.status, result.reason), ("feed_error", "http_503"))
        self.assertEqual((await self.state())["status"], "feed_error")
        self.fetch.side_effect = None
        self.fetch.return_value = [video(1, 100), video(2, 200)]
        self.assertEqual((await tt.check_and_notify(self.bot)).posted, 1)

    async def test_concurrent_manual_and_scheduled_checks_do_not_double_post(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch(_):
            started.set()
            await release.wait()
            return [video(1, 100), video(2, 200)]

        self.fetch.side_effect = fetch
        first = asyncio.create_task(tt.check_and_notify(self.bot))
        await started.wait()
        second = await tt.force_check(self.bot)
        self.assertEqual(second.status, "busy")
        release.set()
        await first
        self.assertEqual(len(self.main_calls()), 1)

    async def test_cancel_releases_lock(self):
        started = asyncio.Event()

        async def fetch(_):
            started.set()
            await asyncio.Event().wait()

        self.fetch.side_effect = fetch
        task = asyncio.create_task(tt.check_and_notify(self.bot))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(tt._check_lock.locked())

    async def test_batch_limit_preserves_remaining_for_next_check(self):
        self.fetch.return_value = [video(i, i * 100) for i in range(1, 9)]
        first = await tt.check_and_notify(self.bot)
        self.assertEqual((first.posted, first.remaining), (5, 2))
        self.assertEqual((await tt.check_and_notify(self.bot)).posted, 2)

    async def test_startup_preserves_existing_due_time_and_concurrent_registration(
        self,
    ):
        task_id = await db.schedule_task("tiktok_check", 123)
        await asyncio.gather(
            scheduler.register_tiktok_task(), scheduler.register_tiktok_task()
        )
        with sqlite3.connect(self.path) as con:
            rows = con.execute(
                "SELECT id,run_at FROM scheduled_tasks WHERE status='pending'"
            ).fetchall()
        self.assertEqual(rows, [(task_id, 123)])

    async def test_exhausted_periodic_job_can_be_recreated(self):
        task_id = await db.schedule_task("tiktok_check", 123)
        for _ in range(4):
            await db.mark_task_running(task_id)
            await db.mark_task_failed(task_id, "failure")
        first, second = await asyncio.gather(
            db.ensure_periodic_task("tiktok_check", 456),
            db.ensure_periodic_task("tiktok_check", 456),
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, task_id)

    async def test_failed_check_schedules_retry_without_duplicate_pending_jobs(self):
        with patch.object(
            scheduler,
            "check_and_notify",
            AsyncMock(return_value=tt.CheckResult("feed_error")),
        ):
            await scheduler._handle_tiktok_check(self.bot)
            await scheduler._handle_tiktok_check(self.bot)
        with sqlite3.connect(self.path) as con:
            rows = con.execute(
                "SELECT run_at FROM scheduled_tasks WHERE status='pending'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(
            rows[0][0],
            int(time.time()) + min(300, scheduler.TIKTOK_CHECK_INTERVAL_SECONDS),
            delta=2,
        )

    async def test_scheduler_survives_outer_db_failure_and_remains_cancellable(self):
        fetch = AsyncMock(
            side_effect=[sqlite3.OperationalError("locked"), asyncio.CancelledError()]
        )
        with patch.object(scheduler, "fetch_due_tasks", fetch):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler.run_db_scheduler(self.bot, poll_interval=0)
        self.assertEqual(fetch.await_count, 2)

    async def test_status_is_read_only_and_requires_admin(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1, type="private"),
            from_user=SimpleNamespace(id=10),
            answer=AsyncMock(),
        )
        with patch.object(admin_tiktok, "get_admin_level", AsyncMock(return_value=3)):
            await admin_tiktok.tiktok_status_handler(message)
        self.fetch.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        self.assertIn("39053", message.answer.call_args.args[0])
        self.assertNotIn("https://example", message.answer.call_args.args[0])
        message.answer.reset_mock()
        with patch.object(admin_tiktok, "get_admin_level", AsyncMock(return_value=0)):
            await admin_tiktok.tiktok_status_handler(message)
        self.assertEqual(message.answer.call_args.args[0], "Недостатній рівень.")

    async def test_status_keeps_posted_and_remaining_counts(self):
        self.fetch.return_value = [video(i, i * 100) for i in range(1, 9)]
        await tt.check_and_notify(self.bot)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1, type="private"),
            from_user=SimpleNamespace(id=10),
            answer=AsyncMock(),
        )
        with patch.object(admin_tiktok, "get_admin_level", AsyncMock(return_value=3)):
            await admin_tiktok.tiktok_status_handler(message)
        text = message.answer.call_args.args[0]
        self.assertIn("Опубліковано відео: 5.", text)
        self.assertIn("Залишилося необроблених: 2.", text)

    async def test_manual_command_reports_disabled_override_and_feed_error(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1, type="private"),
            from_user=SimpleNamespace(id=10),
            bot=self.bot,
            answer=AsyncMock(),
        )
        result = tt.CheckResult("feed_error", reason="timeout", enabled=False)
        with patch.object(
            admin_tiktok, "get_admin_level", AsyncMock(return_value=4)
        ), patch.object(admin_tiktok, "force_check", AsyncMock(return_value=result)):
            await admin_tiktok.tiktok_check_handler(message)
        text = message.answer.call_args.args[0]
        self.assertIn("RSS не відповів", text)
        self.assertIn("Автопублікація вимкнена", text)
        self.assertNotIn("Нових відео немає", text)
