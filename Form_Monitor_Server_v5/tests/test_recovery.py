import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from accounts import Accounts
from backups import read_archive, restore_backup
from email_sender import send_email
from email_templates import password_reset_html
from server import create_app

class EmailLinks(HTMLParser):
    def __init__(self, body):
        super().__init__();self.links=[];self.tags=[]
        self.feed(body)
    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag=='a':self.links.append(dict(attrs)['href'])

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'live'
        self.env=patch.dict(os.environ,{'FM_STORAGE_ROOT':str(self.root),'FM_BACKUPS_ENABLED':'0',
            'FM_PUBLIC_URL':'https://beta.example','FM_SETUP_CODE':'owner-code-for-password-recovery-test',
            'FM_ENCRYPTION_KEY':secrets.token_hex(32),'FM_ALLOWED_EMAILS':'owner@example.com,other@example.com,invited@example.com',
            'FM_ALLOWED_FORM_HOSTS':'forms.example','GOOGLE_CLIENT_ID':'test.apps.googleusercontent.com',
            'GOOGLE_CLIENT_SECRET':'fake','RESEND_API_KEY':'fake-provider-key','ALERT_EMAIL':'wrong@example.com','FM_EMAIL_FROM':''})
        self.env.start();self.mail_patch=patch('recovery.send_email',return_value='test-id');self.mail=self.mail_patch.start()
        self.app=create_app();self.app.testing=True;self.hub=self.app.extensions['hub'];self.accounts=self.hub.accounts
        self.owner=self.app.test_client();self.other=self.app.test_client();self.anon=self.app.test_client()
        self.password='owner original password';self.new_password='owner replacement password'
        r=self.post(self.owner,'/api/register',dict(email='owner@example.com',password=self.password,setup_code=os.environ['FM_SETUP_CODE']))
        self.assertEqual(r.status_code,200);self.user=r.json['user']
        self.assertEqual(self.post(self.other,'/api/register',dict(email='other@example.com',password='other original password')).status_code,200)
    def tearDown(self):
        self.hub.close();self.mail_patch.stop();self.env.stop();self.temp.cleanup()
    def get(self,client,path):return client.get(path,base_url='https://beta.example')
    def post(self,client,path,value,headers=None):
        h={'Origin':'https://beta.example','X-CSRF-Token':self.get(client,'/api/session').json['csrf']};h.update(headers or {})
        return client.post(path,json=value,base_url='https://beta.example',headers=h)
    def link(self,email='owner@example.com'):
        r=self.post(self.anon,'/api/password-reset/request',{'email':email});self.assertEqual(r.status_code,202)
        self.hub.recovery.jobs.join()
        messages=[c for c in self.mail.call_args_list if c.args[0]=='[Sentleaf] Reset your password'];self.assertTrue(messages)
        return re.search(r'#token=([A-Za-z0-9_-]{43})',messages[-1].args[1]).group(1),messages[-1]
    def complete(self,token,**extra):
        value=dict(token=token,password=self.new_password,confirmation=self.new_password);value.update(extra)
        return self.post(self.anon,'/api/password-reset/complete',value)
    def check(self,token):return self.post(self.anon,'/api/password-reset/check',{'token':token}).json['valid']

    def test_end_to_end_revokes_sessions_preserves_workspace_and_requires_sign_in(self):
        second=self.app.test_client();self.assertEqual(self.post(second,'/api/login',dict(email='owner@example.com',password=self.password)).status_code,200)
        w=self.hub.workspace(self.user);connection='b'*32
        w.oauth.store.save(connection,dict(email='inbox@gmail.example',client_id='test.apps.googleusercontent.com',access_token='test-access',refresh_token='test-refresh',expires_at=time.time()+3600))
        with patch('server.validate_url'):ident=w.save(dict(name='Saved monitor',url='https://forms.example/contact/',interval=60,connection_id=connection))
        w.due[ident]=time.time()+7200;w.persist_schedule()
        token,message=self.link('  OWNER@EXAMPLE.COM  ')
        self.assertEqual(message.kwargs['recipient'],'owner@example.com');self.assertTrue(message.kwargs['sensitive'])
        self.assertIn('https://beta.example/reset-password#token=',message.args[1]);self.assertIn('15 minutes',message.args[1])
        link=f'https://beta.example/reset-password#token={token}'
        self.assertEqual(EmailLinks(message.kwargs['html']).links,[link,link])
        self.assertTrue(self.check(token));self.assertTrue(self.check(token))
        r=self.complete(token);self.assertEqual(r.status_code,200,r.json);self.assertIn('Max-Age=0',r.headers['Set-Cookie'])
        self.assertIsNone(self.get(self.anon,'/api/session').json['user'])
        for client in (self.owner,second):self.assertEqual(self.get(client,'/api/state').status_code,401)
        self.assertEqual(self.get(self.other,'/api/state').status_code,200)
        self.assertEqual(self.post(self.anon,'/api/login',dict(email='owner@example.com',password=self.password)).status_code,400)
        self.assertEqual(self.post(self.anon,'/api/login',dict(email='owner@example.com',password=self.new_password)).status_code,200)
        self.assertEqual(w.profiles[0]['id'],ident);self.assertEqual(w.oauth.store.load(connection)['refresh_token'],'test-refresh');self.assertIn(ident,w.due)
        self.hub.recovery.jobs.join();notices=[c for c in self.mail.call_args_list if c.args[0]=='[Sentleaf] Your password was changed']
        self.assertEqual(len(notices),1);self.assertNotIn(self.new_password,notices[0].args[1])
        self.assertEqual(EmailLinks(notices[0].kwargs['html']).links,['https://beta.example/login'])
        self.assertNotIn(self.new_password,notices[0].kwargs['html'])

    def test_unknown_unregistered_disabled_and_throttled_requests_have_same_response(self):
        r=self.post(self.anon,'/api/password-reset/request',{'email':'owner@example.com'})
        for email in ('absent@example.com','invited@example.com','owner@example.com'):
            other=self.post(self.anon,'/api/password-reset/request',{'email':email});self.assertEqual((other.status_code,other.json),(r.status_code,r.json))
        self.hub.recovery.jobs.join();self.assertEqual(self.mail.call_count,1);self.assertNotIn('token',json.dumps(r.json))
        self.assertEqual(self.get(self.owner,'/api/state').status_code,200)

    def test_rate_limits_are_separate_from_login(self):
        service=self.hub.recovery
        for minute in (0,61,122,183):
            with patch('recovery.time.monotonic',return_value=10000+minute):service.request('owner@example.com')
        service.jobs.join();self.assertEqual(self.mail.call_count,3)
        with patch('recovery.time.monotonic',return_value=20000):
            for i in range(25):service.request(f'unknown-{i}@example.com')
        service.jobs.join();self.assertEqual(len(service.requests),10)
        self.assertEqual(self.post(self.anon,'/api/login',dict(email='owner@example.com',password=self.password)).status_code,200)

    def test_request_does_not_wait_for_provider(self):
        entered=threading.Event();release=threading.Event()
        def slow(*a,**kw):entered.set();release.wait(4)
        self.mail.side_effect=slow
        try:
            r=self.post(self.anon,'/api/password-reset/request',{'email':'owner@example.com'})
            self.assertEqual(r.status_code,202);self.assertTrue(entered.wait(2));self.assertFalse(release.is_set())
            self.assertEqual(self.get(self.owner,'/api/state').status_code,200)
        finally:release.set();self.hub.recovery.jobs.join()

    def test_expired_malformed_and_used_tokens_cannot_reset(self):
        token,_=self.link()
        for bad in ('',None,'a'*43,token[:-1]+'!',{'token':token}):
            self.assertFalse(self.check(bad));self.assertEqual(self.complete(bad).status_code,400)
        with self.accounts.connect() as db:db.execute('UPDATE password_resets SET expires=?',(time.time()-1,))
        self.assertFalse(self.check(token));self.assertEqual(self.complete(token).status_code,400)
        fresh=self.accounts.issue_password_reset('owner@example.com');self.assertEqual(self.complete(fresh).status_code,200)
        self.assertFalse(self.check(fresh));self.assertEqual(self.complete(fresh).status_code,400)

    def test_password_policy_and_confirmation_preserve_unused_link(self):
        token,_=self.link()
        for password,confirmation in [('short','short'),('a'*257,'a'*257),(self.new_password,'different'),(None,None)]:
            self.assertEqual(self.complete(token,password=password,confirmation=confirmation).status_code,400);self.assertTrue(self.check(token))
        self.assertEqual(self.complete(token).status_code,200)

    def test_only_hashes_stored_and_all_outstanding_links_invalidated(self):
        first,_=self.link();second=self.accounts.issue_password_reset('owner@example.com')
        with self.accounts.connect() as db:hashes=[r['token_hash'] for r in db.execute('SELECT token_hash FROM password_resets')]
        self.assertIn(hashlib.sha256(first.encode()).hexdigest(),hashes)
        for token in (first,second):self.assertNotIn(token.encode(),self.accounts.database.read_bytes())
        self.assertEqual(self.complete(first).status_code,200);self.assertFalse(self.check(second))

    def test_persisted_link_binds_to_allowlist_and_current_password(self):
        token,_=self.link();fresh=Accounts(self.root)
        self.assertTrue(fresh.check_password_reset(token,{'owner@example.com'}));self.assertFalse(fresh.check_password_reset(token,{'other@example.com'}))
        with self.accounts.connect() as db:db.execute('UPDATE users SET digest=? WHERE id=?',(b'x'*32,self.user['id']))
        self.assertFalse(fresh.check_password_reset(token,{'owner@example.com'}))

    def test_existing_account_database_migrates_without_changing_login(self):
        with self.accounts.connect() as db:db.execute('DROP TABLE password_resets')
        upgraded=Accounts(self.root)
        _,user=upgraded.authenticate(dict(email='owner@example.com',password=self.password))
        self.assertEqual(user['id'],self.user['id'])
        token=upgraded.issue_password_reset('owner@example.com')
        self.assertTrue(upgraded.check_password_reset(token,{'owner@example.com'}))

    def test_token_check_limit_is_independent_from_login(self):
        for _ in range(60):self.assertFalse(self.accounts.check_password_reset('bad',{'owner@example.com'}))
        with self.assertRaisesRegex(ValueError,'Too many attempts'):
            self.accounts.check_password_reset('bad',{'owner@example.com'})
        self.assertEqual(self.post(self.anon,'/api/login',dict(email='owner@example.com',password=self.password)).status_code,200)

    def test_concurrent_redemption_has_one_winner(self):
        token,_=self.link();other=Accounts(self.root);gate=threading.Barrier(2);results=[]
        def redeem(accounts):
            gate.wait()
            try:accounts.complete_password_reset(token,self.new_password,self.new_password,{'owner@example.com'});results.append('ok')
            except ValueError:results.append('rejected')
        threads=[threading.Thread(target=redeem,args=(a,)) for a in (self.accounts,other)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5)
        self.assertCountEqual(results,['ok','rejected'])

    def test_failed_delivery_discards_only_new_link_and_logs_no_secrets(self):
        existing=self.accounts.issue_password_reset('owner@example.com');self.mail.side_effect=RuntimeError('secret-token owner@example.com')
        with self.assertLogs('recovery',level='ERROR') as logs:
            r=self.post(self.anon,'/api/password-reset/request',{'email':'owner@example.com'});self.hub.recovery.jobs.join()
        self.assertEqual(r.status_code,202);self.assertNotIn('secret-token',''.join(logs.output));self.assertNotIn('owner@example.com',''.join(logs.output))
        self.assertTrue(self.check(existing))
        with self.accounts.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM password_resets').fetchone()[0],1)
        self.assertEqual(self.get(self.owner,'/api/state').status_code,200)

    def test_backup_excludes_recovery_links_and_keeps_accounts(self):
        token,_=self.link();archive=self.hub.backup();manifest,files=read_archive(archive)
        snapshot=Path(self.temp.name)/'snapshot.sqlite3';snapshot.write_bytes(files['results/accounts.sqlite3'])
        with sqlite3.connect(snapshot) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM password_resets').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],2)
        destination=Path(self.temp.name)/'restore';restore_backup(archive,destination);restored=Accounts(destination)
        self.assertFalse(restored.check_password_reset(token,{'owner@example.com'}));self.assertTrue(self.check(token))
        _,user=restored.authenticate(dict(email='owner@example.com',password=self.password));self.assertEqual(user['id'],self.user['id'])

    def test_csrf_host_origin_protection_and_canonical_url(self):
        for path in ('request','check','complete'):
            for headers in ({'X-CSRF-Token':'bad'},{'Origin':'https://evil.example'}):
                self.assertEqual(self.post(self.anon,'/api/password-reset/'+path,{},headers).status_code,403)
        self.assertEqual(self.anon.post('/api/password-reset/request',base_url='https://evil.example',json={}).status_code,400)
        self.post(self.anon,'/api/password-reset/request',{'email':'owner@example.com'},{'X-Forwarded-Host':'evil.example'})
        self.hub.recovery.jobs.join();body=self.mail.call_args.args[1]
        self.assertIn('https://beta.example/reset-password#token=',body);self.assertNotIn('evil.example',body)
        for path in ('/forgot-password','/reset-password','/recovery.js'):
            r=self.get(self.anon,path);self.assertEqual(r.status_code,200);self.assertEqual(r.headers['Referrer-Policy'],'no-referrer');self.assertEqual(r.headers['Cache-Control'],'no-store');r.close()
        self.assertEqual(self.get(self.anon,'/api/state').status_code,401);self.assertEqual(self.get(self.anon,'/results/accounts.sqlite3').status_code,404)

    def test_unconfigured_sender_and_bad_payloads(self):
        with patch.dict(os.environ,{'RESEND_API_KEY':''}):self.assertEqual(self.post(self.anon,'/api/password-reset/request',{'email':'owner@example.com'}).status_code,503)
        for email in ('not-an-email',['owner@example.com'],None):self.assertEqual(self.post(self.anon,'/api/password-reset/request',{'email':email}).status_code,400)
        self.assertEqual(self.post(self.anon,'/api/password-reset/request',{'email':'a'*9000}).status_code,413);self.mail.assert_not_called()

    def test_sender_recipient_and_sensitive_error_redaction(self):
        with patch('email_sender.urlopen',return_value=io.BytesIO(b'{"id":"message-id"}')) as network,patch('builtins.print'):
            send_email('Account reset','secret-reset-url',recipient='owner@example.com',sensitive=True)
        payload=json.loads(network.call_args.args[0].data)
        self.assertEqual(payload['to'],['owner@example.com']);self.assertEqual(payload['from'],'Sentleaf <onboarding@resend.dev>')
        self.assertNotIn('html',payload)  # Existing plain-text alerts keep working.
        html=password_reset_html('https://beta.example/reset-password#token=secret-reset-url')
        with patch('email_sender.urlopen',return_value=io.BytesIO(b'{"id":"message-id"}')) as network,patch('builtins.print'):
            send_email('Account reset','secret-reset-url',recipient='owner@example.com',sensitive=True,html=html)
        payload=json.loads(network.call_args.args[0].data)
        self.assertEqual(payload['html'],html);self.assertEqual(payload['text'],'secret-reset-url')
        failure=HTTPError('https://api.resend.com/emails',403,'Rejected',{},io.BytesIO(b'secret-reset-url fake-provider-key'))
        with patch('email_sender.urlopen',side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:send_email('Account reset','secret-reset-url',recipient='owner@example.com',sensitive=True,html=html)
        self.assertNotIn('secret-reset-url',str(caught.exception));self.assertNotIn('fake-provider-key',str(caught.exception))

    def test_email_links_are_escaped_and_need_no_remote_assets(self):
        link='https://beta.example/reset-password?x="<img src=x>&other=1#token=example'
        html=password_reset_html(link);parsed=EmailLinks(html)
        self.assertEqual(parsed.links,[link,link])
        self.assertFalse(set(parsed.tags)&{'img','script','iframe','link','form'})
        self.assertNotIn('<img src=x>',html)

if __name__=='__main__':unittest.main()
