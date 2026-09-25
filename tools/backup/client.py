"""Windows sync client. Only encrypted, validated archives become ready backups."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import string
import subprocess
import sys
import tempfile
import time
from backup_format import (BackupError, NAME_RE, atomic_json, check_encrypted, digest,
                           newest, now, read_json, validate_record, verify_zip)

TOOLS = Path(__file__).resolve().parent
DEFAULT_MESSAGES = {
    'success': {'title':'🦊 JR-Foxy: копію збережено',
                'body':'Монах, резервна копія за {created_at} вже на комп’ютері. Архів перевірено.'},
    'multiple': {'title':'🦊 JR-Foxy: копії завантажено',
                 'body':'Завантажено й перевірено архівів: {count}. Найновіший — {latest_created_at}.'},
    'error': {'title':'JR-Foxy: резервування потребує уваги',
              'body':'{reason}. Деталі: {log_path}.'},
    'partial': {'title':'JR-Foxy: завантаження незавершене',
                'body':'Перевірено архівів: {count}. Не вдалося отримати: {failed_count}. {reason}.'},
}
REASONS = {
    'NETWORK':'Немає зв’язку із сервером. Повторю спробу за розкладом',
    'HOST_KEY':'Змінився або не підтверджений SSH-ключ сервера',
    'AUTH':'Сервер відхилив окремий ключ резервування',
    'KEY':'Не знайдено потрібний ключ розшифрування',
    'DISK':'Диск або папка бекапів недоступні чи бракує місця',
    'ACL':'Не вдалося підтвердити захист папки бекапів',
    'CHECKSUM':'Контрольна сума архіву не збігається',
    'VERIFY':'Архів або база не пройшли перевірку',
    'FORMAT':'Сервер повернув некоректний опис архіву',
    'SERVER':'На сервері не вдалося створити нову копію',
    'TIMEOUT':'Перевищено час перевірки; старі копії збережено',
    'CONFIG':'Перевір налаштування резервування',
    'OTHER':'Операцію не завершено; старі копії збережено',
}


def format_message(template, values):
    for _, field, spec, conversion in string.Formatter().parse(template):
        if field is not None and (field not in values or spec or conversion):
            raise ValueError('Unsupported notification placeholder')
    return template.format_map(values)


class Client:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['folder'])
        self.keys = Path(config['private_key']).parent
        self.limit = time.monotonic()+900
        self.log_path = self.root/'logs'/(now().strftime('%Y-%m-%d')+'.log')

    def seconds(self, maximum):
        remaining = self.limit-time.monotonic()
        if remaining <= 0:
            raise BackupError('TIMEOUT','Client deadline exceeded')
        return min(maximum,remaining)

    def log(self, event, **fields):
        entry = json.dumps({'at':now().isoformat(),'event':event,**fields},ensure_ascii=False)+'\n'
        try:
            self.log_path.parent.mkdir(exist_ok=True)
            with self.log_path.open('a',encoding='utf-8') as output:
                output.write(entry)
        except OSError:
            # Only error logs may fall back to C:. Archives never do.
            fallback = self.keys.parent/'logs'
            fallback.mkdir(exist_ok=True)
            self.log_path = fallback/(now().strftime('%Y-%m-%d')+'.log')
            with self.log_path.open('a',encoding='utf-8') as output:
                output.write(entry)

    def notify(self, event, code=None, count=0, failed_count=0, latest=None):
        from windows_support import toast
        notice_path = self.keys/'notification-state.json'
        try:
            prior = read_json(notice_path,{})
        except (OSError,ValueError):
            prior = {}
        if code and prior.get('code') == code and now().timestamp()-prior.get('at',0) < 86400:
            return
        when = dt.datetime.fromisoformat(latest['created_at_kyiv']).strftime('%d.%m.%Y %H:%M') if latest else ''
        values = {'count':count,'failed_count':failed_count,'created_at':when,
                  'latest_created_at':when,'scheduled_at':'','folder':str(self.root),
                  'filename':latest['filename'] if latest else '',
                  'next_retry_at':'після наступного входу у Windows або планової перевірки',
                  'reason':REASONS.get(code,REASONS['OTHER']),'log_path':str(self.log_path)}
        try:
            templates = read_json(TOOLS/'notifications.uk.json',DEFAULT_MESSAGES)
            template = templates[event]
            title = format_message(template['title'],values)
            body = format_message(template['body'],values)
        except (OSError,ValueError,KeyError,TypeError):
            self.log('notification_template_invalid')
            title = format_message(DEFAULT_MESSAGES[event]['title'],values)
            body = format_message(DEFAULT_MESSAGES[event]['body'],values)
        try:
            toast(TOOLS,self.keys,title[:200],body[:3000])
            atomic_json(notice_path,{'code':code,'at':now().timestamp()})
        except Exception as exc:
            self.log('notification_failed',error_type=type(exc).__name__)

    def ssh(self, command, output=None):
        cfg=self.config
        args=[cfg['ssh'],'-T','-p',str(cfg['port']),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
              '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15',
              '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',
              '-o','UserKnownHostsFile='+cfg['known_hosts'],'-o','GlobalKnownHostsFile=NUL',
              '-i',cfg['ssh_key'],cfg['user']+'@'+cfg['host'],command]
        for delay in (0,60,180):
            if delay:
                if self.seconds(delay+1) < delay+1:
                    raise BackupError('TIMEOUT','Not enough time for another network retry')
                time.sleep(delay)
            try:
                if output:
                    with output.open('wb') as stream:
                        result=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.PIPE,
                                              timeout=self.seconds(300),creationflags=0x08000000)
                else:
                    result=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,
                                          timeout=self.seconds(45),creationflags=0x08000000)
            except subprocess.TimeoutExpired:
                self.log('network_retry',command=command.split()[0],code='TIMEOUT')
                continue
            if result.returncode == 0:
                if output:
                    return None
                try:
                    return json.loads(result.stdout)
                except (ValueError,UnicodeError):
                    raise BackupError('FORMAT','Invalid server response') from None
            error=result.stderr.lower()
            if b'host key verification failed' in error or b'remote host identification' in error:
                raise BackupError('HOST_KEY','Server identity check failed')
            if b'permission denied' in error:
                raise BackupError('AUTH','SSH authentication failed')
            if b'error=' in error:
                raise BackupError('SERVER','Restricted server operation failed')
            self.log('network_retry',command=command.split()[0],exit_code=result.returncode)
        raise BackupError('NETWORK','Server connection failed')

    def prepare(self):
        from windows_support import require_acl_volume
        if not self.root.is_dir():
            raise BackupError('DISK','Backup folder is unavailable')
        require_acl_volume(self.root)
        for name in ('state','logs','tmp'):
            (self.root/name).mkdir(exist_ok=True)
        for field in ('private_key','ssh_key','known_hosts'):
            if not Path(self.config[field]).is_file():
                raise BackupError('KEY' if field=='private_key' else 'CONFIG','Required key or trust file missing')
        for executable in ('age','age_keygen','ssh'):
            if not Path(self.config[executable]).is_file():
                raise BackupError('CONFIG','Required executable missing')
        try:
            public=subprocess.run([self.config['age_keygen'],'-y',self.config['private_key']],
                                  check=True,capture_output=True,timeout=self.seconds(15),
                                  creationflags=0x08000000).stdout.decode('ascii').strip()
        except (OSError,subprocess.SubprocessError,UnicodeError):
            raise BackupError('KEY','Cannot read the existing private key') from None
        if public!=self.config['recipient']:
            raise BackupError('KEY','The private key differs from the archive recipient')
        for stale in (self.root/'tmp').glob('.verify-*'):
            flags=getattr(stale.lstat(),'st_file_attributes',0)
            if stale.is_dir() and not stale.is_symlink() and not flags & 0x400:
                shutil.rmtree(stale)
        for folder in (self.root/'logs',self.keys.parent/'logs'):
            if folder.is_dir():
                for log in folder.glob('????-??-??.log'):
                    if log.stat().st_mtime < now().timestamp()-90*86400:
                        log.unlink()

    def verify(self, path, record):
        from windows_support import protect
        if record is not None:
            check_encrypted(path,record)
        elif not NAME_RE.fullmatch(path.name) or not 100 <= path.stat().st_size <= 600*1024*1024:
            raise BackupError('FORMAT','Unrecognized local archive')
        if shutil.disk_usage(self.root).free < max(path.stat().st_size*8,600*1024*1024):
            raise BackupError('DISK','Insufficient verification space')
        try:
            with tempfile.TemporaryDirectory(prefix='.verify-',dir=self.root/'tmp') as folder:
                work=Path(folder)
                protect(work)
                bundle=work/'backup.zip'
                subprocess.run([self.config['age'],'-d','-i',self.config['private_key'],
                                '-o',str(bundle),str(path)],check=True,capture_output=True,
                               timeout=self.seconds(120),creationflags=0x08000000)
                if record is None:
                    import zipfile
                    with zipfile.ZipFile(bundle) as archive:
                        if archive.getinfo('manifest.json').file_size > 4*1024*1024:
                            raise BackupError('FORMAT','Manifest is too large')
                        metadata=json.loads(archive.read('manifest.json'))
                    record=validate_record({key:metadata[key] for key in
                                             ('backup_id','created_at_utc','created_at_kyiv','recipient')}
                                           | {'filename':path.name,'size':path.stat().st_size,'sha256':digest(path)})
                manifest=verify_zip(bundle,work,record,self.config['recipient'])
                check_encrypted(path,record)
            return manifest,record
        except BackupError:
            raise
        except subprocess.TimeoutExpired:
            raise BackupError('TIMEOUT','Verification exceeded its time limit') from None
        except Exception:
            raise BackupError('VERIFY','Archive decryption or database verification failed') from None

    def remember(self, record):
        verified={**record,'status':'local_verified','verified_at_utc':now().isoformat(),
                  'verifier':'jr-foxy-backup'}
        atomic_json(self.root/'state'/(record['filename']+'.json'),verified)
        return verified

    def inventory(self):
        records=[]
        for sidecar in (self.root/'state').glob('JR-Foxy_*.zip.age.json'):
            try:
                record=validate_record(read_json(sidecar))
                if record.get('status')!='local_verified':
                    continue
                check_encrypted(self.root/record['filename'],record)
                records.append(record)
            except Exception:
                self.log('local_record_invalid',file=sidecar.name)
        return records

    def recover_unindexed(self, records):
        known={r['filename'] for r in records}
        for path in self.root.glob('JR-Foxy_*.zip.age'):
            if path.name in known or not NAME_RE.fullmatch(path.name):
                continue
            try:
                _,record=self.verify(path,None)
                records.append(self.remember(record))
                self.log('index_recovered',backup_id=record['backup_id'])
            except BackupError as exc:
                # Preserve every unverified file, never enroll it in retention.
                self.log('unindexed_file_preserved',file=path.name,code=exc.code)
        return records

    def sync(self, create=False):
        self.prepare()
        remote=self.ssh('status')
        if create:
            before={r['backup_id'] for r in remote['archives']}
            requested=now()
            self.ssh('create')
            for _ in range(120):
                self.seconds(5)
                time.sleep(5)
                remote=self.ssh('status')
                if any(r['backup_id'] not in before for r in remote['archives']):
                    break
                attempt=remote['last_attempt']
                if attempt.get('status')=='error' and dt.datetime.fromisoformat(attempt.get('failed_at',requested.isoformat()))>=requested:
                    raise BackupError('SERVER','Requested backup failed')
            else:
                raise BackupError('TIMEOUT','Requested backup did not finish')
        records=remote.get('archives',[])
        if len(records)>6 or len({r['backup_id'] for r in records})!=len(records):
            raise BackupError('FORMAT','Invalid remote archive list')
        for record in records:
            validate_record(record)
            if record.get('recipient')!=self.config['recipient']:
                raise BackupError('KEY','Server uses a different recipient')
        if not records:
            raise BackupError('SERVER','Server has no ready backups')
        local={r['backup_id']:r for r in self.recover_unindexed(self.inventory())}
        completed=[]
        errors=[]
        for record in newest(records):
            self.seconds(1)
            prior=local.get(record['backup_id'])
            if prior and prior['sha256']==record['sha256']:
                continue
            final=self.root/record['filename']
            partial=self.root/'tmp'/(record['filename']+'.part')
            try:
                try:
                    check_encrypted(final,record)
                    candidate=final
                except BackupError:
                    if shutil.disk_usage(self.root).free < record['size']+600*1024*1024:
                        raise BackupError('DISK','Insufficient download space')
                    self.ssh('get '+record['backup_id'],partial)
                    candidate=partial
                self.verify(candidate,record)
                if candidate!=final:
                    # Existing invalid files with the same ID are preserved in tmp.
                    if final.exists():
                        old=self.root/'tmp'/(record['filename']+'.invalid-'+str(time.time_ns()))
                        final.rename(old)
                    candidate.rename(final)
                local[record['backup_id']]=self.remember(record)
                completed.append(record)
                self.log('verified',backup_id=record['backup_id'])
            except BackupError as exc:
                errors.append(exc.code)
                self.log('archive_failed',backup_id=record['backup_id'],code=exc.code)
        server_error=remote.get('last_attempt',{}).get('status')=='error'
        if errors or server_error:
            code=errors[0] if errors else 'SERVER'
            self.notify('partial' if completed else 'error',code,len(completed),len(errors),
                        newest(completed,1)[0] if completed else None)
            atomic_json(self.root/'state/last-run.json',{'at':now().isoformat(),'status':'error','code':code})
            raise BackupError(code,'Synchronization incomplete; no local backups pruned')
        # Only after the whole batch succeeded. Foreign files have no record and
        # cannot enter this retention operation.
        keep={r['backup_id'] for r in newest(list(local.values()),6)}
        for ident,record in local.items():
            if ident not in keep:
                check_encrypted(self.root/record['filename'],record)
                (self.root/'state'/(record['filename']+'.json')).unlink(missing_ok=True)
                (self.root/record['filename']).unlink()
                self.log('pruned',backup_id=ident)
        atomic_json(self.root/'state/last-run.json',{'at':now().isoformat(),'status':'success',
                                                   'new_count':len(completed),'remote_latest':newest(records,1)[0]['created_at_kyiv']})
        if completed:
            self.notify('success' if len(completed)==1 else 'multiple',count=len(completed),latest=newest(completed,1)[0])
        else:
            atomic_json(self.keys/'notification-state.json',{})
        self.log('sync_complete',new_count=len(completed))
        return len(completed)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['sync','create','status','verify'],nargs='?',default='sync')
    parser.add_argument('--filename')
    args=parser.parse_args()
    from windows_support import single_instance
    config=read_json(TOOLS/'config.json')
    client=Client(config)
    try:
        with single_instance(client.keys/'client.lock'):
            if args.action in ('sync','create'):
                count=client.sync(args.action=='create')
                print('SYNC_OK; new_verified='+str(count))
            elif args.action=='status':
                client.prepare()
                print(json.dumps({'server':client.ssh('status'),'local':client.inventory(),
                                  'last_run':read_json(client.root/'state/last-run.json',{})},ensure_ascii=False,indent=2))
            else:
                client.prepare()
                if not args.filename or not NAME_RE.fullmatch(args.filename):
                    raise BackupError('FORMAT','Supply an archive filename from the backup folder')
                record=validate_record(read_json(client.root/'state'/(args.filename+'.json')))
                client.verify(client.root/args.filename,record)
                client.remember(record)
                print('LOCAL_BACKUP_VERIFIED')
    except BackupError as exc:
        if exc.code=='BUSY':
            return
        client.log('run_failed',code=exc.code)
        client.notify('error',exc.code)
        print('ERROR='+exc.code,file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        client.log('run_failed',code='OTHER',error_type=type(exc).__name__)
        client.notify('error','OTHER')
        print('ERROR='+type(exc).__name__,file=sys.stderr)
        sys.exit(1)


if __name__=='__main__':
    main()
