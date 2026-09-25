"""Windows-only filesystem protection, locking, and notification helpers."""
import contextlib
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


def acl_principals(path):
    """Read ordinary allow ACEs using documented Windows APIs, without a shell.

    Reject null DACLs, deny/object/callback ACEs, and invalid SIDs rather than
    incorrectly treating an unfamiliar access policy as a private directory.
    Buffers returned by Windows are released using LocalFree.
    """
    pointer = ctypes.c_void_p
    pointer_ref = ctypes.POINTER(pointer)
    dword = ctypes.c_uint32
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, dword,
                                           pointer_ref, pointer_ref, pointer_ref,
                                           pointer_ref, pointer_ref]
    advapi.GetNamedSecurityInfoW.restype = dword
    advapi.GetAclInformation.argtypes = [pointer, pointer, dword, ctypes.c_int]
    advapi.GetAclInformation.restype = ctypes.c_int
    advapi.GetAce.argtypes = [pointer, dword, pointer_ref]
    advapi.GetAce.restype = ctypes.c_int
    advapi.IsValidSid.argtypes = [pointer]
    advapi.IsValidSid.restype = ctypes.c_int
    advapi.GetLengthSid.argtypes = [pointer]
    advapi.GetLengthSid.restype = dword
    advapi.ConvertSidToStringSidW.argtypes = [pointer, pointer_ref]
    advapi.ConvertSidToStringSidW.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer

    class AclSize(ctypes.Structure):
        _fields_ = [('count', dword), ('used', dword), ('free', dword)]

    class AceHeader(ctypes.Structure):
        _fields_ = [('type', ctypes.c_ubyte), ('flags', ctypes.c_ubyte),
                    ('size', ctypes.c_uint16)]

    def failed(message):
        return BackupError('ACL', f'{message}: {path}')

    descriptor, dacl = pointer(), pointer()
    result = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None,
                                         ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        raise failed(f'Cannot read access permissions (Windows error {result})')
    try:
        if not dacl.value:
            raise failed('Unrestricted or missing access permissions')
        size = AclSize()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise failed('Cannot inspect access entries')
        principals = []
        for index in range(size.count):
            entry = pointer()
            if not advapi.GetAce(dacl, index, ctypes.byref(entry)) or not entry.value:
                raise failed('Cannot read an access entry')
            header = AceHeader.from_address(entry.value)
            if header.type != 0 or header.size < 16:  # ACCESS_ALLOWED_ACE_TYPE
                raise failed('Unsupported or deny access entry; manual review required')
            principal = pointer(entry.value + 8)  # ACE_HEADER + ACCESS_MASK
            if (not advapi.IsValidSid(principal) or
                    advapi.GetLengthSid(principal) > header.size - 8):
                raise failed('Invalid account identifier in access permissions')
            sid_string = pointer()
            if not advapi.ConvertSidToStringSidW(principal, ctypes.byref(sid_string)):
                raise failed('Cannot read an account identifier')
            try:
                principals.append(ctypes.wstring_at(sid_string.value))
            finally:
                kernel.LocalFree(sid_string)
        return principals
    finally:
        kernel.LocalFree(descriptor)


def protect(path, current_sid=None, *, remove_administrators=False):
    current_sid = current_sid or sid()
    path = Path(path)
    # OpenSSH may create this key with an explicit built-in Administrators grant.
    # Only the dedicated backup key may have that known grant removed. Other
    # files/directories and all unexpected accounts still require manual review.
    if remove_administrators and (path.name != 'backup_ssh_ed25519' or not path.is_file()):
        raise BackupError('ACL', 'Administrators removal is restricted to the backup SSH key')
    require_acl_volume(path)
    rights = '(OI)(CI)F' if path.is_dir() else 'F'
    subprocess.run(['icacls.exe', str(path), '/inheritance:r', '/grant:r',
                    '*'+current_sid+':'+rights, '*S-1-5-18:'+rights],
                   check=True, capture_output=True, timeout=20, creationflags=0x08000000)
    if remove_administrators:
        subprocess.run(['icacls.exe', str(path), '/remove:g', '*S-1-5-32-544'],
                       check=True, capture_output=True, timeout=20, creationflags=0x08000000)
    entries = set(acl_principals(path))
    required = {current_sid, 'S-1-5-18'}
    if entries != required:
        extra = ', '.join(sorted(entries - required)) or 'none'
        missing = ', '.join(sorted(required - entries)) or 'none'
        raise BackupError('ACL', f'Unexpected permissions on {path}; extra SIDs: {extra}; missing SIDs: {missing}')


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
