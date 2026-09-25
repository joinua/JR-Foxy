"""Windows-only filesystem protection, locking, and notification helpers."""
import contextlib
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from backup_format import BackupError


def sid():
    result = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'], check=True,
                            capture_output=True, timeout=15, creationflags=0x08000000)
    values = re.findall(rb'\bS-1-\d+(?:-\d+)+\b', result.stdout)
    if len(values) != 1:
        raise BackupError('ACL', 'Cannot identify Windows user')
    return values[0].decode('ascii')


def require_acl_volume(path):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    volume = ctypes.create_unicode_buffer(32768)
    flags = wintypes.DWORD()
    if not kernel.GetVolumePathNameW(ctypes.c_wchar_p(str(path)), volume, len(volume)):
        raise BackupError('DISK', 'Cannot inspect the backup drive')
    if not kernel.GetVolumeInformationW(volume, None, 0, None, None, ctypes.byref(flags), None, 0):
        raise BackupError('DISK', 'Cannot inspect the backup filesystem')
    if not flags.value & 8:  # FILE_PERSISTENT_ACLS
        raise BackupError('ACL', 'The backup drive does not support access control lists')


def protect(path, current_sid=None):
    current_sid = current_sid or sid()
    path = Path(path)
    require_acl_volume(path)
    rights = '(OI)(CI)F' if path.is_dir() else 'F'
    subprocess.run(['icacls.exe', str(path), '/inheritance:r', '/grant:r',
                    '*'+current_sid+':'+rights, '*S-1-5-18:'+rights],
                   check=True, capture_output=True, timeout=20, creationflags=0x08000000)
    # Validate explicit entries too: inherited-only removal must not leave a
    # pre-existing grant for another account on a reused directory.
    literal = "'"+str(path).replace("'", "''")+"'"
    script = "$a=Get-Acl -LiteralPath "+literal+"; @($a.Access | ForEach-Object { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }) | ConvertTo-Json -Compress"
    encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
                            check=True, capture_output=True, timeout=20, creationflags=0x08000000)
    entries = json.loads(result.stdout.decode('utf-8-sig'))
    entries = [entries] if isinstance(entries,str) else (entries or [])
    if set(entries) != {current_sid, 'S-1-5-18'}:
        raise BackupError('ACL', 'The backup folder has explicit access for another account')


@contextlib.contextmanager
def single_instance(path):
    import msvcrt
    with path.open('a+b') as stream:
        stream.seek(0,2)
        if not stream.tell():
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise BackupError('BUSY','Another backup client is running') from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def powershell(script, *args, timeout=60):
    return subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                           '-File',str(script),*map(str,args)], check=True, capture_output=True,
                          timeout=timeout, creationflags=0x08000000)


def toast(tools, keys, title, body):
    fd, temporary = tempfile.mkstemp(prefix='.notification-', suffix='.json', dir=keys)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump({'title':title,'body':body},stream,ensure_ascii=False)
        powershell(tools/'Notify.ps1', temporary)
    finally:
        Path(temporary).unlink(missing_ok=True)
