#!/usr/bin/python3
"""Install from the reviewed local bundle over the owner's existing SSH login."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

TARGET = Path('/opt/jr-foxy-backup')
CONFIG = Path('/etc/jr-foxy-backup')
ROOT = Path('/var/lib/jr-foxy-backup')
UNITS = Path('/etc/systemd/system')
FORCED = 'restrict,command="/usr/bin/python3 -I /opt/jr-foxy-backup/server.py api" '


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=40, **kwargs)


def atomic(path, data, mode=0o600):
    fd, temp = tempfile.mkstemp(prefix='.foxy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def service(mode):
    return f'''[Unit]
Description=JR-Foxy encrypted backup ({mode})
After=docker.service time-sync.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /opt/jr-foxy-backup/server.py {mode}
TimeoutStartSec=600
KillMode=control-group
UMask=0077
PrivateTmp=true
ProtectSystem=full
NoNewPrivileges=true
Nice=10
'''


def install():
    data = json.load(sys.stdin)
    key = data['ssh_public_key'].strip()
    recipient = data['recipient'].strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [^\r\n]*)?', key):
        raise RuntimeError('Invalid SSH public key')
    key = ' '.join(key.split()[:2]) + ' JR-Foxy-Backup-managed'
    if not re.fullmatch(r'age1[0-9a-z]{58}', recipient):
        raise RuntimeError('Invalid age public key')
    for binary in ('/usr/bin/age', '/usr/bin/docker', '/usr/bin/systemctl', '/usr/bin/python3'):
        if not Path(binary).is_file():
            raise RuntimeError('Required server dependency missing')
    run(['systemd-analyze', 'calendar', 'Tue *-*-* 18:00:00 Europe/Kyiv'], capture_output=True)
    for folder in (TARGET, CONFIG, ROOT, ROOT/'archives', ROOT/'state', ROOT/'logs'):
        if folder.is_symlink():
            raise RuntimeError('Refusing an unexpected symbolic link')
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
    recipient_path = CONFIG/'recipient.txt'
    if recipient_path.exists() and recipient_path.read_text().strip() != recipient:
        raise RuntimeError('Existing age key differs; keys are never replaced automatically')
    atomic(recipient_path, (recipient+'\n').encode())
    config_path = CONFIG/'config.json'
    if not config_path.exists():
        atomic(config_path, json.dumps({'project': '/home/JR-Foxy', 'retain': 6,
                                        'recipient_file': str(recipient_path)}).encode())
    keyfile = CONFIG/'ssh-public-key.txt'
    if keyfile.exists() and keyfile.read_text().strip() != key:
        raise RuntimeError('Existing backup SSH key differs; automatic key replacement refused')
    atomic(keyfile, (key+'\n').encode())
    with (ROOT/'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in ('backup_format.py', 'server.py', 'RESTORE.md'):
            atomic(TARGET/name, Path(__file__).with_name(name).read_bytes())
    auth_dir = Path('/root/.ssh')
    auth_dir.mkdir(mode=0o700, exist_ok=True)
    auth = auth_dir/'authorized_keys'
    text = auth.read_text() if auth.exists() else ''
    forced_line = FORCED + key
    key_blob = key.split()[1]
    related = [line for line in text.splitlines() if key_blob in line.split()]
    if related and related != [forced_line]:
        raise RuntimeError('This SSH key already has different authorization options')
    if not related:
        atomic(auth, (text.rstrip()+'\n'+forced_line+'\n').lstrip('\n').encode())
    for name, mode in (('jr-foxy-backup', 'due'), ('jr-foxy-backup-manual', 'manual')):
        atomic(UNITS/(name+'.service'), service(mode).encode(), 0o644)
    weekly = '''[Unit]
Description=JR-Foxy Tuesday 18:00 Europe/Kyiv
[Timer]
OnCalendar=Tue *-*-* 18:00:00 Europe/Kyiv
Persistent=true
AccuracySec=1s
Unit=jr-foxy-backup.service
[Install]
WantedBy=timers.target
'''
    retry = '''[Unit]
Description=JR-Foxy retry unfinished backup periods
[Timer]
OnBootSec=2min
OnCalendar=*-*-* *:05:00
Persistent=true
AccuracySec=1min
Unit=jr-foxy-backup.service
[Install]
WantedBy=timers.target
'''
    atomic(UNITS/'jr-foxy-backup.timer', weekly.encode(), 0o644)
    atomic(UNITS/'jr-foxy-backup-retry.timer', retry.encode(), 0o644)
    run(['/usr/bin/python3', '-I', str(TARGET/'server.py'), 'import-initial'], capture_output=True)
    run(['/usr/bin/systemctl', 'daemon-reload'], capture_output=True)
    run(['systemd-analyze', 'verify', str(UNITS/'jr-foxy-backup.service'),
         str(UNITS/'jr-foxy-backup-manual.service'), str(UNITS/'jr-foxy-backup.timer'),
         str(UNITS/'jr-foxy-backup-retry.timer')], capture_output=True)
    print('SERVER_INSTALLED: ready for end-to-end verification; scheduling enabled separately')


def enable():
    # Called by the installer only after a real client download and verification.
    run(['/usr/bin/systemctl', 'enable', '--now', 'jr-foxy-backup.timer', 'jr-foxy-backup-retry.timer'], capture_output=True)
    run(['/usr/bin/systemctl', 'is-active', '--quiet', 'jr-foxy-backup.timer', 'jr-foxy-backup-retry.timer'])
    print('SERVER_SCHEDULE_ENABLED: Tuesday 18:00 Europe/Kyiv, hourly retries')
    run(['/usr/bin/systemctl', 'list-timers', '--all', '--no-pager', 'jr-foxy-backup*'])


if __name__ == '__main__':
    os.umask(0o077)
    try:
        if os.geteuid() != 0:
            raise RuntimeError('Run server installation as root')
        if sys.argv[1:] == ['enable']:
            enable()
        elif not sys.argv[1:]:
            install()
        else:
            raise RuntimeError('Unknown installer operation')
    except Exception as exc:
        print('SERVER_INSTALL_ERROR: '+type(exc).__name__, file=sys.stderr)
        if isinstance(exc, RuntimeError):
            print(str(exc), file=sys.stderr)
        sys.exit(1)
