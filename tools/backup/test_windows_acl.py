"""ACL regression tests. Real Windows checks touch temporary fixtures only.

Run on the target PC before resuming installation:
    python test_windows_acl.py
No server, backup, real private key, or antivirus setting is changed.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from backup_format import BackupError
import windows_support as win


class AclPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = self.root / 'backup_ssh_ed25519'
        self.key.write_bytes(b'non-secret test fixture')
        self.owner = 'S-1-5-21-101-202-303-1001'

    def test_extra_account_is_reported_with_actual_file(self):
        with patch.object(win, 'require_acl_volume'), patch.object(win.subprocess, 'run'), \
                patch.object(win, 'acl_principals', return_value=[self.owner, 'S-1-5-18', 'S-1-1-0']):
            with self.assertRaises(BackupError) as raised:
                win.protect(self.key, self.owner)
        self.assertIn(str(self.key), str(raised.exception))
        self.assertIn('S-1-1-0', str(raised.exception))

    def test_admin_removal_is_only_for_dedicated_key(self):
        with patch.object(win, 'require_acl_volume'), patch.object(win.subprocess, 'run') as run, \
                patch.object(win, 'acl_principals', return_value=[self.owner, 'S-1-5-18']):
            win.protect(self.key, self.owner, remove_administrators=True)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[-1], ['icacls.exe', str(self.key), '/remove:g', '*S-1-5-32-544'])
        self.assertTrue(all(command[0] == 'icacls.exe' for command in commands))
        other = self.root / 'backup.agekey'
        other.write_bytes(b'non-secret fixture')
        with self.assertRaises(BackupError):
            win.protect(other, self.owner, remove_administrators=True)
        with self.assertRaises(BackupError):
            win.protect(self.root, self.owner, remove_administrators=True)

    def test_admin_option_does_not_accept_other_accounts(self):
        with patch.object(win, 'require_acl_volume'), patch.object(win.subprocess, 'run'), \
                patch.object(win, 'acl_principals', return_value=[self.owner, 'S-1-5-18', 'S-1-1-0']):
            with self.assertRaises(BackupError):
                win.protect(self.key, self.owner, remove_administrators=True)


@unittest.skipUnless(os.name == 'nt', 'Requires real Windows ACL APIs')
class NativeWindowsAclTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='jr-foxy-acl-check-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.owner = win.sid()
        win.protect(self.root, self.owner)

    def icacls(self, path, *args):
        subprocess.run(['icacls.exe', str(path), *args], check=True,
                       capture_output=True, timeout=20, creationflags=0x08000000)

    def test_directory_and_unicode_file_permissions(self):
        fixture = self.root / 'перевірка з пробілами.txt'
        fixture.write_bytes(b'non-secret fixture')
        win.protect(fixture, self.owner)
        expected = {self.owner, 'S-1-5-18'}
        self.assertEqual(set(win.acl_principals(self.root)), expected)
        self.assertEqual(set(win.acl_principals(fixture)), expected)
        self.assertEqual(fixture.read_bytes(), b'non-secret fixture')

    def test_reported_openssh_permissions_can_be_repaired_idempotently(self):
        fixture = self.root / 'backup_ssh_ed25519'
        original = b'non-secret test fixture; this is not an SSH key'
        fixture.write_bytes(original)
        self.icacls(fixture, '/grant:r', '*S-1-5-32-544:F')
        self.assertIn('S-1-5-32-544', win.acl_principals(fixture))
        with self.assertRaises(BackupError):
            win.protect(fixture, self.owner)
        win.protect(fixture, self.owner, remove_administrators=True)
        win.protect(fixture, self.owner, remove_administrators=True)
        self.assertEqual(set(win.acl_principals(fixture)), {self.owner, 'S-1-5-18'})
        self.assertEqual(fixture.read_bytes(), original)

    def test_other_explicit_grants_are_not_silently_removed(self):
        fixture = self.root / 'backup_ssh_ed25519'
        fixture.write_bytes(b'non-secret fixture')
        self.icacls(fixture, '/grant:r', '*S-1-1-0:R')
        with self.assertRaises(BackupError):
            win.protect(fixture, self.owner, remove_administrators=True)
        self.assertIn('S-1-1-0', win.acl_principals(fixture))


if __name__ == '__main__':
    if os.name != 'nt':
        raise SystemExit('Run this check on Windows; real Windows ACL tests cannot run here.')
    unittest.main(verbosity=2)
