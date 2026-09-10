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
        self.public_csrf = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, salt BLOB NOT NULL, digest BLOB NOT NULL, owner INTEGER NOT NULL)')
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
