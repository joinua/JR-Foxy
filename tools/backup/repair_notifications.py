"""Update and test only the Windows notification component; no SSH or backups."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from backup_format import BackupError, read_json
from windows_support import powershell, single_instance

PACKAGE = Path(__file__).resolve().parent
PREVIOUS = {
    'Notify.ps1': 'fb9badb9ff2b459a7052d79aaa0654313c15426e922d807fce2a31872a373c24',
    'windows_support.py': 'f57e0f7c608f90a2bb35151fb59f479e1beaa2b4a1c14ccb391810087bc0f23a',
    'client.py': 'cad735dfc8886dff67ed41c532335815530937ce570e432f173a5ee8b1c6dba6',
}


def replace_file(path, data):
    fd, name = tempfile.mkstemp(prefix='.notification-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def repair(folder):
    if os.name != 'nt':
        raise BackupError('PLATFORM', 'Run this repair on Windows.')
    folder = folder.resolve()
    if folder == PACKAGE:
        raise BackupError('CONFIG', 'Extract the update separately from the installed BackupTools folder.')
    config = read_json(folder / 'config.json')
    keys = Path(config['private_key']).parent
    with single_instance(keys / 'installer.lock'), single_instance(keys / 'client.lock'):
        changes = []
        for name, previous_hash in PREVIOUS.items():
            target = folder / name
            before, after = target.read_bytes(), (PACKAGE / name).read_bytes()
            if before == after:
                continue
            if hashlib.sha256(before).hexdigest() != previous_hash:
                raise BackupError('VERSION', f'{name} has other changes; automatic replacement refused.')
            saved = folder / (name + '.before-notification-fix')
            if saved.exists() and saved.read_bytes() != before:
                raise BackupError('VERSION', f'An earlier saved version of {name} differs; stopped.')
            changes.append((target, saved, before, after))
        # Validate all inputs before changing any installed file.
        for _, saved, before, _ in changes:
            if not saved.exists():
                with saved.open('xb') as output:
                    output.write(before)
        changed = []
        try:
            for target, _, before, after in changes:
                replace_file(target, after)
                changed.append((target, before))
        except Exception:
            for target, before in reversed(changed):
                replace_file(target, before)
            raise
        print('NOTIFICATION_COMPONENT_UPDATED', flush=True)
        # Prepare only: refresh the existing AppUserModelID and Start menu link.
        # No task registration, notification preference, or server call here.
        result = powershell(folder / 'Register-Windows.ps1', '-Python', sys.executable,
                            '-Tools', folder, '-Mode', 'Prepare')
        print(result.stdout.decode('utf-8', errors='replace').strip(), flush=True)
        result = powershell(folder / 'Notify.ps1', '-Test')
        print(result.stdout.decode('utf-8', errors='replace').strip(), flush=True)
        print('VISUAL_CONFIRMATION_REQUIRED: check the banner and Win+N.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tools', type=Path, default=Path(r'D:\JR-Foxy\BackupTools'))
    args = parser.parse_args()
    try:
        repair(args.tools)
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout.decode('utf-8', errors='replace'), end='')
        if exc.stderr:
            print(exc.stderr.decode('utf-8', errors='replace'), end='')
        print('NOTIFICATION_REPAIR_ERROR: exit code', exc.returncode)
        sys.exit(1)
    except BackupError as exc:
        print('NOTIFICATION_REPAIR_ERROR=' + exc.code + ': ' + str(exc))
        sys.exit(1)
