"""Self-service website ownership. No administrator queue or shared domain grants."""
import hashlib
import hmac
import os
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit
from server_network import allowed_hosts, public_request, validate_url


class VerificationHTML(HTMLParser):
    def __init__(self, token):
        super().__init__()
        self.token = token
        self.hidden_span = False
        self.found = False

    def handle_starttag(self, tag, attrs):
        self.hidden_span = tag == 'span' and 'hidden' in dict(attrs)

    def handle_endtag(self, tag):
        self.hidden_span = False

    def handle_data(self, data):
        if self.hidden_span and data.strip() == 'sentleaf-verification=' + self.token:
            self.found = True


class SiteVerification:
    def __init__(self, accounts):
        self.accounts = accounts
        self.attempts = {}
        with accounts.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS verified_sites (
                user_id TEXT NOT NULL, host TEXT NOT NULL, verified REAL NOT NULL,
                PRIMARY KEY(user_id,host))''')

    @staticmethod
    def host(url):
        validate_url(url)
        return urlsplit(url).hostname.encode('idna').decode('ascii').lower()

    def verified(self, user, host):
        if user['owner'] and host in allowed_hosts():
            return True
        with self.accounts.connect() as db:
            return db.execute('SELECT 1 FROM verified_sites WHERE user_id=? AND host=?', (user['id'], host)).fetchone() is not None

    def challenge(self, user, url):
        host = self.host(url)
        key = bytes.fromhex(os.environ['FM_ENCRYPTION_KEY'])
        token = hmac.new(key, ('sentleaf-site-v1:' + user['id'] + ':' + host).encode(), hashlib.sha256).hexdigest()
        return dict(host=host, verified=self.verified(user, host), token=token,
                    filename='sentleaf-verification.txt',
                    snippet='<span hidden>sentleaf-verification=' + token + '</span>')

    def verify(self, user, url):
        challenge = self.challenge(user, url)
        if challenge['verified']:
            return challenge
        now = time.time()
        if now - self.attempts.get(user['id'], 0) < 10:
            raise ValueError('Wait a few seconds before checking the website again.')
        self.attempts[user['id']] = now
        token = challenge['token'].encode()
        # Both options work without DNS propagation or a message to the owner.
        candidates = [('https://' + challenge['host'] + '/sentleaf-verification.txt', True), (url, False)]
        for target, is_file in candidates:
            try:
                status, headers, body = public_request(target)
            except ValueError:
                continue
            parser = VerificationHTML(challenge['token'])
            if not is_file:
                parser.feed(body.decode('utf-8', errors='replace'))
            if status == 200 and (body.strip() == token if is_file else parser.found):
                with self.accounts.connect() as db:
                    db.execute('INSERT OR REPLACE INTO verified_sites VALUES (?,?,?)', (user['id'], challenge['host'], now))
                challenge['verified'] = True
                return challenge
        raise ValueError('Verification code not found. Publish the HTML block on this contact page, or upload the text file to the website root, then try again. Use the final page URL if the site redirects.')

    def require(self, user, url):
        host = self.host(url)
        if not self.verified(user, host):
            raise ValueError('Confirm website ownership in the Website step before saving this monitor.')
