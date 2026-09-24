"""Form Monitor local workspace. Start with the existing project virtualenv."""
import argparse
from collections import deque
from datetime import datetime, timezone
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from http.cookies import SimpleCookie
from accounts import Accounts
import uuid
import webbrowser
from google_mail import OAuthFlow, Store, MailError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
FIELDS = {"name":"your-name","email":"your-email","subject":"your-subject","message":"your-message"}


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def events_at(path, limit=100):
    events = []
    try:
        with path.open(encoding='utf-8') as file:
            for line in deque(file, maxlen=limit):
                try:
                    item = json.loads(line)
                    if isinstance(item, dict): events.append(item)
                except ValueError: pass
    except OSError: pass
    return events


class Workspace:
    def __init__(self, root=ROOT, user=None, code_root=ROOT):
        self.root = root
        self.code_root = code_root
        self.user = user or dict(owner=True, email="")
        self.oauth = OAuthFlow(root, code_root)
        self.data = root / 'results' / 'workspace'
        self.file = self.data / 'profiles.json'
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = False
        self.active = None
        self.worker = None
        self.logs = deque(maxlen=300)
        self.due = {}
        self.last_errors = {}
        self.csrf = secrets.token_urlsafe(32)
        if self.file.exists():
            self.profiles = read_json(self.file)
            if not isinstance(self.profiles, list):
                raise ValueError('profiles.json is damaged. Restore it from a backup before starting.')
        else:
            self.profiles = [dict(id=uuid.uuid4().hex, name='Form Monitor Lab', url='http://form-monitor-lab.local/contact/',
                selector='form.wpcf7-form', fields=dict(FIELDS), interval=60, revision=1)]
            if not self.user['owner']: self.profiles = []
            write_json(self.file, self.profiles)

    def profile(self, ident):
        for item in self.profiles:
            if item['id'] == ident and not item.get('deleted'): return item
        raise ValueError('Monitor not found.')

    @staticmethod
    def validate(data):
        name = str(data.get('name','')).strip()
        url = str(data.get('url','')).strip()
        parsed = urlsplit(url)
        if not 1 <= len(name) <= 80: raise ValueError('Enter a monitor name (up to 80 characters).')
        if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
            or len(url)>2000 or any(c.isspace() for c in url)):
            raise ValueError('Enter a complete http:// or https:// form URL without a password.')
        try: parsed.port
        except ValueError: raise ValueError('Invalid URL port.')
        interval = data.get('interval')
        if type(interval) is not int or not 1 <= interval <= 10080: raise ValueError('Interval must be 1–10080 minutes.')
        selector = str(data.get('selector','form.wpcf7-form')).strip()
        if not 1 <= len(selector) <= 300: raise ValueError('Enter a form selector.')
        fields = data.get('fields',FIELDS)
        if not isinstance(fields,dict) or set(fields)!=set(FIELDS): raise ValueError('Provide all four form field names.')
        if any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_\-]{1,80}',v) for v in fields.values()):
            raise ValueError('Field names may contain letters, numbers, underscores and hyphens.')
        if len(set(fields.values()))!=4: raise ValueError('Each field must have a different name.')
        connection_id = data.get('connection_id','')
        if not isinstance(connection_id,str) or (connection_id and not re.fullmatch('[a-f0-9]{32}',connection_id)):
            raise ValueError('Choose a valid Gmail connection.')
        group = data.get('group', '')
        if not isinstance(group, str) or len(group.strip()) > 60:
            raise ValueError('Use a client or site group of up to 60 characters.')
        return dict(name=name,url=url,selector=selector,fields=fields,interval=interval,connection_id=connection_id,group=group.strip())

    def save(self, data):
        value = self.validate(data)
        with self.lock:
            if value['connection_id']:self.oauth.store.load(value['connection_id'])
            elif not self.user['owner']:raise ValueError('Connect and choose your Gmail account first.')
            ident = data.get('id')
            if ident:
                previous = self.profile(ident)
                if self.active == ident: raise ValueError('Wait for this check to finish before editing.')
                revision = previous['revision'] + int(any(previous.get(k,'')!=value[k] for k in ('url','selector','fields','connection_id')))
                value.update(id=ident,revision=revision)
                updated = [value if p['id']==ident else p for p in self.profiles]
            else:
                if sum(not p.get('deleted') for p in self.profiles)>=20: raise ValueError('This local workspace supports up to 20 monitors.')
                value.update(id=uuid.uuid4().hex,revision=1)
                updated = self.profiles + [value]
            write_json(self.file, updated)
            self.profiles = updated
            if value['id'] in self.due: self.due[value['id']]=time.time()+value['interval']*60
            self.last_errors.pop(value['id'],None)
            return value['id']

    def delete(self, ident):
        with self.lock:
            previous = self.profile(ident)
            if self.active == ident:
                raise ValueError('Wait for this check to finish before removing the monitor.')
            updated = [dict(p, deleted=True) if p['id']==ident else p for p in self.profiles]
            write_json(self.file, updated)
            self.profiles = updated
            self.due.pop(ident, None)
            self.last_errors.pop(ident, None)
            self.wake.set()

    def start_google(self):
        with self.lock:
            if self.active:raise ValueError('Wait for the active check to finish before connecting Google.')
            return self.oauth.start()

    def disconnect_google(self, ident):
        with self.lock:
            if self.active or self.oauth.pending:raise ValueError('Wait for the active check or Google connection request to finish.')
            self.oauth.store.remove(ident)
            for p in self.profiles:
                if p.get('connection_id')==ident:
                    self.due.pop(p['id'],None)
                    self.last_errors[p['id']]='Gmail was disconnected. Choose an inbox in Edit settings.'

    def connections(self):
        owner=self.user['owner']
        recipient=os.environ.get('ALERT_EMAIL','').strip() if owner else self.user['email']
        return dict(gmail=os.environ.get('GMAIL_ADDRESS','').strip() if owner else '',
            gmail_ready=bool(owner and os.environ.get('GMAIL_ADDRESS','').strip() and os.environ.get('GMAIL_APP_PASSWORD','').strip()),
            alert_email=recipient,
            resend_ready=bool(os.environ.get('RESEND_API_KEY','').strip() and recipient),
            live_link_ready=bool(owner and os.environ.get('LIVE_LINK_USER') and os.environ.get('LIVE_LINK_PASSWORD')),
            owner=owner)

    def ready(self, p):
        c=self.connections()
        if p.get('connection_id'):self.oauth.store.load(p['connection_id'])
        elif not c['gmail_ready']: raise ValueError('Gmail is not connected. Choose a Google account in Edit settings or configure the existing terminal connection.')
        if not c['resend_ready']: raise ValueError('Email alerts are not connected. See Connections for terminal setup.')
        if urlsplit(p['url']).hostname.endswith('.localsite.io') and not c['live_link_ready']:
            raise ValueError('Live Link credentials are missing. See Connections.')

    def run(self, ident):
        with self.lock:
            if self.stop: raise ValueError('Workspace is stopping.')
            if self.oauth.pending:raise ValueError('Finish connecting Gmail before running a check.')
            if self.active: raise ValueError('Another check is running. Wait for it to finish.')
            p=dict(self.profile(ident)); self.ready(p)
            runtime = self.data/'runtime'/f'{ident}.json'
            write_json(runtime,p)
            self.active=ident
            self.logs.clear();self.logs.append('Starting '+p['name']+'…')
            self.last_errors.pop(ident,None)
            self.worker=threading.Thread(target=self.execute,args=(p,runtime),daemon=False)
            self.worker.start()

    def environment_for_run(self,p,runtime):
        env=os.environ.copy();env.update(FM_PROFILE=str(runtime),FM_DATA_ROOT=str(self.root),ALERT_EMAIL=self.connections()['alert_email'],PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1')
        if not self.user['owner']:
            for key in ('GMAIL_ADDRESS','GMAIL_APP_PASSWORD','LIVE_LINK_USER','LIVE_LINK_PASSWORD'):env.pop(key,None)
        return env

    def execute(self,p,runtime):
        env=self.environment_for_run(p,runtime)
        secret_values=[env.get(k,'') for k in ('RESEND_API_KEY','GMAIL_APP_PASSWORD','LIVE_LINK_PASSWORD')]
        def redact(line):
            for secret in secret_values:
                for value in {secret,secret.replace(' ','')}:
                    if value: line=line.replace(value,'[REDACTED]')
            return line
        try:
            with subprocess.Popen([sys.executable,'-u',str(self.code_root/'service.py'),'--once'],cwd=self.code_root,env=env,
                    stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace') as child:
                for line in child.stdout:
                    with self.lock: self.logs.append(redact(line.rstrip()))
                code=child.wait()
            if code not in (0,1):
                with self.lock:self.last_errors[p['id']]='Check or notification error. Open Live activity for details.'
        except Exception as error:
            with self.lock:
                self.last_errors[p['id']]='Could not complete the check. See Live activity.'
                self.logs.append(redact(f'{type(error).__name__}: {error}'))
        finally:
            with self.lock:
                self.active=None
                if p['id'] in self.due: self.due[p['id']]=time.time()+self.profile(p['id'])['interval']*60
                self.wake.set()

    def schedule(self, ident, enabled):
        if type(enabled) is not bool: raise ValueError('Invalid schedule setting.')
        with self.lock:
            p=self.profile(ident)
            if enabled:
                self.ready(p)
                self.due[ident]=time.time()
            else:self.due.pop(ident,None)
            self.wake.set()

    def scheduler(self):
        while not self.stop:
            with self.lock:
                if not self.active and not self.oauth.pending and self.due:
                    ident=min(self.due,key=self.due.get)
                    if self.due[ident]<=time.time():
                        try:self.run(ident)
                        except ValueError as error:
                            self.last_errors[ident]=str(error);self.due.pop(ident,None)
            self.wake.wait(1);self.wake.clear()

    def snapshot(self, history_days=None, report_limit=200, event_limit=100):
        with self.lock:
            profiles=json.loads(json.dumps(self.profiles));active=self.active;due=dict(self.due)
            logs=list(self.logs);errors=dict(self.last_errors)
        all_reports=[];all_events=[]
        for p in profiles:
            folder=self.root/'results'/'monitors'/p['id']
            reports=[]
            for path in (folder/'reports').glob('MONITOR-*/report.json'):
                r=read_json(path)
                if isinstance(r,dict):
                    r['profile_name']=p['name'];r['profile_id']=p['id'];r['archived']=bool(p.get('deleted'))
                    r['evidence']=f"/evidence/{p['id']}/{path.parent.name}/page.png" if (path.parent/'page.png').is_file() else None
                    reports.append(r)
            reports.sort(key=lambda r:r.get('started_utc',''),reverse=True)
            events=[]
            for path in (folder/'service').glob('*/events.jsonl'):events.extend(events_at(path, None if history_days else 100))
            events.sort(key=lambda e:e.get('time_utc',''),reverse=True)
            for e in events:e['profile_name']=p['name'];e['profile_id']=p['id'];e['archived']=bool(p.get('deleted'))
            latest=next((e for e in events if e.get('revision')==p['revision']),None)
            p.update(running=active==p['id'],scheduled=p['id'] in due,next_run=due.get(p['id']),
                status=latest.get('final_status','UNTESTED') if latest else 'UNTESTED',latest=latest,
                last_report=next((r for r in reports if r.get('revision')==p['revision']),None),error=errors.get(p['id']))
            all_reports.extend(reports);all_events.extend(events)
        all_reports.sort(key=lambda r:r.get('started_utc',''),reverse=True)
        all_events.sort(key=lambda e:e.get('time_utc',''),reverse=True)
        if history_days is not None:
            cutoff=time.time()-history_days*86400
            def recent(item,key):
                try:return datetime.fromisoformat(item.get(key,'').replace('Z','+00:00')).timestamp()>=cutoff
                except (TypeError,ValueError):return False
            all_reports=[r for r in all_reports if recent(r,'started_utc')]
            all_events=[e for e in all_events if recent(e,'time_utc')]
            for p in profiles:
                if p['last_report'] and not recent(p['last_report'],'started_utc'):p['last_report']=None
        return dict(user=self.user,google=self.oauth.status(),csrf=self.csrf,profiles=[p for p in profiles if not p.get('deleted')],active=active,logs=logs,connections=self.connections(),
            reports=all_reports[:report_limit],events=all_events[:event_limit],report_count=len(all_reports))
