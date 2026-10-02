"""Single-instance HTTPS application. Run with the supplied Gunicorn config."""
import atexit
from contextlib import ExitStack
import io
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, redirect, send_file, g
from accounts import Accounts
from workspace import Workspace, read_json, write_json
from server_google import WebOAuth
from server_network import allowed_hosts, validate_url
from site_verification import SiteVerification
from backups import create_backup, backup_files
from recovery import PasswordRecovery, REQUEST_MESSAGE
from billing import Billing, BillingUnavailable, BillingAccessError, DailyLimit
from inquiries import Inquiries
from reporting import weekly_summary, history_csv, timestamp

ROOT=Path(__file__).resolve().parent
class RunnerBusy(ValueError):pass

class ServerWorkspace(Workspace):
    def __init__(self, hub, user):
        self.hub=hub
        super().__init__(hub.accounts.data_root(user),dict(user,owner=False),ROOT)
        self.user=user
        self.oauth=WebOAuth(self.root,hub.origin)
        self.schedule_file=self.data/'schedule.json'
        saved=read_json(self.schedule_file,{})
        self.due={p['id']:max(time.time()+5,float(saved[p['id']])) for p in self.profiles if not p.get('deleted') and p['id'] in saved}

    def connections(self):
        return dict(owner=False,gmail='',gmail_ready=False,live_link_ready=bool(self.live_link_host()),
                    alert_email=self.user['email'],resend_ready=bool(os.environ.get('RESEND_API_KEY','')))

    def live_link_host(self):
        """A temporary test tunnel belongs only to the server's actual owner."""
        host=os.environ.get('FM_LIVE_LINK_HOST','').strip().lower()
        if not self.user.get('owner'):
            return ''
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.localsite\.io',host):
            return ''
        if host not in allowed_hosts():
            return ''
        if not os.environ.get('LIVE_LINK_USER','').strip() or not os.environ.get('LIVE_LINK_PASSWORD',''):
            return ''
        return host

    def environment_for_run(self,p,runtime):
        env=super().environment_for_run(p,runtime)
        env['FM_VERIFIED_FORM_HOST']=urlsplit(p['url']).hostname
        # Never inherit global mailbox credentials or tunnel credentials by default.
        for key in ('GMAIL_ADDRESS','GMAIL_APP_PASSWORD','LIVE_LINK_USER','LIVE_LINK_PASSWORD','STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET'):
            env.pop(key,None)
        address=urlsplit(p['url'])
        if (address.scheme=='https' and address.port in (None,443)
                and not address.username and not address.password
                and address.hostname==self.live_link_host()):
            env['LIVE_LINK_USER']=os.environ['LIVE_LINK_USER'].strip()
            env['LIVE_LINK_PASSWORD']=os.environ['LIVE_LINK_PASSWORD']
        return env

    def save(self,data):
        if data.get('group') and not self.hub.billing.state(self.user)['features']['groups']:
            previous = self.profile(data['id']).get('group','') if data.get('id') else ''
            if data['group'] != previous:raise ValueError('Client groups are included in Studio.')
        self.hub.billing.check(self.user,self.profiles,data=data)
        self.hub.sites.require(self.user,str(data.get('url','')))
        if not data.get('connection_id'):raise ValueError('Choose your connected Gmail account.')
        ident=super().save(data);self.persist_schedule();return ident

    def ready(self,p):
        self.hub.billing.check(self.user,self.profiles,profile=p)
        self.hub.sites.require(self.user,p['url'])
        if not p.get('connection_id'):raise ValueError('Connect Gmail and choose it in Edit settings.')
        host=urlsplit(p['url']).hostname
        if host.endswith('.localsite.io') and host!=self.live_link_host():
            raise ValueError('This Live Link is not configured for this account. The server owner must configure its exact hostname and credentials.')
        super().ready(p)

    def persist_schedule(self):
        with self.lock:write_json(self.schedule_file,self.due)

    def schedule(self,ident,enabled):
        super().schedule(ident,enabled);self.persist_schedule()

    def delete(self,ident):
        super().delete(ident);self.persist_schedule()

    def disconnect_google(self,ident):
        super().disconnect_google(ident);self.persist_schedule()

    def run(self,ident):
        if not self.hub.runner.acquire(blocking=False):raise RunnerBusy('A check is running on the server. Try again shortly.')
        try:
            with self.lock:
                if self.stop or self.active or self.oauth.pending:raise ValueError('Wait for the current operation to finish.')
                self.ready(self.profile(ident))
                self.hub.billing.reserve_run(self.user)
                super().run(ident)
        except BaseException:
            self.hub.runner.release();raise

    def execute(self,p,runtime):
        try:super().execute(p,runtime)
        finally:
            self.persist_schedule();self.hub.runner.release()

    def scheduler(self):
        while not self.stop:
            with self.lock:
                if not self.active and not self.oauth.pending and self.due:
                    ident=min(self.due,key=self.due.get)
                    if self.due[ident]<=time.time():
                        try:self.run(ident)
                        except RunnerBusy:pass
                        except DailyLimit as error:
                            self.last_errors[ident]=str(error)
                            self.due[ident]=(int(time.time()//86400)+1)*86400+5
                            self.persist_schedule()
                        except BillingAccessError as error:
                            self.last_errors[ident]=str(error)
                            self.due[ident]=time.time()+600
                            self.persist_schedule()
                        except ValueError as error:
                            self.last_errors[ident]=str(error);self.due.pop(ident,None);self.persist_schedule()
            self.wake.wait(1);self.wake.clear()

class ServerHub:
    def __init__(self,root,origin):
        self.root=root;self.origin=origin;self.accounts=Accounts(root)
        self.sites=SiteVerification(self.accounts)
        self.billing=Billing(self.accounts,origin)
        self.inquiries=Inquiries(self.accounts,origin)
        if self.accounts.setup_required():self.accounts.setup_code=os.environ['FM_SETUP_CODE']
        self.workspaces={};self.lock=threading.RLock();self.runner=threading.Lock();self.stopping=False
        self.mutations=threading.RLock()
        self.backup_stop=threading.Event();self.backup_worker=None;self.backup_error=''
        self.recovery=None
        # Schedules are restored for all accounts, without waiting for somebody to log in.
        with self.accounts.connect() as db:users=db.execute('SELECT id,email,owner FROM users').fetchall()
        for row in users:self.workspace(dict(id=row['id'],email=row['email'],owner=bool(row['owner'])))

    def backup(self):
        # Exclude HTTP mutations, workspace creation and scheduled/child checks.
        # Runner acquisition must not block while a scheduler owns a workspace lock.
        with self.mutations, self.lock, ExitStack() as locks:
            if self.stopping:raise RunnerBusy('Server is restarting. Try again shortly.')
            if not self.runner.acquire(blocking=False):
                raise RunnerBusy('A check is running. Wait for it to finish before downloading a backup.')
            locks.callback(self.runner.release)
            locks.enter_context(self.accounts.lock)
            for w in self.workspaces.values():
                locks.enter_context(w.lock)
                w.persist_schedule()
            result=create_backup(self.root)
            self.backup_error=''
            return result

    def backup_status(self):
        files=backup_files(self.root)
        latest=files[0] if files else None
        return dict(latest=latest.stat().st_mtime if latest else None,count=len(files),
                    automatic=os.environ.get('FM_BACKUPS_ENABLED','1')!='0',error=self.backup_error)

    def backup_loop(self):
        while not self.backup_stop.is_set():
            try:
                self.inquiries.prune()
                latest=self.backup_status()['latest']
                if not self.accounts.setup_required() and (latest is None or time.time()-latest>=86400):
                    self.backup()
            except RunnerBusy:
                pass  # Retry when the active check has finished.
            except Exception as error:
                self.backup_error='Automatic backup failed. Download a copy or check the server logs.'
                logging.getLogger(__name__).error('Automatic backup failed (%s).',type(error).__name__)
            self.backup_stop.wait(60)

    def start_backups(self):
        if os.environ.get('FM_BACKUPS_ENABLED','1')!='0':
            self.backup_worker=threading.Thread(target=self.backup_loop,daemon=True)
            self.backup_worker.start()

    def workspace(self,user):
        with self.lock:
            if self.stopping:raise ValueError('Server is restarting. Try again shortly.')
            if user['id'] not in self.workspaces:
                app=ServerWorkspace(self,user);self.workspaces[user['id']]=app
                threading.Thread(target=app.scheduler,daemon=True).start()
            return self.workspaces[user['id']]

    def close(self):
        self.inquiries.close()
        self.billing.close()
        if self.recovery:self.recovery.close()
        self.backup_stop.set()
        if self.backup_worker:self.backup_worker.join(timeout=10)
        with self.lock:
            self.stopping=True
            apps=list(self.workspaces.values())
        for app in apps:
            with app.lock:app.stop=True;app.wake.set();app.oauth.cancel.set();app.persist_schedule()
        for app in apps:
            if app.worker:app.worker.join(timeout=520)

def create_app():
    origin=(os.environ.get('FM_PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL','')).rstrip('/')
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.port not in (None,443):
        raise RuntimeError('Set FM_PUBLIC_URL to the canonical HTTPS origin.')
    render_host=os.environ.get('RENDER_EXTERNAL_HOSTNAME','').strip().lower()
    redirect_pages={'/','/workspace','/login','/forgot-password','/reset-password'}
    for key in ('FM_ENCRYPTION_KEY','FM_SETUP_CODE','FM_ALLOWED_EMAILS','FM_STORAGE_ROOT'):
        if not os.environ.get(key):raise RuntimeError('Missing server setting: '+key)
    if not re.fullmatch('[a-fA-F0-9]{64}',os.environ['FM_ENCRYPTION_KEY']):raise RuntimeError('FM_ENCRYPTION_KEY must be 64 hex characters.')
    if len(os.environ['FM_SETUP_CODE'])<24:raise RuntimeError('FM_SETUP_CODE must be at least 24 random characters.')
    allowed={x.strip().lower() for x in os.environ['FM_ALLOWED_EMAILS'].split(',') if x.strip()}
    public_registration=os.environ.get('FM_PUBLIC_REGISTRATION')=='1'
    if public_registration:allowed=None
    hub=ServerHub(Path(os.environ['FM_STORAGE_ROOT']),origin)
    hub.recovery=PasswordRecovery(hub.accounts,origin,allowed)
    app=Flask(__name__,static_folder=None);app.config['MAX_CONTENT_LENGTH']=8192;app.extensions['hub']=hub
    atexit.register(hub.close)
    cookie_name='__Host-fm_session'
    def current_token():return request.cookies.get(cookie_name,'')
    def cookie(response,token):
        response.set_cookie(cookie_name,token,max_age=43200 if token else 0,secure=True,httponly=True,samesite='Lax',path='/')
        return response

    @app.before_request
    def guard():
        # Render's health probe contains no account data and may use an internal hostname.
        if request.path=='/healthz' and request.method=='GET':return None
        if request.host.lower()!=parsed.netloc.lower():
            # Keep old Render bookmarks usable after moving to a custom domain.
            # Never forward credentials, API calls, OAuth callbacks or query strings.
            if render_host and request.host.lower()==render_host and request.method in ('GET','HEAD') and request.path in redirect_pages:
                return redirect(origin+request.path,code=302)
            return jsonify(error='Unexpected host.'),400
        # Provider callbacks authenticate their untouched body with Stripe signatures.
        # No other API route is exempt from browser session/origin/CSRF checks.
        if request.path=='/api/billing/webhook' and request.method=='POST':
            request.max_content_length=262144
            return None
        if request.method=='POST' or request.path=='/oauth/google/callback':
            hub.mutations.acquire();g.mutation_lock=True
        g.auth=hub.accounts.session(current_token())
        if g.auth and allowed is not None and g.auth['user']['email'] not in allowed:
            hub.accounts.logout(current_token());g.auth=None
        if request.method=='POST':
            if request.headers.get('Origin')!=origin or request.headers.get('Sec-Fetch-Site')=='cross-site':return jsonify(error='Request origin rejected.'),403
            csrf=g.auth['csrf'] if g.auth else hub.accounts.public_csrf
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),csrf):return jsonify(error='Refresh the page and try again.'),403
        if (request.path.startswith('/api/') and request.path not in ('/api/inquiries','/api/plans','/api/session','/api/login','/api/register','/api/password-reset/request','/api/password-reset/check','/api/password-reset/complete')) or request.path.startswith('/evidence/') or request.path=='/oauth/google/callback':
            if not g.auth:return jsonify(error='Please sign in.'),401

    @app.teardown_request
    def release_mutation_lock(error=None):
        if g.pop('mutation_lock',False):hub.mutations.release()

    @app.after_request
    def security_headers(response):
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Strict-Transport-Security':'max-age=31536000', 'Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.errorhandler(ValueError)
    def invalid(error):return jsonify(error=str(error)),400
    @app.errorhandler(BillingUnavailable)
    def billing_unavailable(error):return jsonify(error=str(error)),503
    @app.errorhandler(413)
    def oversized(error):return jsonify(error='Request is too large.'),413
    @app.errorhandler(500)
    def failed(error):return jsonify(error='Server could not complete this operation.'),500
    def data():
        value=request.get_json(silent=True)
        if not isinstance(value,dict):raise ValueError('Send a JSON object.')
        return value
    def workspace():return hub.workspace(g.auth['user'])

    @app.get('/healthz')
    def health():return jsonify(ok=True)
    @app.get('/api/session')
    def session():return jsonify(user=g.auth['user'] if g.auth else None,csrf=g.auth['csrf'] if g.auth else hub.accounts.public_csrf,setup_required=hub.accounts.setup_required(),public_registration=public_registration)
    @app.get('/api/plans')
    def plans():return jsonify(hub.billing.catalog())
    @app.post('/api/inquiries')
    def submit_inquiry():
        hub.inquiries.submit(data())
        return jsonify(ok=True,message='Your request has been saved for review. This does not create an account or start a subscription.'),202
    @app.post('/api/inquiries/delete')
    def delete_inquiry():
        if not g.auth['user']['owner']:return jsonify(error='Only the owner can manage requests.'),403
        hub.inquiries.delete(data().get('id'))
        return jsonify(ok=True)
    @app.post('/api/password-reset/request')
    def request_password_reset():
        if not hub.recovery.available():
            return jsonify(error='Password reset emails are temporarily unavailable. Please try again later.'),503
        hub.recovery.request(data().get('email'))
        return jsonify(ok=True,message=REQUEST_MESSAGE),202
    @app.post('/api/password-reset/check')
    def check_password_reset():
        return jsonify(valid=hub.accounts.check_password_reset(data().get('token'),allowed))
    @app.post('/api/password-reset/complete')
    def complete_password_reset():
        value=data()
        email=hub.accounts.complete_password_reset(value.get('token'),value.get('password'),value.get('confirmation'),allowed)
        hub.recovery.changed(email)
        return cookie(jsonify(ok=True),'')
    @app.post('/api/register')
    @app.post('/api/login')
    def authenticate():
        value=data()
        if allowed is not None and str(value.get('email','')).strip().lower() not in allowed:raise ValueError('Access has not been enabled for this account yet. Request access through the contact page.')
        token,user=hub.accounts.authenticate(value,register=request.path=='/api/register')
        hub.accounts.logout(current_token());return cookie(jsonify(ok=True,user=user),token)
    @app.post('/api/logout')
    def logout():
        hub.accounts.logout(current_token());return cookie(jsonify(ok=True),'')
    @app.get('/api/state')
    def state():
        access=hub.billing.state(g.auth['user'])
        value=workspace().snapshot(history_days=access['history_days']);value['csrf']=g.auth['csrf']
        value['billing']=access
        if g.auth['user']['owner']:
            value['inquiries']=hub.inquiries.listing()
            value['inquiry_notifications']=hub.inquiries.notification_settings()
        if g.auth['user']['owner']:value['backups']=hub.backup_status()
        return jsonify(value)
    @app.get('/api/history')
    def history():
        try:page=max(0,int(request.args.get('page','0')))
        except ValueError:raise ValueError('Choose a valid history page.')
        access=hub.billing.state(g.auth['user'])
        rows=workspace().snapshot(history_days=access['history_days'],report_limit=None)['reports']
        ident=request.args.get('monitor','')
        if ident:rows=[r for r in rows if r['profile_id']==ident]
        return jsonify(reports=rows[page*50:(page+1)*50],total=len(rows),page=page,page_size=50)
    @app.get('/api/history.csv')
    def export_history():
        access=hub.billing.state(g.auth['user'])
        if not access['features']['csv_export']:return jsonify(error='CSV export is included in Studio.'),403
        rows=workspace().snapshot(history_days=access['history_days'],report_limit=None)['reports']
        ident=request.args.get('monitor','')
        if ident:rows=[r for r in rows if r['profile_id']==ident]
        return send_file(io.BytesIO(history_csv(rows).encode('utf-8')),mimetype='text/csv',
                         as_attachment=True,download_name='sentleaf-check-history.csv')
    @app.get('/api/reports/weekly')
    def weekly_report():
        access=hub.billing.state(g.auth['user'])
        if not access['features']['weekly_report']:return jsonify(error='Weekly reports are included in Studio.'),403
        try:weeks=int(request.args.get('weeks_ago','0'))
        except ValueError:raise ValueError('Choose a valid week.')
        if not 0<=weeks<=11:raise ValueError('Choose one of the last 12 weeks.')
        return jsonify(weekly_summary(workspace().snapshot(history_days=90,event_limit=None),weeks))
    @app.post('/api/billing/trial')
    def billing_trial():
        if not any(a['available'] for a in workspace().oauth.store.listing()):
            raise ValueError('Connect Gmail first so you can use the full trial.')
        return jsonify(ok=True,billing=hub.billing.start_trial(g.auth['user']))
    @app.post('/api/billing/checkout')
    def billing_checkout():
        hub.billing.ensure_ready()
        if not any(a['available'] for a in workspace().oauth.store.listing()):
            raise ValueError('Connect Gmail before choosing a paid plan so you can confirm access works for your account.')
        return jsonify(url=hub.billing.checkout(g.auth['user'],data().get('plan')))
    @app.post('/api/billing/portal')
    def billing_portal():return jsonify(url=hub.billing.portal(g.auth['user']))
    @app.post('/api/billing/refresh')
    def billing_refresh():
        hub.billing.sync(g.auth['user']['id'])
        return jsonify(ok=True,billing=hub.billing.state(g.auth['user']))
    @app.post('/api/billing/webhook')
    def billing_webhook():
        hub.billing.webhook(request.get_data(),request.headers.get('Stripe-Signature',''))
        return jsonify(received=True)
    @app.post('/api/backups/download')
    def download_backup():
        if not g.auth['user']['owner']:return jsonify(error='Only the server owner can download backups.'),403
        files=backup_files(hub.root)
        # Reuse a recent copy to avoid repeated clicks filling the disk.
        path=files[0] if files and time.time()-files[0].stat().st_mtime<60 else hub.backup()
        return send_file(io.BytesIO(path.read_bytes()),mimetype='application/octet-stream',
                         as_attachment=True,download_name=path.name)
    @app.post('/api/profiles')
    def profile():return jsonify(ok=True,id=workspace().save(data()))
    @app.post('/api/sites/challenge')
    def site_challenge():return jsonify(hub.sites.challenge(g.auth['user'],str(data().get('url',''))))
    @app.post('/api/sites/verify')
    def site_verify():return jsonify(hub.sites.verify(g.auth['user'],str(data().get('url',''))))
    @app.post('/api/billing/readiness')
    def billing_readiness():
        if not g.auth['user']['owner']:return jsonify(error='Only the owner can check billing configuration.'),403
        from billing_readiness import inspect_billing
        return jsonify(inspect_billing(hub.billing))
    @app.post('/api/run')
    def run():workspace().run(data().get('id'));return jsonify(ok=True)
    @app.post('/api/delete')
    def delete():workspace().delete(data().get('id'));return jsonify(ok=True)
    @app.post('/api/schedule')
    def schedule():
        value=data();workspace().schedule(value.get('id'),value.get('enabled'));return jsonify(ok=True)
    @app.post('/api/google/disconnect')
    def disconnect():workspace().disconnect_google(data().get('id'));return jsonify(ok=True)
    @app.post('/api/google/start')
    def google_start():
        w=workspace()
        with w.lock:
            if w.active:raise ValueError('Wait for the current check to finish.')
            return jsonify(ok=True,url=w.oauth.start(current_token()))
    @app.get('/oauth/google/callback')
    def google_callback():
        w=workspace()
        try:w.oauth.complete(request.args,current_token())
        except ValueError as error:w.oauth.message=str(error)
        return redirect('/workspace#connections',code=303)
    @app.get('/evidence/<ident>/<run>/page.png')
    def evidence(ident,run):
        w=workspace()
        if not re.fullmatch('[a-f0-9]{32}',ident) or not re.fullmatch('MONITOR-[a-f0-9]{32}',run) or not any(p['id']==ident for p in w.profiles):return jsonify(error='Not found.'),404
        path=w.root/'results/monitors'/ident/'reports'/run/'page.png'
        if not path.is_file():return jsonify(error='Not found.'),404
        report=read_json(path.parent/'report.json',{})
        if timestamp(report.get('started_utc')) < time.time()-hub.billing.state(g.auth['user'])['history_days']*86400:
            return jsonify(error='This screenshot is outside your plan’s history window.'),404
        return send_file(path,mimetype='image/png')
    assets={'':'home.html','workspace':'index.html','login':'login.html','login.js':'login.js','login.css':'login.css','home.css':'home.css','home.js':'home.js','style.css':'style.css','app.js':'app.js','brand.css':'brand.css','logo.svg':'logo.svg','favicon.svg':'favicon.svg','instrument-regular.woff2':'instrument-regular.woff2','instrument-semibold.woff2':'instrument-semibold.woff2'}
    assets.update({'forgot-password':'recovery.html','reset-password':'recovery.html','recovery.js':'recovery.js'})
    assets.update({'contact':'contact.html','privacy':'privacy.html','contact.js':'contact.js','contact.css':'contact.css'})
    assets['trial']='trial.html'
    assets.update({'plans.js':'plans.js','plans.css':'plans.css','refresh.css':'refresh.css'})
    @app.get('/')
    @app.get('/<path:name>')
    def static_file(name=''):
        if name=='trial' and g.auth:return redirect('/workspace#billing',code=303)
        if name=='trial' and public_registration:return redirect('/login?signup=1',code=303)
        if name=='workspace' and not g.auth:return redirect('/login',code=303)
        if name not in assets:return jsonify(error='Not found.'),404
        return send_file(ROOT/'dashboard_assets'/assets[name])
    hub.start_backups()
    hub.billing.start()
    hub.inquiries.start()
    return app
