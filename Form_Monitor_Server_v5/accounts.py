"""Local account ownership, password hashing and expiring browser sessions."""
import hashlib
import hmac
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

class Accounts:
    def __init__(self, root):
        self.root = Path(root)
        self.folder = self.root / 'results'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.database = self.folder / 'accounts.sqlite3'
        self.lock = threading.RLock()
        self.sessions = {}
        self.attempts = []
        self.reset_attempts = []
        self.public_csrf = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, salt BLOB NOT NULL, digest BLOB NOT NULL, owner INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS password_resets (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, password_digest BLOB NOT NULL, expires REAL NOT NULL)')
            db.execute('DELETE FROM password_resets WHERE expires <= ?', (time.time(),))
        self.setup_code = secrets.token_urlsafe(18) if self.setup_required() else ''

    def connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        return db

    def setup_required(self):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0

    @staticmethod
    def digest(password, salt):
        return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=3, maxmem=128*1024*1024, dklen=32)

    def data_root(self, user):
        if not re.fullmatch('[a-f0-9]{32}', user['id']):
            raise ValueError('Invalid account.')
        return self.folder / 'users' / user['id']

    def throttle(self):
        now = time.monotonic()
        self.attempts = [t for t in self.attempts if now-t < 60]
        if len(self.attempts) >= 10:
            raise ValueError('Too many attempts. Wait one minute and try again.')
        self.attempts.append(now)

    def authenticate(self, data, register=False):
        email = str(data.get('email', '')).strip().lower()
        password = data.get('password', '')
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            raise ValueError('Enter a password of up to 256 characters.')
        if len(email) > 254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email):
            raise ValueError('Enter a valid email address.')
        with self.lock:
            self.throttle()
            with self.connect() as db:
                if register:
                    owner = db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
                    if owner and not hmac.compare_digest(str(data.get('setup_code', '')), self.setup_code):
                        raise ValueError('Enter the owner setup code shown in the dashboard terminal.')
                    if len(password) < 12:
                        raise ValueError('Use at least 12 characters for your password.')
                    if db.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
                        raise ValueError('Could not create this account. Try signing in.')
                    user = dict(id=uuid.uuid4().hex, email=email, owner=bool(owner))
                    salt = secrets.token_bytes(16)
                    digest = self.digest(password, salt)
                    destination = self.data_root(user)
                    try:
                        if owner:
                            for name in ('workspace', 'monitors', 'google_connections'):
                                source = self.folder / name
                                if source.exists():
                                    shutil.copytree(source, destination / 'results' / name)
                        db.execute('INSERT INTO users VALUES (?,?,?,?,?)', (user['id'],email,salt,digest,int(owner)))
                        db.commit()
                    except Exception:
                        if destination.exists(): shutil.rmtree(destination)
                        raise
                    if owner: self.setup_code = ''
                else:
                    row = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
                    computed = self.digest(password, bytes(row['salt']) if row else b'0'*16)
                    if row is None or not hmac.compare_digest(computed, bytes(row['digest'])):
                        raise ValueError('Email or password is incorrect.')
                    user = dict(id=row['id'], email=row['email'], owner=bool(row['owner']))
            token = secrets.token_urlsafe(32)
            now = time.time()
            self.sessions = {k:v for k,v in self.sessions.items() if v['expires'] > now}
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = dict(user=user, csrf=secrets.token_urlsafe(32), expires=now+12*3600)
            return token, user

    def session(self, token):
        if not isinstance(token,str) or len(token)>100: return None
        with self.lock:
            key = hashlib.sha256(token.encode()).hexdigest()
            value = self.sessions.get(key)
            if value and value['expires'] > time.time(): return value
            self.sessions.pop(key, None)
            return None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def issue_password_reset(self, email, lifetime=900):
        """Store only a random token's hash; requesting a link does not alter login."""
        with self.lock, self.connect() as db:
            now = time.time()
            db.execute('DELETE FROM password_resets WHERE expires <= ?', (now,))
            row = db.execute('SELECT id,digest FROM users WHERE email=?', (email,)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            db.execute('INSERT INTO password_resets VALUES (?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), row['id'], row['digest'], now+lifetime))
            return token

    def discard_password_reset(self, token):
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM password_resets WHERE token_hash=?',
                       (hashlib.sha256(token.encode()).hexdigest(),))

    def reset_throttle(self):
        # Independent of login and link requests, so requests cannot lock out login.
        now = time.monotonic()
        self.reset_attempts = [t for t in self.reset_attempts if now-t < 60]
        if len(self.reset_attempts) >= 60:
            raise ValueError('Too many attempts. Wait one minute and try again.')
        self.reset_attempts.append(now)

    @staticmethod
    def reset_account(db, token, allowed):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        row = db.execute('''SELECT users.id,users.email,users.digest,password_resets.password_digest
            FROM password_resets JOIN users ON users.id=password_resets.user_id
            WHERE token_hash=? AND expires>?''',
            (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if (row is None or row['email'] not in allowed
                or not hmac.compare_digest(bytes(row['digest']), bytes(row['password_digest']))):
            return None
        return row

    def check_password_reset(self, token, allowed):
        with self.lock, self.connect() as db:
            self.reset_throttle()
            return self.reset_account(db, token, allowed) is not None

    def complete_password_reset(self, token, password, confirmation, allowed):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError('Use between 12 and 256 characters for your password.')
        if not isinstance(confirmation, str) or password != confirmation:
            raise ValueError('Passwords do not match.')
        with self.lock, self.connect() as db:
            self.reset_throttle()
            # Check, update, and consumption must be one transaction, including
            # when more than one request or Accounts instance redeems a link.
            db.execute('BEGIN IMMEDIATE')
            row = self.reset_account(db, token, allowed)
            if row is None:
                raise ValueError('This reset link is invalid or has expired. Request a new one.')
            salt = secrets.token_bytes(16)
            digest = self.digest(password, salt)
            db.execute('UPDATE users SET salt=?,digest=? WHERE id=?', (salt, digest, row['id']))
            db.execute('DELETE FROM password_resets WHERE user_id=?', (row['id'],))
            db.commit()
            self.sessions = {key: value for key, value in self.sessions.items()
                             if value['user']['id'] != row['id']}
            return row['email']
