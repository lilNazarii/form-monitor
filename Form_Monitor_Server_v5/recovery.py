"""Bounded, asynchronous account emails; public responses never reveal membership."""
import hashlib
import logging
import os
import queue
import re
import threading
import time

from email_sender import send_email

RESET_SECONDS = 15 * 60
REQUEST_MESSAGE = ('If this email belongs to an enabled account, a reset link will arrive shortly. '
                   'Check your inbox and spam folder. You can request another link after one minute.')


class PasswordRecovery:
    def __init__(self, accounts, origin, allowed):
        self.accounts = accounts
        self.origin = origin
        self.allowed = frozenset(allowed)
        self.jobs = queue.Queue(maxsize=32)
        self.lock = threading.Lock()
        self.requests = []
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self.run, name='account-emails', daemon=True)
        self.worker.start()

    @staticmethod
    def available():
        return bool(os.environ.get('RESEND_API_KEY', '').strip())

    def request(self, email):
        if not isinstance(email, str):
            raise ValueError('Enter a valid email address.')
        email = email.strip().lower()
        if len(email) > 254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email):
            raise ValueError('Enter a valid email address.')
        # Apply the same limits even to unknown or disabled addresses. Do not
        # query membership or wait for the email provider in the HTTP handler.
        key = hashlib.sha256(email.encode()).hexdigest()
        with self.lock:
            now = time.monotonic()
            self.requests = [(t, k) for t, k in self.requests if now-t < 3600]
            own = [t for t, k in self.requests if k == key]
            if (self.stopped.is_set() or len(self.requests) >= 60
                    or sum(now-t < 60 for t, _ in self.requests) >= 10
                    or len(own) >= 3 or any(now-t < 60 for t in own)):
                return
            try:
                self.jobs.put_nowait(('reset', email))
            except queue.Full:
                return
            self.requests.append((now, key))

    def changed(self, email):
        try:
            self.jobs.put_nowait(('changed', email))
        except queue.Full:
            logging.getLogger(__name__).error('Password-change notification queue is full.')

    def run(self):
        while not self.stopped.is_set():
            try:
                job = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if job is None:
                    return
                kind, email = job
                self.deliver(kind, email)
            except Exception:
                # No addresses, bodies, token values, provider payloads, or traceback locals.
                logging.getLogger(__name__).error('Account email could not be sent. Check the email provider configuration.')
            finally:
                self.jobs.task_done()

    def deliver(self, kind, email):
        if email not in self.allowed:
            return
        if kind == 'changed':
            send_email('[Sentleaf] Your password was changed',
                       'Your Sentleaf password was changed. All previous sign-in sessions for this account were ended.\n\n'
                       'If you did not make this change, secure your email account and request a new password reset at:\n'
                       f'{self.origin}/forgot-password\n\n'
                       'Sentleaf will never ask you to send your password by email.',
                       recipient=email, sensitive=True)
            return
        token = self.accounts.issue_password_reset(email, RESET_SECONDS)
        if token is None:
            return
        # Fragments are not sent to Render/access logs or as an HTTP referrer.
        # The reset page removes it from the address bar and submits it only in a POST body.
        link = f'{self.origin}/reset-password#token={token}'
        try:
            send_email('[Sentleaf] Reset your password',
                       'A password reset was requested for your Sentleaf account.\n\n'
                       f'Choose a new password using this link:\n{link}\n\n'
                       'This link expires in 15 minutes. It can be used once.\n\n'
                       'If you did not request this, you can ignore this email. Your password has not changed.\n\n'
                       'Sentleaf will never ask you to send your password by email.',
                       recipient=email, sensitive=True)
        except Exception:
            self.accounts.discard_password_reset(token)
            raise

    def close(self):
        self.stopped.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass
        self.worker.join(timeout=22)
