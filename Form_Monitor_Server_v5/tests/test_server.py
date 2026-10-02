import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import create_app, RunnerBusy
from google_mail import SCOPE, Store
from server_network import validate_url
from transfer import export_data, import_data

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'FM_TRIALS_ENABLED':'0','FM_BACKUPS_ENABLED':'0','FM_INQUIRY_EMAILS_ENABLED':'0','FM_PUBLIC_URL':'https://beta.example','FM_STORAGE_ROOT':str(self.root),'FM_SETUP_CODE':'test-owner-code-that-is-long-enough','FM_ENCRYPTION_KEY':secrets.token_hex(32),'FM_ALLOWED_EMAILS':'owner@example.com,second@example.com','FM_ALLOWED_FORM_HOSTS':'forms.example','GOOGLE_CLIENT_ID':'test.apps.googleusercontent.com','GOOGLE_CLIENT_SECRET':'fake','RESEND_API_KEY':'fake'},clear=False);self.env.start()
        self.render_env=patch.dict(os.environ,{'RENDER_EXTERNAL_HOSTNAME':'legacy.onrender.com'});self.render_env.start()
        self.addCleanup(self.render_env.stop)
        self.app=create_app();self.app.testing=True;self.hub=self.app.extensions['hub']
        self.a=self.app.test_client();self.b=self.app.test_client()
        self.register(self.a,'owner@example.com',True);self.register(self.b,'second@example.com')
    def tearDown(self):
        self.hub.close();self.env.stop();self.temp.cleanup()
    def get(self,c,path):return c.get(path,base_url='https://beta.example')
    def post(self,c,path,value,csrf=None):
        if csrf is None:csrf=self.get(c,'/api/session').json['csrf']
        return c.post(path,json=value,base_url='https://beta.example',headers={'Origin':'https://beta.example','X-CSRF-Token':csrf})
    def register(self,c,email,owner=False):
        r=self.post(c,'/api/register',dict(email=email,password='test password long enough',setup_code=os.environ['FM_SETUP_CODE'] if owner else ''))
        self.assertEqual(r.status_code,200,r.json)
        self.assertIn('Secure',r.headers['Set-Cookie']);self.assertIn('HttpOnly',r.headers['Set-Cookie']);self.assertIn('SameSite=Lax',r.headers['Set-Cookie'])
    def workspace(self,c):return self.hub.workspace(self.get(c,'/api/session').json['user'])
    def connect(self,c,email):
        r=self.post(c,'/api/google/start',{});self.assertEqual(r.status_code,200,r.json)
        query=parse_qs(urlsplit(r.json['url']).query);self.assertEqual(query['redirect_uri'],['https://beta.example/oauth/google/callback'])
        response=[{'access_token':'test-access','refresh_token':'test-refresh','scope':SCOPE,'expires_in':3600},{'emailAddress':email}]
        with patch('server_google.request_json',side_effect=response):
            r=self.get(c,'/oauth/google/callback?state='+query['state'][0]+'&code=test-code');self.assertEqual(r.status_code,303)
        return self.get(c,'/api/state').json['google']['accounts'][0]['id']
    def profile(self,c,connection,url='https://forms.example/contact/'):
        data=dict(name='A form',url=url,selector='form.wpcf7-form',fields=dict(name='your-name',email='your-email',subject='your-subject',message='your-message'),interval=60,connection_id=connection)
        with patch.object(self.hub.sites,'require'):r=self.post(c,'/api/profiles',data)
        self.assertEqual(r.status_code,200,r.json);return r.json['id']
    def test_accounts_and_auth(self):
        c=self.app.test_client();self.assertEqual(self.get(c,'/api/state').status_code,401)
        self.assertEqual(self.post(c,'/api/register',dict(email='uninvited@example.com',password='long enough password')).status_code,400)
        self.assertEqual(self.post(self.a,'/api/logout',{},csrf='bad').status_code,403)
        self.assertEqual(self.post(self.a,'/api/logout',{}).status_code,200)
        self.assertEqual(self.get(self.a,'/api/state').status_code,401)
        self.assertEqual(self.post(self.a,'/api/login',dict(email='owner@example.com',password='wrong')).status_code,400)
        self.assertEqual(self.post(self.a,'/api/login',dict(email='owner@example.com',password='test password long enough')).status_code,200)
        self.assertEqual(self.a.get('/api/state',base_url='https://evil.example').status_code,400)
        self.assertEqual(self.get(self.a,'/google_client.json').status_code,404)
        self.assertEqual(self.get(self.a,'/results/accounts.sqlite3').status_code,404)

    def test_billing_public_catalog_and_protected_routes(self):
        c=self.app.test_client()
        catalog=self.get(c,'/api/plans')
        self.assertEqual(catalog.status_code,200)
        self.assertEqual(catalog.json['plans'][0]['amount'],500)
        self.assertFalse(catalog.json['checkout_ready'])
        self.assertNotIn('secret',catalog.get_data(as_text=True).lower())
        for path in ('/api/billing/checkout','/api/billing/portal','/api/billing/trial','/api/billing/refresh'):
            self.assertEqual(self.post(c,path,{}).status_code,401)
            self.assertEqual(self.post(self.b,path,{},csrf='wrong').status_code,403)
        self.assertEqual(self.post(self.b,'/api/billing/checkout',{'plan':'solo'}).status_code,503)
        # A callback is session-independent but never signature-independent.
        with patch.dict(os.environ,{'FM_BILLING_ENABLED':'1','STRIPE_SECRET_KEY':'sk_test_fixture',
              'STRIPE_WEBHOOK_SECRET':'whsec_fixture','STRIPE_PRICE_SOLO':'price_solo','STRIPE_PRICE_STUDIO':'price_studio'}):
            response=c.post('/api/billing/webhook',data=b'{}',base_url='https://beta.example')
            self.assertEqual(response.status_code,400)
            self.assertIn('signature',response.json['error'])
            response=c.post('/api/billing/webhook',data=b'x'*262145,base_url='https://beta.example')
            self.assertEqual(response.status_code,413)
            self.assertEqual(self.post(self.b,'/api/billing/trial',{}).status_code,400)
            self.connect(self.b,'second@gmail.example')
            self.assertEqual(self.post(self.b,'/api/billing/trial',{}).status_code,200)
            self.assertEqual(self.post(self.b,'/api/billing/trial',{}).status_code,400)

    def test_billing_assets_and_off_state_preserve_workspace(self):
        for path in ('/plans.js','/plans.css','/refresh.css'):
            response=self.get(self.a,path)
            self.assertEqual(response.status_code,200)
            response.close()
        self.assertEqual(self.get(self.b,'/api/state').json['billing']['plan'],'beta')
        self.assertEqual(self.get(self.a,'/api/state').json['billing']['plan'],'owner')

    def test_public_requests_require_consent_and_csrf_and_are_owner_only(self):
        visitor=self.app.test_client()
        value=dict(email='visitor@example.com',website='https://example.com/contact',kind='access',message='One form',consent=True)
        self.assertEqual(self.post(visitor,'/api/inquiries',value,csrf='bad').status_code,403)
        self.assertEqual(self.post(visitor,'/api/inquiries',dict(value,consent=False)).status_code,400)
        self.assertEqual(self.post(visitor,'/api/inquiries',value).status_code,202)
        self.assertEqual(self.post(visitor,'/api/inquiries',value).status_code,202)
        requests=self.get(self.a,'/api/state').json['inquiries']
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0]['email'],'visitor@example.com')
        self.assertEqual(requests[0]['email_status'],'pending')
        self.assertEqual(self.get(self.a,'/api/state').json['inquiry_notifications']['recipient'],'owner@example.com')
        self.assertNotIn('inquiry_notifications',self.get(self.b,'/api/state').json)
        self.assertNotIn('inquiries',self.get(self.b,'/api/state').json)
        self.assertEqual(self.get(visitor,'/api/state').status_code,401)
        self.assertEqual(self.post(self.b,'/api/inquiries/delete',{'id':requests[0]['id']}).status_code,403)
        self.assertEqual(self.post(self.a,'/api/inquiries/delete',{'id':requests[0]['id']}).status_code,200)
        self.assertEqual(self.get(self.a,'/api/state').json['inquiries'],[])

    def test_inquiries_honeypot_retention_and_url_validation(self):
        value=dict(email='visitor@example.com',website='',kind='data',message='Please delete my data',consent=True)
        self.hub.inquiries.submit(dict(value,company_fax='bot'))
        self.assertEqual(self.hub.inquiries.listing(),[])
        for url in ('javascript:alert(1)','https://user:password@example.com','https://example.com/?token=secret'):
            with self.assertRaises(ValueError):self.hub.inquiries.submit(dict(value,website=url))
        self.hub.inquiries.submit(value)
        with self.hub.accounts.connect() as db:
            db.execute('UPDATE inquiries SET created=?',(time.time()-91*86400,))
        self.assertEqual(self.hub.inquiries.listing(),[])
    def test_gmail_ownership_and_encryption(self):
        ident=self.connect(self.a,'owner@gmail.example')
        aw=self.workspace(self.a);self.assertEqual(aw.oauth.store.load(ident)['email'],'owner@gmail.example')
        self.assertNotIn(b'test-refresh',aw.oauth.store.path(ident).read_bytes())
        self.assertEqual(self.get(self.b,'/api/state').json['google']['accounts'],[])
        self.post(self.b,'/api/google/disconnect',dict(id=ident));self.assertTrue(aw.oauth.store.path(ident).exists())
        with patch.dict(os.environ,{'FM_ENCRYPTION_KEY':secrets.token_hex(32)}):
            with self.assertRaises(ValueError):aw.oauth.store.load(ident)
    def test_old_render_pages_redirect_to_canonical_domain(self):
        for path in ('/','/workspace','/login','/forgot-password','/reset-password'):
            for method in ('GET','HEAD'):
                with self.subTest(path=path,method=method):
                    r=self.a.open(path+'?next=https://evil.example&code=do-not-forward',method=method,base_url='https://legacy.onrender.com')
                    self.assertEqual(r.status_code,302)
                    self.assertEqual(r.headers['Location'],'https://beta.example'+path)
                    self.assertNotIn('Set-Cookie',r.headers)
                    self.assertEqual(r.headers['Cache-Control'],'no-store')
    def test_old_render_domain_cannot_access_accounts_or_accept_mutations(self):
        for path in ('/api/session','/api/state','/oauth/google/callback?code=secret','/evidence/private/page.png'):
            with self.subTest(path=path):
                r=self.a.get(path,base_url='https://legacy.onrender.com')
                self.assertEqual(r.status_code,400)
                self.assertNotIn('Location',r.headers)
        for path in ('/','/login','/api/login','/api/password-reset/complete'):
            with self.subTest(post=path):
                r=self.a.post(path,base_url='https://legacy.onrender.com',json={})
                self.assertEqual(r.status_code,400)
                self.assertNotIn('Location',r.headers)
        for host in ('evil.example','legacy.onrender.com.evil.example'):
            self.assertEqual(self.a.get('/login',base_url='https://'+host).status_code,400)
        csrf=self.get(self.a,'/api/session').json['csrf']
        r=self.a.post('/api/logout',base_url='https://beta.example',json={},headers={'Origin':'https://legacy.onrender.com','X-CSRF-Token':csrf})
        self.assertEqual(r.status_code,403)
        self.assertIsNotNone(self.get(self.a,'/api/session').json['user'])
    def test_oauth_wrong_account_and_replay(self):
        query=parse_qs(urlsplit(self.post(self.a,'/api/google/start',{}).json['url']).query);state=query['state'][0]
        with patch('server_google.request_json') as network:
            self.get(self.b,'/oauth/google/callback?state='+state+'&code=wrong-account');network.assert_not_called()
        self.assertTrue(self.workspace(self.a).oauth.pending)
        with patch('server_google.request_json',side_effect=[dict(access_token='a',refresh_token='r',scope=SCOPE),dict(emailAddress='owner@gmail.example')]):
            self.get(self.a,'/oauth/google/callback?state='+state+'&code=valid')
        with patch('server_google.request_json') as network:
            self.get(self.a,'/oauth/google/callback?state='+state+'&code=replay');network.assert_not_called()
    def test_form_evidence_and_schedule_isolation(self):
        connection=self.connect(self.a,'owner@gmail.example');ident=self.profile(self.a,connection);aw=self.workspace(self.a)
        run='MONITOR-'+'b'*32;folder=aw.root/'results/monitors'/ident/'reports'/run;folder.mkdir(parents=True);(folder/'page.png').write_bytes(b'owner screenshot')
        from datetime import datetime, timezone
        (folder/'report.json').write_text(json.dumps(dict(started_utc=datetime.now(timezone.utc).isoformat())))
        response=self.get(self.a,f'/evidence/{ident}/{run}/page.png');self.assertEqual(response.data,b'owner screenshot');response.close()
        self.assertEqual(self.get(self.b,f'/evidence/{ident}/{run}/page.png').status_code,404)
        self.assertEqual(self.post(self.b,'/api/delete',dict(id=ident)).status_code,400)
        self.assertEqual(self.post(self.b,'/api/run',dict(id=ident)).status_code,400)
        self.assertEqual(self.get(self.b,'/api/state').json['profiles'],[])
        aw.due[ident]=time.time()+600;aw.persist_schedule();self.hub.close()
        fresh=create_app();hub=fresh.extensions['hub']
        try:
            restored=hub.workspaces[aw.user['id']];self.assertIn(ident,restored.due)
            self.assertEqual(restored.profiles[0]['connection_id'],connection)
            self.assertTrue(hub.accounts.setup_code=='')
        finally:hub.close()
    def test_global_runner_gate(self):
        aw=self.workspace(self.a);self.hub.runner.acquire()
        try:
            with self.assertRaises(RunnerBusy):aw.run('a'*32)
        finally:self.hub.runner.release()
    def test_public_network_policy(self):
        with self.assertRaises(ValueError):validate_url('http://forms.example/')
        with self.assertRaises(ValueError):validate_url('https://127.0.0.1/')
        with patch('server_network.socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',443))]):
            with self.assertRaises(ValueError):validate_url('https://forms.example/')
        with patch('server_network.socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]):self.assertEqual(validate_url('https://forms.example/'),'https://forms.example/')

    def test_live_link_run_is_owner_and_host_scoped(self):
        target='https://test-lab.localsite.io/contact/'
        env={'FM_ALLOWED_FORM_HOSTS':'forms.example,test-lab.localsite.io,other-lab.localsite.io',
             'FM_LIVE_LINK_HOST':'test-lab.localsite.io','LIVE_LINK_USER':'test-user',
             'LIVE_LINK_PASSWORD':'test-tunnel-secret','GMAIL_ADDRESS':'legacy@example.com',
             'GMAIL_APP_PASSWORD':'legacy-secret'}
        with patch.dict(os.environ,env):
            connection=self.connect(self.a,'owner@gmail.example')
            ident=self.profile(self.a,connection,target)
            aw=self.workspace(self.a)
            second_connection=self.connect(self.b,'second@gmail.example')
            second_id=self.profile(self.b,second_connection,target)
            bw=self.workspace(self.b)
            state=self.get(self.a,'/api/state')
            self.assertNotIn('test-tunnel-secret',state.get_data(as_text=True))
            self.assertFalse(bw.connections()['live_link_ready'])
            with patch.object(self.hub.sites,'require'), patch('workspace.subprocess.Popen') as popen:
                child=popen.return_value.__enter__.return_value
                child.stdout=['test-tunnel-secret\n'];child.wait.return_value=0
                response=self.post(self.a,'/api/run',dict(id=ident))
                self.assertEqual(response.status_code,200,response.json)
                aw.worker.join(timeout=5)
                self.assertFalse(aw.worker.is_alive())
                run_env=popen.call_args.kwargs['env']
                self.assertEqual(run_env['LIVE_LINK_USER'],'test-user')
                self.assertEqual(run_env['LIVE_LINK_PASSWORD'],'test-tunnel-secret')
                self.assertNotIn('GMAIL_APP_PASSWORD',run_env)
                self.assertNotIn('GMAIL_ADDRESS',run_env)
                self.assertNotIn('test-tunnel-secret','\n'.join(aw.logs))
                response=self.post(self.b,'/api/run',dict(id=second_id))
                self.assertEqual(response.status_code,400,response.json)
                self.assertEqual(popen.call_count,1)
            for url in ('https://forms.example/contact/','https://other-lab.localsite.io/contact/'):
                p=dict(aw.profile(ident),url=url)
                run_env=aw.environment_for_run(p,self.root/'unused.json')
                self.assertNotIn('LIVE_LINK_USER',run_env)
                self.assertNotIn('LIVE_LINK_PASSWORD',run_env)
            second_env=bw.environment_for_run(bw.profile(second_id),self.root/'unused.json')
            self.assertNotIn('LIVE_LINK_PASSWORD',second_env)
            with patch.object(self.hub.sites,'require'):
                with self.assertRaisesRegex(ValueError,'not configured'):
                    aw.ready(dict(aw.profile(ident),url='https://other-lab.localsite.io/contact/'))

    def test_live_link_requires_explicit_complete_configuration(self):
        aw=self.workspace(self.a)
        base={'FM_ALLOWED_FORM_HOSTS':'test-lab.localsite.io','FM_LIVE_LINK_HOST':'test-lab.localsite.io',
              'LIVE_LINK_USER':'test-user','LIVE_LINK_PASSWORD':'test-tunnel-secret'}
        with patch.dict(os.environ,base):
            for override in ({'FM_LIVE_LINK_HOST':''},{'FM_LIVE_LINK_HOST':'*.localsite.io'},
                             {'FM_ALLOWED_FORM_HOSTS':'forms.example'},{'LIVE_LINK_USER':''},
                             {'LIVE_LINK_PASSWORD':''}):
                with self.subTest(override=override), patch.dict(os.environ,override):
                    self.assertFalse(aw.connections()['live_link_ready'])
                    run_env=aw.environment_for_run({'url':'https://test-lab.localsite.io/'},self.root/'unused.json')
                    self.assertNotIn('LIVE_LINK_PASSWORD',run_env)
    def test_transfer_excludes_credentials(self):
        connection=self.connect(self.a,'owner@gmail.example');ident=self.profile(self.a,connection)
        archive=self.root/'migration.zip';export_data(self.root,'owner@example.com',archive)
        import_data(self.root,'second@example.com',archive)
        path=self.hub.accounts.data_root(self.workspace(self.b).user)/'results/workspace/profiles.json'
        p=json.loads(path.read_text())[0];self.assertEqual(p['connection_id'],'');self.assertEqual(p['revision'],2)
        import zipfile
        with zipfile.ZipFile(archive) as z:self.assertEqual(z.namelist(),['workspace/profiles.json'])

if __name__=='__main__':unittest.main()
