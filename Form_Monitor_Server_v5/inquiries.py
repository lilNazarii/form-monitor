"""Small, owner-reviewed inbox. Submitting a request never grants access."""
import re
import time
import uuid
from urllib.parse import urlsplit


class Inquiries:
    def __init__(self, accounts):
        self.accounts = accounts
        self.attempts = []
        with accounts.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS inquiries (
                id TEXT PRIMARY KEY, email TEXT NOT NULL, website TEXT NOT NULL,
                kind TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL)''')

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
            if len(email) > 254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email):
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
                db.execute('INSERT INTO inquiries VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex,email,website,kind,message,now))

    def listing(self):
        self.prune()
        with self.accounts.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM inquiries ORDER BY created DESC LIMIT 100')]

    def prune(self):
        with self.accounts.connect() as db:
            db.execute('DELETE FROM inquiries WHERE created<?', (time.time()-90*86400,))

    def delete(self, ident):
        with self.accounts.connect() as db:
            db.execute('DELETE FROM inquiries WHERE id=?', (ident,))
