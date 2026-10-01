"""Archived or demoted profiles cannot keep privileges from legacy admin rows."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_OWNER_ID", "1")
os.environ.setdefault("INVITE_CHAT_ID", "-100")
os.environ.setdefault("ADMIN_LOG_CHAT_ID", "-200")
os.environ.setdefault("FAMILY_CHAT_ID", "-300")

from app.core import db


class RolePermissionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_role_and_active_status_override_old_admin_level(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with patch.object(db, "DB_PATH", path):
                await db.init_db()
                async with aiosqlite.connect(path) as connection:
                    await connection.execute(
                        "INSERT INTO admins (user_id, level, created_at, updated_at) VALUES (20, 4, 1, 1)"
                    )
                    await connection.execute(
                        "INSERT INTO profiles (user_id, role, status, created_at, updated_at) "
                        "VALUES (20, 'Офіцер', 'active', 'now', 'now')"
                    )
                    await connection.commit()
                self.assertEqual(await db.get_admin_level(20), 1)
                async with aiosqlite.connect(path) as connection:
                    await connection.execute("UPDATE profiles SET status='archived' WHERE user_id=20")
                    await connection.commit()
                self.assertEqual(await db.get_admin_level(20), 0)
                self.assertEqual(await db.get_admin_level(1), 4)
