import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backups import backup_files, create_backup, read_archive, restore_backup
from google_mail import protected, Store
from server import create_app, RunnerBusy


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'source'
        self.env = patch.dict(os.environ, {
            'FM_BACKUPS_ENABLED': '0', 'FM_PUBLIC_URL': 'https://beta.example',
            'FM_STORAGE_ROOT': str(self.root), 'FM_SETUP_CODE': 'owner-setup-code-for-backup-tests',
            'FM_ENCRYPTION_KEY': secrets.token_hex(32),
            'FM_ALLOWED_EMAILS': 'owner@example.com,other@example.com',
            'GOOGLE_CLIENT_ID': 'test.apps.googleusercontent.com', 'GOOGLE_CLIENT_SECRET': 'fake',
            'RESEND_API_KEY': 'fake', 'FM_ALLOWED_FORM_HOSTS': 'forms.example',
        })
        self.env.start()
        self.app = create_app(); self.app.testing = True
        self.hub = self.app.extensions['hub']; self.client = self.app.test_client()
        self.password = 'original owner password'
        result = self.post(self.client, '/api/register', dict(email='owner@example.com',
            password=self.password, setup_code=os.environ['FM_SETUP_CODE']))
        self.assertEqual(result.status_code, 200)
        self.user = result.json['user']; self.workspace = self.hub.workspace(self.user)
        self.connection = 'b' * 32
        self.workspace.oauth.store.save(self.connection, dict(email='owner@gmail.example',
            client_id='test.apps.googleusercontent.com', access_token='fake-access',
            refresh_token='fake-refresh', expires_at=time.time()+3600))
        with patch('server.validate_url'):
            self.monitor = self.workspace.save(dict(name='Saved form',url='https://forms.example/contact/',
                interval=15,connection_id=self.connection))
        self.workspace.due[self.monitor] = time.time()+3600
        self.workspace.persist_schedule()
        self.statefile = self.workspace.root / 'results/monitors' / self.monitor / 'service/1/state.json'
        self.statefile.parent.mkdir(parents=True)
        self.statefile.write_text('{"notified_state":"DOWN"}')

    def tearDown(self):
        self.hub.close(); self.env.stop(); self.temp.cleanup()

    def get(self, client, path):
        return client.get(path, base_url='https://beta.example')

    def post(self, client, path, value=None):
        csrf = self.get(client, '/api/session').json['csrf']
        return client.post(path, json=value or {}, base_url='https://beta.example',
            headers={'Origin':'https://beta.example', 'X-CSRF-Token':csrf})

    def test_restore_real_account_settings_and_gmail_without_checks(self):
        archive = self.hub.backup()
        raw = archive.read_bytes()
        for secret in (b'owner@example.com', b'fake-refresh', self.password.encode(),
                       os.environ['FM_ENCRYPTION_KEY'].encode()):
            self.assertNotIn(secret, raw)
        manifest, files = read_archive(archive)
        self.assertEqual(manifest['accounts'], 1)
        destination = Path(self.temp.name) / 'restored'
        restore_backup(archive, destination)
        schedule = destination / self.workspace.schedule_file.relative_to(self.root)
        self.assertEqual(json.loads(schedule.read_text()), {})
        self.assertTrue(json.loads(self.workspace.schedule_file.read_text()))
        self.assertEqual((destination / self.statefile.relative_to(self.root)).read_bytes(), self.statefile.read_bytes())
        with patch.dict(os.environ, {'FM_STORAGE_ROOT': str(destination)}), \
                patch('workspace.subprocess.Popen') as processes, patch('server_google.request_json') as google:
            restored_app = create_app(); restored = restored_app.extensions['hub']
            try:
                result = self.post(restored_app.test_client(), '/api/login', dict(
                    email='owner@example.com', password=self.password))
                self.assertEqual(result.status_code, 200)
                w = restored.workspace(result.json['user'])
                self.assertEqual(w.profiles[0]['id'], self.monitor)
                self.assertEqual(w.profiles[0]['name'], 'Saved form')
                self.assertEqual(w.oauth.store.load(self.connection)['refresh_token'], 'fake-refresh')
                self.assertFalse(w.due)
                processes.assert_not_called(); google.assert_not_called()
            finally:
                restored.close()

    def test_owner_only_csrf_and_no_public_archive_access(self):
        unauth = self.app.test_client()
        self.assertEqual(self.post(unauth, '/api/backups/download').status_code, 401)
        other = self.app.test_client()
        self.assertEqual(self.post(other, '/api/register', dict(email='other@example.com',
            password='another account password')).status_code, 200)
        self.assertEqual(self.post(other, '/api/backups/download').status_code, 403)
        self.assertNotIn('backups', self.get(other, '/api/state').json)
        result = self.client.post('/api/backups/download', base_url='https://beta.example',
            headers={'Origin':'https://beta.example','X-CSRF-Token':'wrong'})
        self.assertEqual(result.status_code, 403)
        result = self.post(self.client, '/api/backups/download')
        self.assertEqual(result.status_code, 200)
        self.assertIn('attachment;', result.headers['Content-Disposition'])
        archive = backup_files(self.root)[0]
        self.assertEqual(result.data, archive.read_bytes()); result.close()
        self.assertEqual(self.get(unauth, '/backups/'+archive.name).status_code, 404)

    def test_corruption_wrong_key_and_existing_target_fail_before_writes(self):
        archive = self.hub.backup()
        destination = Path(self.temp.name) / 'restored'
        with patch.dict(os.environ, {'FM_ENCRYPTION_KEY':secrets.token_hex(32)}):
            with self.assertRaises(ValueError):restore_backup(archive, destination)
        self.assertFalse(destination.exists())
        broken = Path(self.temp.name)/'damaged.fmbackup'
        broken.write_bytes(archive.read_bytes()[:-20]+b'broken')
        with self.assertRaises(ValueError):restore_backup(broken, destination)
        self.assertFalse(destination.exists())
        with self.assertRaisesRegex(ValueError, 'new directory'):restore_backup(archive, self.root)
        self.assertTrue(self.hub.accounts.database.exists())

    def test_unexpected_paths_and_duplicate_entries_are_rejected(self):
        archive = self.hub.backup()
        raw = protected(archive.read_bytes(), decrypt=True)
        for name in ('../escape', '/tmp/escape', 'results/accounts.sqlite3'):
            buffer = io.BytesIO(raw)
            with zipfile.ZipFile(buffer, 'a') as bundle:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    bundle.writestr(name, b'bad')
            bad = Path(self.temp.name)/'unsafe.fmbackup'
            bad.write_bytes(protected(buffer.getvalue()))
            with self.assertRaises(ValueError):restore_backup(bad, Path(self.temp.name)/'unsafe-output')
            self.assertFalse((Path(self.temp.name)/'unsafe-output').exists())

    def test_only_core_data_is_copied_and_retention_follows_verified_success(self):
        history = self.workspace.root / 'results/monitors' / self.monitor / 'reports/MONITOR-test'
        history.mkdir(parents=True); (history/'page.png').write_bytes(b'screenshot')
        unrelated = self.root/'backups'/'keep.txt'
        unrelated.parent.mkdir(exist_ok=True); unrelated.write_text('keep')
        for _ in range(8):
            archive = self.hub.backup()
        self.assertEqual(len(backup_files(self.root)), 7)
        _, files = read_archive(archive)
        self.assertFalse(any('reports/' in name for name in files))
        self.assertTrue(unrelated.exists())
        before = backup_files(self.root)
        self.workspace.oauth.store.path(self.connection).write_bytes(b'broken token')
        with self.assertRaises(ValueError):self.hub.backup()
        self.assertEqual(backup_files(self.root), before)
        self.assertEqual(list((self.root/'backups').glob('.fm-stage-*')), [])

    def test_active_check_defers_backup_and_mutation_gate_is_released(self):
        self.hub.runner.acquire()
        try:
            with self.assertRaises(RunnerBusy):self.hub.backup()
            self.assertEqual(self.post(self.client,'/api/backups/download').status_code, 400)
        finally:
            self.hub.runner.release()
        result = []
        thread = threading.Thread(target=lambda: result.append(self.hub.backup()))
        thread.start(); thread.join(5)
        self.assertFalse(thread.is_alive(), 'backup lock leaked')
        self.assertEqual(len(result), 1)

    def test_sqlite_wal_is_included_and_staging_data_is_validated(self):
        with sqlite3.connect(self.hub.accounts.database) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('UPDATE users SET email=? WHERE id=?', ('renamed@example.com',self.user['id']))
            writer.commit()
            archive = self.hub.backup()
            target = Path(self.temp.name)/'wal-restored'
            restore_backup(archive, target)
            with sqlite3.connect(target/'results/accounts.sqlite3') as db:
                self.assertEqual(db.execute('SELECT email FROM users').fetchone()[0], 'renamed@example.com')

    def test_automatic_loop_creates_once_and_skips_a_recent_copy(self):
        class OneCycle:
            def __init__(self):self.done=False
            def is_set(self):return self.done
            def wait(self, timeout):self.done=True
            def set(self):self.done=True
        self.hub.backup_stop=OneCycle()
        self.hub.backup_loop()
        self.assertEqual(len(backup_files(self.root)), 1)
        self.hub.backup_stop=OneCycle()
        self.hub.backup_loop()
        self.assertEqual(len(backup_files(self.root)), 1)


if __name__ == '__main__':
    unittest.main()
