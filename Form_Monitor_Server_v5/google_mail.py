"""Local Windows OAuth connection and read-only Gmail API adapter.
No OAuth credentials or message bodies are exposed through the dashboard API.
"""
import base64
import ctypes
from ctypes import wintypes
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit, parse_qs, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

SCOPE='https://www.googleapis.com/auth/gmail.readonly'
TOKEN_URL='https://oauth2.googleapis.com/token'
API='https://gmail.googleapis.com/gmail/v1/users/me/'

class MailError(ValueError): pass

def protected(data, decrypt=False):
    from cryptography.fernet import Fernet, InvalidToken
    try:
        key=base64.urlsafe_b64encode(bytes.fromhex(os.environ['FM_ENCRYPTION_KEY']))
        cipher=Fernet(key)
        return cipher.decrypt(data) if decrypt else cipher.encrypt(data)
    except (KeyError,ValueError,InvalidToken):
        raise MailError('Could not unlock server Gmail storage. Check the encryption key.') from None

def request_json(url, data=None, token=None):
    headers={'Accept':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    body=urlencode(data).encode() if data is not None else None
    if body is not None:headers['Content-Type']='application/x-www-form-urlencoded'
    try:
        with urlopen(Request(url,data=body,headers=headers),timeout=12) as response:return json.load(response)
    except HTTPError as error:
        # Do not propagate response bodies, tokens, authorization codes, or URLs.
        if error.code in (400,401):raise MailError('Google authorization expired or was rejected. Reconnect this Gmail account.') from None
        if error.code==403:raise MailError('Google denied access. Check Gmail API, test users and the read-only permission in Google Cloud.') from None
        if error.code==429:raise MailError('Google temporarily limited requests. Try again later.') from None
        raise MailError('Google service returned an error. Try again later.') from None
    except (URLError,TimeoutError,ValueError):raise MailError('Could not reach Google or read its response. Check the connection and try again.') from None

class Store:
    def __init__(self,root,client_root=None):self.root=Path(root);self.client_root=Path(client_root) if client_root else Path(__file__).resolve().parent;self.folder=self.root/'results'/'google_connections'
    def client(self):
        ident=os.environ.get('GOOGLE_CLIENT_ID','').strip()
        secret=os.environ.get('GOOGLE_CLIENT_SECRET','').strip()
        if not ident.endswith('.apps.googleusercontent.com') or not secret:
            raise MailError('The server owner must configure a Google Web application client.')
        return dict(client_id=ident,client_secret=secret)
    def path(self,ident):
        if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident):raise MailError('Invalid Gmail connection.')
        return self.folder/(ident+'.bin')
    def load(self,ident):
        try:return json.loads(protected(self.path(ident).read_bytes(),decrypt=True))
        except OSError:raise MailError('Gmail connection was removed. Choose another inbox in Edit settings.') from None
    def save(self,ident,value):
        data=protected(json.dumps(value).encode());path=self.path(ident);path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.tmp');temp.write_bytes(data);temp.replace(path)
    def listing(self):
        items=[]
        for p in sorted(self.folder.glob('*.bin')):
            try:
                value=self.load(p.stem);items.append(dict(id=p.stem,email=value['email'],available=True))
            except (ValueError,KeyError):items.append(dict(id=p.stem,email='Connection needs reconnecting',available=False))
        return items
    def remove(self,ident):
        try:self.path(ident).unlink()
        except FileNotFoundError:raise MailError('Gmail connection was already removed.') from None
    def access(self,ident,force=False):
        value=self.load(ident)
        if force or value.get('expires_at',0)<time.time()+30:
            client=self.client()
            if value.get('client_id')!=client['client_id']:raise MailError('The Google app configuration changed. Reconnect Gmail.')
            result=request_json(TOKEN_URL,dict(client,grant_type='refresh_token',refresh_token=value['refresh_token']))
            if not result.get('access_token'):raise MailError('Google did not return an access token. Reconnect Gmail.')
            value.update(access_token=result['access_token'],expires_at=time.time()+int(result.get('expires_in',3600)))
            if result.get('refresh_token'):value['refresh_token']=result['refresh_token']
            self.save(ident,value)
        return value['access_token']

class GmailAPI:
    """Small adapter retaining the monitor's INBOX search and raw-message matcher."""
    def __init__(self,root,ident):
        self.store=Store(root);self.ident=ident;self.token=self.store.access(ident)
        profile=self.get('profile');expected=self.store.load(ident)['email']
        if profile.get('emailAddress','').lower()!=expected.lower():raise MailError('Gmail account does not match the selected connection. Reconnect it.')
    def get(self,path):return request_json(API+path,token=self.token)
    def select(self,mailbox,readonly=True):
        if mailbox!='INBOX' or not readonly:raise MailError('Only read-only Inbox checks are supported.')
        self.token=self.store.access(self.ident)
        return 'OK',[]
    def uid(self,operation,*args):
        if operation=='search':
            token=str(args[-1]).strip('"')
            if not re.fullmatch(r'MONITOR-[a-f0-9]{32}',token):raise MailError('Invalid test reference.')
            ids=[];page=None
            while True:
                query=dict(labelIds='INBOX',q='subject:'+token,maxResults=100)
                if page:query['pageToken']=page
                result=self.get('messages?'+urlencode(query))
                ids.extend(m['id'].encode('ascii') for m in result.get('messages',[]))
                page=result.get('nextPageToken')
                if not page:break
            return 'OK',[b' '.join(ids)]
        if operation=='fetch':
            if args[1]!='(BODY.PEEK[])':raise MailError('Only read-only message access is supported.')
            ident=args[0].decode('ascii');result=self.get('messages/'+quote(ident,safe='')+'?format=raw')
            if 'INBOX' not in result.get('labelIds',[]):return 'OK',[]
            raw=result.get('raw','')
            if not raw:return 'OK',[]
            return 'OK',[(b'RAW',base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))]
        raise MailError('Unsupported Gmail operation.')
    def logout(self):return 'BYE',[]

# The desktop dashboard is retained only as a reusable workspace module.
class OAuthFlow:
    def __init__(self,*args):pass
