"""Notification failures must be diagnosable without logging custom messages."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from backup_format import BackupError
from windows_support import toast


class NotificationFailureTests(unittest.TestCase):
    def test_records_fixed_stage_and_removes_temporary_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            error = subprocess.CalledProcessError(
                1, ['powershell.exe'], output=b'NOTIFY_ERROR=SettingRead\r\n',
                stderr=b'private text which must not be logged')
            with patch('windows_support.powershell', side_effect=error):
                with self.assertRaises(BackupError) as caught:
                    toast(root, root, 'Private title', 'Private body')
            self.assertEqual(caught.exception.code, 'NOTIFICATION')
            self.assertEqual(str(caught.exception), 'SettingRead')
            self.assertEqual(list(root.iterdir()), [])

    def test_unrecognized_failure_does_not_copy_error_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            error = subprocess.CalledProcessError(
                1, ['powershell.exe'], output=b'NOTIFY_ERROR=unexpected message text',
                stderr=b'private text')
            with patch('windows_support.powershell', side_effect=error):
                with self.assertRaises(BackupError) as caught:
                    toast(root, root, 'Private title', 'Private body')
            self.assertEqual(str(caught.exception), 'PowerShellFailed')
            self.assertEqual(list(root.iterdir()), [])
