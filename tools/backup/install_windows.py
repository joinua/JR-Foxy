"""Interactive one-time installer; background runs use only the restricted key."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from backup_format import BackupError, atomic_json, now, read_json
from windows_support import protect, powershell, sid, single_instance

PACKAGE = Path(__file__).resolve().parent
FILES = ('backup_format.py','client.py','windows_support.py','Notify.ps1',
         'Register-Windows.ps1','Status.ps1','README.uk.md','RESTORE.md')
SERVER_FILES = ('backup_format.py','server.py','install_server.py','RESTORE.md')


def execute(args, **kwargs):
    result=subprocess.run(args,check=True,**kwargs)
    return result


def install(args):
    if os.name!='nt' or sys.version_info < (3,11):
        raise BackupError('PLATFORM','Run with Python 3.11 or newer on Windows')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',args.host) or not 1 <= args.port <= 65535:
        raise BackupError('CONFIG','Invalid SSH hostname')
    folder=Path(args.folder)
    if not folder.is_dir():
        raise BackupError('DISK','The existing backup folder is unavailable')
    tools=folder.parent/'BackupTools'
    keys=Path(os.environ['LOCALAPPDATA'])/'JR-Foxy/Keys'
    if not (keys/'backup.agekey').is_file():
        raise BackupError('KEY','The existing age private key is missing; it will not be regenerated')
    owner=sid()
    protect(keys,owner)
    protect(folder,owner)
    tools.mkdir(exist_ok=True)
    protect(tools,owner)
    for name in ('tmp','state','logs'):
        (folder/name).mkdir(exist_ok=True)
        protect(folder/name,owner)
    with single_instance(keys/'installer.lock'), single_instance(keys/'client.lock'):
        bins={name:shutil.which(name) for name in ('age','age-keygen','ssh','scp','ssh-keygen')}
        if not all(bins.values()):
            raise BackupError('DEPENDENCY','age and Windows OpenSSH must be installed')
        recipient=execute([bins['age-keygen'],'-y',str(keys/'backup.agekey')],capture_output=True,timeout=15).stdout.decode('ascii').strip()
        if not re.fullmatch(r'age1[0-9a-z]{58}',recipient):
            raise BackupError('KEY','Expected a standard age recipient')
        ssh_key=keys/'backup_ssh_ed25519'
        if not ssh_key.exists():
            execute([bins['ssh-keygen'],'-t','ed25519','-N','','-C','JR-Foxy Backup','-f',str(ssh_key)],timeout=30)
        protect(ssh_key,owner)
        public=execute([bins['ssh-keygen'],'-y','-P','','-f',str(ssh_key)],capture_output=True,timeout=15).stdout.decode('ascii').strip()
        if not public.startswith('ssh-ed25519 '):
            raise BackupError('KEY','The existing backup SSH key has an unsupported type')
        (keys/'backup_ssh_ed25519.pub').write_text(public+'\n',encoding='ascii')
        trust=keys/'known_hosts'
        lookup=args.host if args.port==22 else '['+args.host+']:'+str(args.port)
        if not trust.exists():
            existing=Path.home()/'.ssh/known_hosts'
            result=execute([bins['ssh-keygen'],'-F',lookup,'-f',str(existing)],capture_output=True,timeout=15)
            lines=[line for line in result.stdout.decode('ascii').splitlines() if line and not line.startswith('#')]
            if not lines:
                raise BackupError('HOST_KEY','No previously trusted host key; verify it through the provider first')
            trust.write_text('\n'.join(lines)+'\n',encoding='ascii')
        protect(trust,owner)
        manual_key=Path(args.admin_key).expanduser().resolve()
        if not manual_key.is_file():
            raise BackupError('KEY','The existing administrative SSH key is missing')
        ssh_options=['-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15',
                     '-o','UserKnownHostsFile='+str(trust),'-o','GlobalKnownHostsFile=NUL','-i',str(manual_key)]
        ssh=[bins['ssh'],'-T','-p',str(args.port),*ssh_options,'root@'+args.host]
        for name in FILES:
            if (PACKAGE/name).resolve() != (tools/name).resolve():
                shutil.copyfile(PACKAGE/name,tools/name)
        from client import DEFAULT_MESSAGES
        templates=tools/'notifications.uk.json'
        if not templates.exists():
            atomic_json(templates,DEFAULT_MESSAGES)
        config_path=tools/'config.json'
        config={'host':args.host,'port':args.port,'user':'root','folder':str(folder),
                'recipient':recipient,'private_key':str(keys/'backup.agekey'),'ssh_key':str(ssh_key),
                'known_hosts':str(trust),'age':bins['age'],'age_keygen':bins['age-keygen'],
                'ssh':bins['ssh'],'python':sys.executable,'retain':6}
        if config_path.exists():
            prior=read_json(config_path)
            for field in ('host','port','folder','recipient','private_key','ssh_key','known_hosts'):
                if prior.get(field)!=config[field]:
                    raise BackupError('CONFIG','Existing settings differ; automatic replacement refused')
            config={**config,**prior}
        atomic_json(config_path,config)
        print('LOCAL_KEYS_AND_CONFIGURATION_OK',flush=True)
        with tempfile.TemporaryDirectory(prefix='.setup-',dir=keys) as temp:
            bundle=Path(temp)/'server.zip'
            with zipfile.ZipFile(bundle,'x',compression=zipfile.ZIP_DEFLATED) as archive:
                for name in SERVER_FILES:
                    archive.write(PACKAGE/name,name)
            execute([bins['scp'],'-P',str(args.port),*ssh_options,str(bundle),
                     'root@'+args.host+':/root/jr-foxy-backup-setup.zip'],timeout=120)
        extract="python3 -c \"import zipfile; z=zipfile.ZipFile('/root/jr-foxy-backup-setup.zip'); z.extractall('/root/jr-foxy-backup-setup')\""
        execute([*ssh,extract],timeout=30)
        payload=json.dumps({'ssh_public_key':public,'recipient':recipient}).encode('ascii')
        execute([*ssh,'python3 /root/jr-foxy-backup-setup/install_server.py'],input=payload,timeout=120)
        from client import Client
        checker=Client(config)
        report=checker.ssh('status')
        if abs((dt.datetime.fromisoformat(report['server_time_utc'])-now()).total_seconds())>300:
            raise BackupError('CLOCK','Server and PC clocks differ by more than five minutes')
        # A harmless forbidden command must fail using the new restricted key.
        restricted=[bins['ssh'],'-T','-p',str(args.port),'-o','BatchMode=yes','-o','IdentitiesOnly=yes',
                    '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15',
                    '-o','UserKnownHostsFile='+str(trust),'-o','GlobalKnownHostsFile=NUL',
                    '-i',str(ssh_key),'root@'+args.host,'whoami']
        forbidden=subprocess.run(restricted,capture_output=True,timeout=30)
        if forbidden.returncode==0 or b'ERROR=DENIED' not in forbidden.stderr:
            raise BackupError('ACCESS','Restricted SSH access did not pass its isolation check')
        result=powershell(tools/'Register-Windows.ps1','-Python',sys.executable,'-Tools',tools,'-Mode','Prepare')
        print(result.stdout.decode('utf-8',errors='replace').strip(),flush=True)
    # Release the installation lock before the real client's process takes it.
    print('Creating and verifying a real backup through the restricted SSH key...',flush=True)
    execute([sys.executable,str(tools/'client.py'),'create'],timeout=920)
    result=powershell(tools/'Register-Windows.ps1','-Python',sys.executable,'-Tools',tools,'-Mode','Enable')
    print(result.stdout.decode('utf-8',errors='replace').strip(),flush=True)
    execute([*ssh,'python3 /root/jr-foxy-backup-setup/install_server.py enable'],timeout=90)
    report=Client(config).ssh('status')
    print('BACKUP_AUTOMATION_READY')
    print('NEXT_SERVER_BACKUP='+report['next_scheduled_at'])
    print('LOCAL_FOLDER='+str(folder))
    print('NOTIFICATION_TEMPLATES='+str(templates))
    print('PRIVATE_KEYS='+str(keys))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',required=True)
    parser.add_argument('--port',type=int,default=22)
    parser.add_argument('--folder',default=r'D:\JR-Foxy\Backup')
    parser.add_argument('--admin-key',required=True)
    args=parser.parse_args()
    try:
        install(args)
    except BackupError as exc:
        print('INSTALL_ERROR='+exc.code+': '+str(exc),file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print('INSTALL_ERROR: an installation step failed (exit '+str(exc.returncode)+').',file=sys.stderr)
        if exc.stderr:
            print(exc.stderr.decode('utf-8',errors='replace')[:2000],file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print('INSTALL_ERROR='+type(exc).__name__+': '+str(exc),file=sys.stderr)
        sys.exit(1)
