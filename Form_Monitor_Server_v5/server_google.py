"""Web OAuth: callback bound to the initiating account and browser session."""
import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import urlencode
from google_mail import Store, MailError, request_json, TOKEN_URL, API, SCOPE

class WebOAuth:
    def __init__(self, root, origin):
        self.store=Store(root)
        self.origin=origin
        self.lock=threading.RLock()
        self.cancel=threading.Event()
        self.flow=None
        self.message=''

    @property
    def pending(self):
        with self.lock:
            if self.flow and self.flow['expires'] < time.time():self.flow=None
            return self.flow is not None

    def status(self):
        try:self.store.client();configured=True
        except MailError:configured=False
        return dict(configured=configured,supported=True,pending=self.pending,message=self.message,accounts=self.store.listing())

    def start(self, session_token):
        client=self.store.client()
        with self.lock:
            if self.pending:raise MailError('Finish the current Google connection or wait five minutes.')
            state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
            self.flow=dict(state=state,verifier=verifier,session=hashlib.sha256(session_token.encode()).hexdigest(),expires=time.time()+300)
            self.message='Waiting for Google approval.'
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(dict(client_id=client['client_id'],redirect_uri=self.origin+'/oauth/google/callback',response_type='code',scope=SCOPE,state=state,code_challenge=challenge,code_challenge_method='S256',access_type='offline',prompt='consent select_account'))

    def complete(self, args, session_token):
        with self.lock:
            if not self.pending:raise MailError('Google request expired. Connect again.')
            flow=self.flow
            if len(args.getlist('state'))!=1 or not secrets.compare_digest(args.get('state',''),flow['state']) or not secrets.compare_digest(hashlib.sha256(session_token.encode()).hexdigest(),flow['session']):
                raise MailError('Google request does not match this signed-in session.')
            self.flow=None
        try:
            if args.get('error') or len(args.getlist('code'))!=1:raise MailError('Google permission was not granted.')
            client=self.store.client()
            tokens=request_json(TOKEN_URL,dict(client,code=args['code'],code_verifier=flow['verifier'],redirect_uri=self.origin+'/oauth/google/callback',grant_type='authorization_code'))
            if not tokens.get('refresh_token') or not tokens.get('access_token') or SCOPE not in tokens.get('scope','').split():raise MailError('Grant read-only Gmail permission and connect again.')
            profile=request_json(API+'profile',token=tokens['access_token']);email=profile.get('emailAddress','').strip().lower()
            if '@' not in email:raise MailError('Google did not identify the mailbox.')
            ident=hashlib.sha256(email.encode()).hexdigest()[:32]
            self.store.save(ident,dict(email=email,client_id=client['client_id'],access_token=tokens['access_token'],refresh_token=tokens['refresh_token'],expires_at=time.time()+int(tokens.get('expires_in',3600))))
            self.message='Connected '+email+'. Choose it in your monitor settings.'
        except Exception:
            self.message='Google connection failed. Check configuration and try again.'
            raise MailError(self.message) from None
