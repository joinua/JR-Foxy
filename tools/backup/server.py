#!/usr/bin/python3
"""Server backup worker and a strictly restricted SSH command endpoint."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import uuid
import zipfile
from zoneinfo import ZoneInfo
from backup_format import (BackupError, atomic_json, check_encrypted, digest,
                           newest, now, read_json, snapshot, validate_record)

CONFIG = Path('/etc/jr-foxy-backup/config.json')
ROOT = Path('/var/lib/jr-foxy-backup')


def settings():
    config = read_json(CONFIG)
    if not config or config.get('retain') != 6:
        raise BackupError('CONFIG', 'Backup configuration is missing or invalid')
    return config


@contextlib.contextmanager
def lock(name, shared=False, blocking=True):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / name).open('a') as stream:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(stream, flags | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise BackupError('BUSY', 'Another backup is running') from None
        yield


def period(at):
    local = at.astimezone(ZoneInfo('Europe/Kyiv'))
    result = (local - dt.timedelta(days=(local.weekday()-1) % 7)).replace(
        hour=18, minute=0, second=0, microsecond=0)
    if result > local:
        result -= dt.timedelta(days=7)
    return result.astimezone(dt.timezone.utc)


def catalog():
    records = read_json(ROOT / 'state/index.json', [])
    for record in records:
        validate_record(record)
    if len({r['backup_id'] for r in records}) != len(records):
        raise BackupError('FORMAT', 'Duplicate backup identifiers')
    return records


def import_initial():
    """Adopt root-owned first-run sidecars; never scan or remove foreign files."""
    with lock('catalog.lock'):
        records = catalog()
        known = {r['backup_id'] for r in records}
        for path in (ROOT / 'archives').glob('JR-Foxy_*.zip.age.json'):
            record = validate_record(read_json(path))
            if record['backup_id'] in known:
                continue
            archive = ROOT / 'archives' / record['filename']
            check_encrypted(archive, record)
            records.append(record)
            known.add(record['backup_id'])
        atomic_json(ROOT / 'state/index.json', records)
        state_file = ROOT / 'state/schedule.json'
        if not state_file.exists():
            latest = newest(records, 1)
            completed = period(dt.datetime.fromisoformat(latest[0]['created_at_utc'])) if latest else None
            atomic_json(state_file, {'completed_period': completed.isoformat() if completed else None})


def runtime_info(project):
    result = subprocess.run(['/usr/bin/docker', 'inspect', 'jr-foxy'], check=True,
                            capture_output=True, timeout=20)
    data = json.loads(result.stdout)[0]
    mounts = [m for m in data['Mounts'] if m['Destination'] == '/app/data']
    if not data['State']['Running'] or len(mounts) != 1 or Path(mounts[0]['Source']).resolve() != (project/'data').resolve():
        raise BackupError('SOURCE', 'The running bot or its data mount differs')
    return {'container_id': data['Id'], 'started_at': data['State']['StartedAt'],
            'image_id': data['Image'], 'image_reference': data['Config']['Image'],
            'version': (data['Config'].get('Labels') or {}).get('com.jokerrecon.jr-foxy.version', 'unknown')}


def make_backup(config, trigger, scheduled):
    project = Path(config['project'])
    recipient = Path(config['recipient_file']).read_text().strip()
    if not re.fullmatch(r'age1[0-9a-z]{58}', recipient):
        raise BackupError('KEY', 'Invalid age recipient')
    source = project / 'data/jrfoxy.db'
    sources = {'config/.env': project/'.env', 'config/docker-compose.yml': project/'docker-compose.yml'}
    payloads = {}
    for name, path in sources.items():
        if not path.is_file() or not path.stat().st_size:
            raise BackupError('SOURCE', 'A required source file is missing or empty')
        payloads[name] = path.read_bytes()
    if not source.is_file():
        raise BackupError('SOURCE', 'Source database is missing')
    if shutil.disk_usage(ROOT).free < source.stat().st_size*4 + 64*1024*1024:
        raise BackupError('DISK', 'Insufficient server disk space')
    runtime = runtime_info(project)
    created = now()
    local = created.astimezone(ZoneInfo('Europe/Kyiv'))
    ident = uuid.uuid4().hex
    name = 'JR-Foxy_' + local.strftime('%Y-%m-%d_%H-%M-%S_UTC%z_') + ident[:8] + '.zip.age'
    for stale in ROOT.glob('.worker-tmp-*'):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)
    with tempfile.TemporaryDirectory(prefix='.worker-tmp-', dir=ROOT) as folder:
        work = Path(folder)
        database = work/'jrfoxy.db'
        counts = snapshot(source, database)
        if runtime_info(project) != runtime or any(p.read_bytes() != payloads[n] for n,p in sources.items()):
            raise BackupError('CHANGED', 'Deployment changed during backup')
        payloads['RESTORE.md'] = Path(__file__).with_name('RESTORE.md').read_bytes()
        files = {n: {'size': len(b), 'sha256': hashlib.sha256(b).hexdigest()} for n,b in payloads.items()}
        files['data/jrfoxy.db'] = {'size': database.stat().st_size, 'sha256': digest(database)}
        manifest = {'format_version': 1, 'producer': 'jr-foxy-backup', 'backup_id': ident,
                    'trigger': trigger, 'scheduled_at': scheduled.isoformat() if scheduled else None,
                    'created_at_utc': created.isoformat(), 'created_at_kyiv': local.isoformat(),
                    'snapshot_verified_at_utc': now().isoformat(), 'recipient': recipient,
                    'runtime': runtime, 'files': files,
                    'sqlite': {'version': __import__('sqlite3').sqlite_version, 'integrity_check': 'ok',
                               'foreign_key_check': 'ok', 'table_counts': counts}}
        bundle = work/'backup.zip'
        with zipfile.ZipFile(bundle, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database, 'data/jrfoxy.db')
            for entry, data in payloads.items():
                archive.writestr(entry, data)
            archive.writestr('manifest.json', json.dumps(manifest, indent=2))
        encrypted = work/'backup.zip.age'
        subprocess.run(['/usr/bin/age', '-r', recipient, '-o', str(encrypted), str(bundle)],
                       check=True, capture_output=True, timeout=180)
        record = validate_record({'backup_id': ident, 'filename': name, 'recipient': recipient,
                                 'created_at_utc': created.isoformat(), 'created_at_kyiv': local.isoformat(),
                                 'completed_at_utc': now().isoformat(), 'size': encrypted.stat().st_size,
                                 'sha256': digest(encrypted), 'status': 'server_created'})
        with encrypted.open('rb') as stream:
            os.fsync(stream.fileno())
        with lock('catalog.lock'):
            records = catalog()
            destination = ROOT/'archives'/name
            if destination.exists():
                raise BackupError('COLLISION', 'Backup name already exists')
            encrypted.rename(destination)
            # A complete archive plus its root-owned sidecar is the recovery record.
            atomic_json(destination.with_suffix(destination.suffix+'.json'), record)
            records.append(record)
            atomic_json(ROOT/'state/index.json', records)
            keep = newest(records, config['retain'])
            keep_ids = {r['backup_id'] for r in keep}
            # Drop index references first; unindexed files after a crash are harmless.
            atomic_json(ROOT/'state/index.json', keep)
            for old in records:
                if old['backup_id'] not in keep_ids:
                    archive = ROOT/'archives'/old['filename']
                    archive.with_suffix(archive.suffix+'.json').unlink(missing_ok=True)
                    archive.unlink(missing_ok=True)
    return record


def worker(manual=False):
    with lock('worker.lock', blocking=False):
        config = settings()
        for stale in ROOT.glob('.worker-tmp-*'):
            if stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale)
        import_initial()
        scheduled = period(now())
        state = read_json(ROOT/'state/schedule.json', {})
        done = state.get('completed_period')
        if not manual and done and dt.datetime.fromisoformat(done) >= scheduled:
            return None
        trigger = 'manual' if manual else ('scheduled' if now()-scheduled < dt.timedelta(minutes=5) else 'catch_up')
        atomic_json(ROOT/'state/attempt.json', {'status': 'running', 'started_at': now().isoformat(),
                                                'scheduled_at': scheduled.isoformat(), 'trigger': trigger})
        try:
            record = make_backup(config, trigger, scheduled)
            state['completed_period'] = scheduled.isoformat()
            atomic_json(ROOT/'state/schedule.json', state)
            atomic_json(ROOT/'state/attempt.json', {'status': 'success', 'completed_at': now().isoformat(),
                                                   'backup_id': record['backup_id']})
            audit('success', record['backup_id'])
            return record
        except Exception as exc:
            code = exc.code if isinstance(exc, BackupError) else type(exc).__name__
            reason = str(exc) if isinstance(exc, (BackupError, RuntimeError)) else code
            atomic_json(ROOT/'state/attempt.json', {'status': 'error', 'failed_at': now().isoformat(),
                                                   'scheduled_at': scheduled.isoformat(), 'code': code, 'reason':reason})
            audit('error', {'code':code, 'reason':reason})
            raise


def audit(event, detail):
    logs = ROOT/'logs'
    logs.mkdir(exist_ok=True)
    with (logs/(now().strftime('%Y-%m-%d')+'.log')).open('a') as output:
        output.write(json.dumps({'at': now().isoformat(), 'event': event, 'detail': detail})+'\n')
    cutoff = now().timestamp() - 90*86400
    for path in logs.glob('????-??-??.log'):
        if path.stat().st_mtime < cutoff:
            path.unlink()


def status():
    with lock('catalog.lock', shared=True):
        records = newest(catalog())
    last = read_json(ROOT/'state/attempt.json', {})
    if last.get('status') == 'running':
        with (ROOT/'worker.lock').open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                last = {**last, 'status': 'error', 'code': 'INTERRUPTED'}
    done = read_json(ROOT/'state/schedule.json', {}).get('completed_period')
    return {'archives': records, 'last_attempt': last, 'completed_period': done,'server_time_utc':now().isoformat(),
            'timezone': 'Europe/Kyiv', 'retain': 6,
            'next_scheduled_at': (period(now()).astimezone(ZoneInfo('Europe/Kyiv'))+dt.timedelta(days=7)).isoformat()}


def api(command):
    if command in {'status', 'list'}:
        report = status()
        print(json.dumps(report if command == 'status' else report['archives']))
    elif re.fullmatch(r'get [0-9a-f]{32}', command):
        ident = command[4:]
        with lock('catalog.lock', shared=True):
            record = next((r for r in catalog() if r['backup_id'] == ident), None)
            if record is None:
                raise BackupError('MISSING', 'Archive is no longer available')
            with (ROOT/'archives'/record['filename']).open('rb') as stream:
                shutil.copyfileobj(stream, sys.stdout.buffer)
    elif command == 'create':
        # Fixed unit name, no caller arguments or shell evaluation.
        subprocess.run(['/usr/bin/systemctl', 'start', '--no-block', 'jr-foxy-backup-manual.service'],
                       check=True, capture_output=True, timeout=15)
        print(json.dumps({'status': 'requested'}))
    else:
        raise BackupError('DENIED', 'Only status, list, get <id>, and create are permitted')


def deadline(signum, frame):
    raise BackupError('TIMEOUT', 'Backup operation exceeded its time limit')


if __name__ == '__main__':
    os.umask(0o077)
    signal.signal(signal.SIGALRM, deadline)
    signal.signal(signal.SIGTERM, deadline)
    signal.alarm(580 if sys.argv[1:] != ['api'] else 300)
    try:
        if os.geteuid() != 0:
            raise BackupError('DENIED', 'Root service required')
        if sys.argv[1:] == ['api']:
            api(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        elif sys.argv[1:] in (['due'], ['manual']):
            result = worker(sys.argv[1] == 'manual')
            print(json.dumps({'status': 'created' if result else 'not_due', 'archive': result}))
        elif sys.argv[1:] == ['import-initial']:
            import_initial()
        else:
            raise BackupError('DENIED', 'Invalid command')
    except BackupError as exc:
        if exc.code == 'BUSY':
            print(json.dumps({'status': 'busy'}))
        else:
            print('ERROR='+exc.code, file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print('ERROR='+type(exc).__name__, file=sys.stderr)
        sys.exit(1)
