"""Offline tests: real SQLite/ZIP, mocked encryption/SSH/Windows boundaries."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import zipfile
from zoneinfo import ZoneInfo
import backup_format as fmt
import server
from client import Client, format_message

PUBLIC='age1'+'q'*58  # Encryption is stubbed; this is not a real key.


def fixture_db(path):
    db=sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA wal_autocheckpoint=0')
    for name in fmt.REQUIRED_TABLES:
        db.execute(f'CREATE TABLE {name}(id INTEGER PRIMARY KEY)')
        db.execute(f'INSERT INTO {name} VALUES(1)')
    db.execute('CREATE TABLE balance(id INTEGER PRIMARY KEY, value INTEGER)')
    db.executemany('INSERT INTO balance VALUES(?, ?)',[(1,0),(2,0)])
    db.commit()
    return db


def record(index, payload=b'x'*200):
    ident=f'{index:08x}'+'a'*24
    stamp=dt.datetime(2026,9,1,tzinfo=dt.timezone.utc)+dt.timedelta(days=index)
    local=stamp.astimezone(ZoneInfo('Europe/Kyiv'))
    name='JR-Foxy_'+local.strftime('%Y-%m-%d_%H-%M-%S_UTC%z_')+ident[:8]+'.zip.age'
    return {'backup_id':ident,'filename':name,'created_at_utc':stamp.isoformat(),
            'created_at_kyiv':local.isoformat(),'size':len(payload),
            'sha256':hashlib.sha256(payload).hexdigest(),'recipient':PUBLIC,'status':'server_created'}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_tuesday_boundary(self):
        before=dt.datetime.fromisoformat('2026-09-29T17:59:59+03:00')
        at=dt.datetime.fromisoformat('2026-09-29T18:00:00+03:00')
        self.assertEqual(server.period(before).isoformat(),'2026-09-22T15:00:00+00:00')
        self.assertEqual(server.period(at).isoformat(),'2026-09-29T15:00:00+00:00')

    def test_kyiv_daylight_saving(self):
        spring=dt.datetime.fromisoformat('2026-03-31T18:00:00+03:00')
        autumn=dt.datetime.fromisoformat('2026-10-27T18:00:00+02:00')
        self.assertEqual(server.period(spring).hour,15)
        self.assertEqual(server.period(autumn).hour,16)

    def test_wal_snapshot_during_transactions(self):
        source=self.root/'live.db'
        connection=fixture_db(source)
        self.addCleanup(connection.close)
        stop=threading.Event()
        def writer():
            db=sqlite3.connect(source)
            try:
                for i in range(1,1000):
                    if stop.is_set(): break
                    db.execute('UPDATE balance SET value=?',(i,))
                    db.commit()
            finally:
                db.close()
        thread=threading.Thread(target=writer)
        thread.start()
        try:
            for i in range(3):
                destination=self.root/f'copy-{i}.db'
                counts=fmt.snapshot(source,destination)
                self.assertEqual(counts['balance'],2)
                with sqlite3.connect(destination) as db:
                    self.assertEqual(db.execute('SELECT COUNT(DISTINCT value) FROM balance').fetchone(),(1,))
                self.assertFalse(Path(str(destination)+'-wal').exists())
        finally:
            stop.set(); thread.join()

    def test_missing_database_not_created(self):
        missing=self.root/'missing.db'
        with self.assertRaises(RuntimeError):
            fmt.snapshot(missing,self.root/'copy.db')
        self.assertFalse(missing.exists())

    def test_foreign_key_violation_rejected(self):
        source=self.root/'live.db'
        db=fixture_db(source)
        db.execute('CREATE TABLE child(id INTEGER REFERENCES profiles(id))')
        db.execute('INSERT INTO child VALUES(999)'); db.commit()
        try:
            with self.assertRaisesRegex(RuntimeError,'foreign_key_check'):
                fmt.snapshot(source,self.root/'copy.db')
        finally:
            db.close()

    def make_bundle(self, counts_wrong=False, extra=False, corrupt=False):
        database=self.root/'base.db'
        db=fixture_db(database); db.close()
        data={'data/jrfoxy.db':database.read_bytes(),'config/.env':b'TOKEN=fake\n',
              'config/docker-compose.yml':b'services: {}\n','RESTORE.md':b'Restore offline\n'}
        rec=record(7)
        manifest={'format_version':1,'producer':'jr-foxy-backup','recipient':PUBLIC,
                  'backup_id':rec['backup_id'],'created_at_kyiv':rec['created_at_kyiv'],
                  'files':{n:{'size':len(b),'sha256':hashlib.sha256(b).hexdigest()} for n,b in data.items()},
                  'sqlite':{'integrity_check':'ok','foreign_key_check':'ok',
                            'table_counts':{**{n:1 for n in fmt.REQUIRED_TABLES},'balance':2}}}
        if counts_wrong: manifest['sqlite']['table_counts']['profiles']=2
        if corrupt: data['config/.env']=b'TOKEN=oops\n'
        bundle=self.root/'archive.zip'
        with zipfile.ZipFile(bundle,'w') as archive:
            for n,b in data.items(): archive.writestr(n,b)
            archive.writestr('manifest.json',json.dumps(manifest))
            if extra: archive.writestr('../escape','no')
        work=self.root/'verify'; work.mkdir()
        return bundle,work,rec

    def test_archive_verified_offline(self):
        bundle,work,rec=self.make_bundle()
        verified=fmt.verify_zip(bundle,work,rec,PUBLIC)
        self.assertEqual(verified['backup_id'],rec['backup_id'])

    def test_archive_path_traversal_rejected(self):
        bundle,work,rec=self.make_bundle(extra=True)
        with self.assertRaises(RuntimeError): fmt.verify_zip(bundle,work,rec,PUBLIC)
        self.assertFalse((self.root/'escape').exists())

    def test_archive_changed_payload_rejected(self):
        bundle,work,rec=self.make_bundle(corrupt=True)
        with self.assertRaises(RuntimeError): fmt.verify_zip(bundle,work,rec,PUBLIC)

    def test_archive_wrong_counts_rejected(self):
        bundle,work,rec=self.make_bundle(counts_wrong=True)
        with self.assertRaises(RuntimeError): fmt.verify_zip(bundle,work,rec,PUBLIC)

    def test_record_rejects_path_and_fake_id(self):
        rec=record(2)
        for name in ('../../.env','C:\\file.zip.age','JR-Foxy_bad.zip.age'):
            with self.assertRaises(fmt.BackupError): fmt.validate_record({**rec,'filename':name})
        with self.assertRaises(fmt.BackupError): fmt.validate_record({**rec,'backup_id':'b'*32})

    def test_ciphertext_corruption_rejected(self):
        path=self.root/'file.age'; path.write_bytes(b'y'*200)
        with self.assertRaises(fmt.BackupError): fmt.check_encrypted(path,record(1))

    def test_restricted_api_rejects_arbitrary_commands(self):
        with patch.object(server.subprocess,'run') as run:
            for command in ('','whoami','bash','status; cat /etc/shadow','get ../../.env',
                            'get '+'a'*32+';whoami','create now','list\nwhoami','sftp'):
                with self.assertRaises(fmt.BackupError): server.api(command)
            run.assert_not_called()

    def test_notification_placeholders_are_data(self):
        self.assertEqual(format_message('Hi {reason}',{'reason':'<script>'}),'Hi <script>')
        for text in ('{reason.__class__}','{missing}','{reason!r}','{reason:>100}'):
            with self.assertRaises(ValueError): format_message(text,{'reason':'x'})

    def make_client(self):
        for name in ('state','tmp','logs','keys'): (self.root/name).mkdir()
        client=Client({'folder':str(self.root),'private_key':str(self.root/'keys/key'),
                       'recipient':PUBLIC})
        client.prepare=Mock(); client.notify=Mock(); client.log=Mock(); client.verify=Mock()
        old=[record(i) for i in range(1,8)]
        for rec in old:
            (self.root/rec['filename']).write_bytes(b'x'*200)
            fmt.atomic_json(self.root/'state'/(rec['filename']+'.json'),{**rec,'status':'local_verified'})
        client.inventory=Mock(return_value=old)
        client.recover_unindexed=Mock(side_effect=lambda values:values)
        (self.root/'foreign.txt').write_text('Keep me')
        return client,old

    def test_partial_download_preserves_all_old_copies(self):
        client,old=self.make_client()
        remote=[record(9),record(10)]
        def ssh(command,output=None):
            if command=='status': return {'archives':remote,'last_attempt':{}}
            if command=='get '+remote[0]['backup_id']: raise fmt.BackupError('NETWORK','test interruption')
            output.write_bytes(b'x'*200)
        client.ssh=ssh
        with self.assertRaises(fmt.BackupError): client.sync()
        for rec in old: self.assertTrue((self.root/rec['filename']).is_file())
        self.assertTrue((self.root/'foreign.txt').exists())

    def test_success_keeps_latest_six_only(self):
        client,old=self.make_client()
        remote=[record(10)]
        def ssh(command,output=None):
            if command=='status': return {'archives':remote,'last_attempt':{}}
            output.write_bytes(b'x'*200)
        client.ssh=ssh
        self.assertEqual(client.sync(),1)
        self.assertEqual(len(list(self.root.glob('JR-Foxy_*.zip.age'))),6)
        self.assertFalse((self.root/old[0]['filename']).exists())
        self.assertTrue((self.root/remote[0]['filename']).exists())
        self.assertTrue((self.root/'foreign.txt').exists())

    def test_noop_does_not_notify(self):
        client,old=self.make_client()
        client.inventory=Mock(return_value=old[-6:])
        client.ssh=Mock(return_value={'archives':old[-6:],'last_attempt':{}})
        self.assertEqual(client.sync(),0)
        client.notify.assert_not_called()
        self.assertEqual(client.ssh.call_count,1)

    def test_failed_server_encryption_keeps_history_and_cleans_plaintext(self):
        root=self.root/'server'; (root/'archives').mkdir(parents=True)
        project=self.root/'project'; (project/'data').mkdir(parents=True)
        db=fixture_db(project/'data/jrfoxy.db'); db.close()
        (project/'.env').write_text('TOKEN=fake\n')
        (project/'docker-compose.yml').write_text('services: {}\n')
        key=self.root/'recipient'; key.write_text(PUBLIC)
        config={'project':str(project),'recipient_file':str(key),'retain':6}
        previous=root/'archives/previous.zip.age'; previous.write_bytes(b'previous')
        with patch.object(server,'ROOT',root), patch.object(server,'runtime_info',return_value={'image_id':'test'}), \
             patch.object(server.subprocess,'run',side_effect=subprocess.CalledProcessError(1,'age')):
            with self.assertRaises(subprocess.CalledProcessError): server.make_backup(config,'manual',None)
        self.assertEqual(previous.read_bytes(),b'previous')
        self.assertEqual(list(root.glob('.worker-tmp-*')),[])

    def test_server_publishes_verified_payload_then_rotates(self):
        root=self.root/'server'; (root/'archives').mkdir(parents=True)
        project=self.root/'project'; (project/'data').mkdir(parents=True)
        db=fixture_db(project/'data/jrfoxy.db'); db.close()
        (project/'.env').write_text('TOKEN=fake\n')
        (project/'docker-compose.yml').write_text('services: {}\n')
        key=self.root/'recipient'; key.write_text(PUBLIC)
        config={'project':str(project),'recipient_file':str(key),'retain':6}
        old=[record(i) for i in range(1,7)]
        for rec in old:
            archive=root/'archives'/rec['filename']; archive.write_bytes(b'x'*200)
            fmt.atomic_json(archive.with_suffix(archive.suffix+'.json'),rec)
        fmt.atomic_json(root/'state/index.json',old)
        foreign=root/'archives/foreign.age'; foreign.write_bytes(b'keep')
        # Fake transport envelope only; cryptographic round trips are a live
        # installation check using actual age on both machines.
        def encrypt(args,**kwargs):
            Path(args[args.index('-o')+1]).write_bytes(b'TEST-ENVELOPE\n'+Path(args[-1]).read_bytes())
            return subprocess.CompletedProcess(args,0)
        with patch.object(server,'ROOT',root), patch.object(server,'runtime_info',return_value={'image_id':'test'}), \
             patch.object(server.subprocess,'run',side_effect=encrypt):
            result=server.make_backup(config,'manual',None)
            listed=server.catalog()
        self.assertEqual(len(listed),6)
        self.assertFalse((root/'archives'/old[0]['filename']).exists())
        self.assertTrue(foreign.exists())
        bundle=self.root/'payload.zip'
        bundle.write_bytes((root/'archives'/result['filename']).read_bytes().split(b'\n',1)[1])
        work=self.root/'verify'; work.mkdir()
        fmt.verify_zip(bundle,work,result,PUBLIC)

    def test_failed_period_remains_due_and_retry_closes_it(self):
        root=self.root/'server'; (root/'archives').mkdir(parents=True)
        initial='2026-09-22T15:00:00+00:00'
        fmt.atomic_json(root/'state/schedule.json',{'completed_period':initial})
        time=dt.datetime.fromisoformat('2026-09-29T15:05:00+00:00')
        with patch.object(server,'ROOT',root), patch.object(server,'settings',return_value={'retain':6}), \
             patch.object(server,'now',return_value=time), patch.object(server,'import_initial'), \
             patch.object(server,'make_backup',side_effect=RuntimeError('Failed')):
            with self.assertRaises(RuntimeError): server.worker()
        self.assertEqual(fmt.read_json(root/'state/schedule.json')['completed_period'],initial)
        with patch.object(server,'ROOT',root), patch.object(server,'settings',return_value={'retain':6}), \
             patch.object(server,'now',return_value=time), patch.object(server,'import_initial'), \
             patch.object(server,'make_backup',return_value=record(9)) as create:
            self.assertIsNotNone(server.worker())
            self.assertIsNone(server.worker())
            create.assert_called_once()

    def test_long_outage_creates_one_current_copy(self):
        root=self.root/'server'; (root/'archives').mkdir(parents=True)
        fmt.atomic_json(root/'state/schedule.json',{'completed_period':'2026-08-04T15:00:00+00:00'})
        time=dt.datetime.fromisoformat('2026-09-30T12:00:00+00:00')
        with patch.object(server,'ROOT',root), patch.object(server,'settings',return_value={'retain':6}), \
             patch.object(server,'now',return_value=time), patch.object(server,'import_initial'), \
             patch.object(server,'make_backup',return_value=record(9)) as create:
            server.worker(); server.worker()
            create.assert_called_once()
            self.assertEqual(create.call_args.args[1],'catch_up')

    def test_two_workers_cannot_run_together(self):
        with patch.object(server,'ROOT',self.root):
            with server.lock('worker.lock',blocking=False):
                with self.assertRaises(fmt.BackupError):
                    with server.lock('worker.lock',blocking=False): pass


if __name__=='__main__': unittest.main()
