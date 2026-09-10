"""Single-instance, closed-beta HTTPS application. Run with the supplied Gunicorn config."""
import atexit
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
from server_network import validate_url

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
        return dict(owner=False,gmail='',gmail_ready=False,live_link_ready=False,
                    alert_email=self.user['email'],resend_ready=bool(os.environ.get('RESEND_API_KEY','')))

    def save(self,data):
        validate_url(str(data.get('url','')))
        if not data.get('connection_id'):raise ValueError('Choose your connected Gmail account.')
        ident=super().save(data);self.persist_schedule();return ident

    def ready(self,p):
        validate_url(p['url'])
        if not p.get('connection_id'):raise ValueError('Connect Gmail and choose it in Edit settings.')
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
        try:super().run(ident)
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
                        except ValueError as error:
                            self.last_errors[ident]=str(error);self.due.pop(ident,None);self.persist_schedule()
            self.wake.wait(1);self.wake.clear()

class ServerHub:
    def __init__(self,root,origin):
        self.root=root;self.origin=origin;self.accounts=Accounts(root)
        if self.accounts.setup_required():self.accounts.setup_code=os.environ['FM_SETUP_CODE']
        self.workspaces={};self.lock=threading.RLock();self.runner=threading.Lock();self.stopping=False
        # Schedules are restored for all accounts, without waiting for somebody to log in.
        with self.accounts.connect() as db:users=db.execute('SELECT id,email,owner FROM users').fetchall()
        for row in users:self.workspace(dict(id=row['id'],email=row['email'],owner=bool(row['owner'])))

    def workspace(self,user):
        with self.lock:
            if self.stopping:raise ValueError('Server is restarting. Try again shortly.')
            if user['id'] not in self.workspaces:
                app=ServerWorkspace(self,user);self.workspaces[user['id']]=app
                threading.Thread(target=app.scheduler,daemon=True).start()
            return self.workspaces[user['id']]

    def close(self):
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
    for key in ('FM_ENCRYPTION_KEY','FM_SETUP_CODE','FM_ALLOWED_EMAILS','FM_STORAGE_ROOT'):
        if not os.environ.get(key):raise RuntimeError('Missing server setting: '+key)
    if not re.fullmatch('[a-fA-F0-9]{64}',os.environ['FM_ENCRYPTION_KEY']):raise RuntimeError('FM_ENCRYPTION_KEY must be 64 hex characters.')
    if len(os.environ['FM_SETUP_CODE'])<24:raise RuntimeError('FM_SETUP_CODE must be at least 24 random characters.')
    allowed={x.strip().lower() for x in os.environ['FM_ALLOWED_EMAILS'].split(',') if x.strip()}
    hub=ServerHub(Path(os.environ['FM_STORAGE_ROOT']),origin)
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
        if request.host.lower()!=parsed.netloc.lower():return jsonify(error='Unexpected host.'),400
        g.auth=hub.accounts.session(current_token())
        if g.auth and g.auth['user']['email'] not in allowed:
            hub.accounts.logout(current_token());g.auth=None
        if request.method=='POST':
            if request.headers.get('Origin')!=origin or request.headers.get('Sec-Fetch-Site')=='cross-site':return jsonify(error='Request origin rejected.'),403
            csrf=g.auth['csrf'] if g.auth else hub.accounts.public_csrf
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),csrf):return jsonify(error='Refresh the page and try again.'),403
        if (request.path.startswith('/api/') and request.path not in ('/api/session','/api/login','/api/register')) or request.path.startswith('/evidence/') or request.path=='/oauth/google/callback':
            if not g.auth:return jsonify(error='Please sign in.'),401

    @app.after_request
    def security_headers(response):
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Strict-Transport-Security':'max-age=31536000', 'Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.errorhandler(ValueError)
    def invalid(error):return jsonify(error=str(error)),400
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
    def session():return jsonify(user=g.auth['user'] if g.auth else None,csrf=g.auth['csrf'] if g.auth else hub.accounts.public_csrf,setup_required=hub.accounts.setup_required())
    @app.post('/api/register')
    @app.post('/api/login')
    def authenticate():
        value=data()
        if str(value.get('email','')).strip().lower() not in allowed:raise ValueError('This account is not enabled for the private beta.')
        token,user=hub.accounts.authenticate(value,register=request.path=='/api/register')
        hub.accounts.logout(current_token());return cookie(jsonify(ok=True,user=user),token)
    @app.post('/api/logout')
    def logout():
        hub.accounts.logout(current_token());return cookie(jsonify(ok=True),'')
    @app.get('/api/state')
    def state():
        value=workspace().snapshot();value['csrf']=g.auth['csrf'];return jsonify(value)
    @app.post('/api/profiles')
    def profile():return jsonify(ok=True,id=workspace().save(data()))
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
        return send_file(path,mimetype='image/png')
    assets={'':'home.html','workspace':'index.html','login':'login.html','login.js':'login.js','login.css':'login.css','home.css':'home.css','home.js':'home.js','style.css':'style.css','app.js':'app.js'}
    @app.get('/')
    @app.get('/<path:name>')
    def static_file(name=''):
        if name=='workspace' and not g.auth:return redirect('/login',code=303)
        if name not in assets:return jsonify(error='Not found.'),404
        return send_file(ROOT/'dashboard_assets'/assets[name])
    return app
