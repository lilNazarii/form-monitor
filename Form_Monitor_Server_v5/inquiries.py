"""Small, owner-reviewed inbox. Submitting a request never grants access."""
import re
import json
import logging
import os
import threading
import time
import uuid
from urllib.parse import urlsplit
from email_sender import send_email, EmailDeliveryError
from email_templates import inquiry_email


class Inquiries:
    def __init__(self, accounts, origin=''):
        self.accounts = accounts
        self.origin = origin
        self.attempts = []
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.delivery_lock = threading.Lock()
        self.worker = None
        with accounts.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS inquiries (
                id TEXT PRIMARY KEY, email TEXT NOT NULL, website TEXT NOT NULL,
                kind TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL)''')
            # Existing requests are not backfilled into email on deployment.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(inquiries)')}
            for name, definition in {
                'email_status': "TEXT NOT NULL DEFAULT 'not_queued'",
                'email_attempts': 'INTEGER NOT NULL DEFAULT 0',
                'email_next': 'REAL NOT NULL DEFAULT 0',
                'email_first_attempt': 'REAL',
                'email_payload': 'TEXT',
                'email_id': 'TEXT',
            }.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE inquiries ADD COLUMN {name} {definition}')

    def submit(self, data):
        with self.accounts.lock:
            now = time.time()
            self.attempts = [stamp for stamp in self.attempts if now-stamp < 60]
            if len(self.attempts) >= 8:
                raise ValueError('Please wait a minute before sending another request.')
            self.attempts.append(now)
            if data.get('company_fax'):  # Honeypot: do not store bot submissions.
                return
            email = str(data.get('email', '')).strip().lower()
            website = str(data.get('website', '')).strip()
            kind = data.get('kind', 'access')
            message = str(data.get('message', '')).strip()
            if len(email) > 254 or not re.fullmatch(r'[^\s@<>,;:"\\]+@[^\s@<>,;:"\\]+\.[^\s@<>,;:"\\]+', email):
                raise ValueError('Enter a valid email address.')
            if kind not in ('access', 'support', 'data') or len(message) > 2000:
                raise ValueError('Choose a request type and use up to 2,000 characters.')
            if data.get('consent') is not True:
                raise ValueError('Confirm that we may use these details to handle your request.')
            if website:
                try:
                    parsed = urlsplit(website)
                    valid = (len(website) <= 500 and parsed.scheme == 'https' and parsed.hostname
                             and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError('Use an HTTPS page address without login details or query parameters.')
            with self.accounts.connect() as db:
                db.execute('DELETE FROM inquiries WHERE created<?', (now-90*86400,))
                if db.execute('SELECT COUNT(*) FROM inquiries WHERE created>?', (now-86400,)).fetchone()[0] >= 100:
                    raise ValueError('Requests are busy today. Please try again tomorrow.')
                if db.execute('SELECT 1 FROM inquiries WHERE email=? AND kind=? AND created>?', (email,kind,now-3600)).fetchone():
                    return
                db.execute('''INSERT INTO inquiries (id,email,website,kind,message,created,email_status)
                              VALUES (?,?,?,?,?,?,'pending')''', (uuid.uuid4().hex,email,website,kind,message,now))
            self.wake.set()

    def listing(self):
        self.prune()
        with self.accounts.connect() as db:
            return [dict(row) for row in db.execute('''SELECT id,email,website,kind,message,created,email_status
                FROM inquiries ORDER BY created DESC LIMIT 100''')]

    def notification_settings(self):
        with self.accounts.connect() as db:
            owner = db.execute('SELECT email FROM users WHERE owner=1 LIMIT 1').fetchone()
        recipient = owner['email'] if owner else ''
        enabled = os.environ.get('FM_INQUIRY_EMAILS_ENABLED', '1') != '0'
        return dict(recipient=recipient, enabled=enabled,
                    ready=bool(enabled and recipient and self.origin and os.environ.get('RESEND_API_KEY', '').strip()))

    def deliver_next(self):
        """Persist the envelope before sending; retries reuse its idempotency key.

        The single-instance app has one worker. A lease also recovers interrupted
        deliveries after a restart. Never retry past the provider's 24-hour key
        retention, and never use visitor input as the notification recipient.
        """
        if not self.delivery_lock.acquire(blocking=False):
            return False
        try:
            settings = self.notification_settings()
            if not settings['ready']:
                return False
            now = time.time()
            with self.accounts.lock, self.accounts.connect() as db:
                row = db.execute('''SELECT * FROM inquiries WHERE email_status IN ('pending','retry','sending')
                    AND email_next<=? ORDER BY created LIMIT 1''', (now,)).fetchone()
                if row is None:
                    return False
                inquiry = dict(row)
                first_attempt = inquiry['email_first_attempt']
                if inquiry['created'] < now-90*86400 or (first_attempt is not None and now-first_attempt >= 23*3600) or inquiry['email_attempts'] >= 8:
                    db.execute("UPDATE inquiries SET email_status='failed' WHERE id=?", (inquiry['id'],))
                    return True
                if inquiry['email_payload']:
                    payload = json.loads(inquiry['email_payload'])
                else:
                    payload = inquiry_email(inquiry, self.origin)
                    payload.update(recipient=settings['recipient'], sensitive=True,
                                   sender=os.environ.get('FM_EMAIL_FROM', '').strip() or 'Sentleaf <onboarding@resend.dev>',
                                   idempotency_key=f'inquiry/{inquiry["id"]}')
                # A changed owner must never receive mail meant for another owner.
                if payload['recipient'] != settings['recipient']:
                    db.execute("UPDATE inquiries SET email_status='failed' WHERE id=?", (inquiry['id'],))
                    return True
                db.execute('''UPDATE inquiries SET email_status='sending',email_attempts=email_attempts+1,
                    email_next=?,email_first_attempt=COALESCE(email_first_attempt,?),email_payload=? WHERE id=?''',
                    (now+60,now,json.dumps(payload),inquiry['id']))
            try:
                message_id = send_email(**payload)
            except Exception as error:
                attempts = inquiry['email_attempts']+1
                retryable = isinstance(error, EmailDeliveryError) and error.retryable
                status = 'retry' if retryable and attempts < 8 else 'failed'
                delay = min(60*3**(attempts-1), 3600)
                with self.accounts.lock, self.accounts.connect() as db:
                    db.execute('UPDATE inquiries SET email_status=?,email_next=? WHERE id=?',
                               (status,time.time()+delay,inquiry['id']))
                logging.getLogger(__name__).warning('Inquiry notification %s (%s).', status, type(error).__name__)
            else:
                with self.accounts.lock, self.accounts.connect() as db:
                    db.execute("UPDATE inquiries SET email_status='accepted',email_id=? WHERE id=?", (message_id,inquiry['id']))
            return True
        finally:
            self.delivery_lock.release()

    def run(self):
        while not self.stopped.is_set():
            self.wake.clear()
            try:
                processed = self.deliver_next()
            except Exception:
                logging.getLogger(__name__).error('Inquiry notification queue could not be processed.')
                processed = False
            # Bound the rate even after a backlog; never occupy the request thread.
            self.wake.wait(1 if processed else 15)

    def start(self):
        if self.worker is None and os.environ.get('FM_INQUIRY_EMAILS_ENABLED', '1') != '0':
            self.worker = threading.Thread(target=self.run, name='inquiry-emails', daemon=True)
            self.worker.start()

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=22)

    def prune(self):
        with self.accounts.connect() as db:
            db.execute('DELETE FROM inquiries WHERE created<?', (time.time()-90*86400,))

    def delete(self, ident):
        with self.accounts.connect() as db:
            db.execute('DELETE FROM inquiries WHERE id=?', (ident,))
