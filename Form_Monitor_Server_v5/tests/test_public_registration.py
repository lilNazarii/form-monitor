"""Open registration must not open owner routes or bypass account isolation."""
import os
import secrets
import tempfile
import unittest
from unittest.mock import patch
from server import create_app


class PublicRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            'FM_PUBLIC_REGISTRATION': '1', 'FM_ALLOWED_EMAILS': 'owner@example.com',
            'FM_STORAGE_ROOT': self.temp.name, 'FM_PUBLIC_URL': 'https://beta.example',
            'FM_SETUP_CODE': 'test-owner-code-that-is-long-enough',
            'FM_ENCRYPTION_KEY': secrets.token_hex(32), 'FM_BACKUPS_ENABLED': '0',
            'FM_INQUIRY_EMAILS_ENABLED': '0', 'FM_BILLING_ENABLED': '0',
            'FM_TRIALS_ENABLED': '1', 'GOOGLE_CLIENT_ID': 'test.apps.googleusercontent.com',
            'GOOGLE_CLIENT_SECRET': 'fake', 'RESEND_API_KEY': ''})
        env.start(); self.addCleanup(env.stop)
        self.app = create_app(); self.app.testing = True
        self.hub = self.app.extensions['hub']; self.addCleanup(self.hub.close)
        self.owner = self.app.test_client(); self.visitor = self.app.test_client()

    def get(self, client, path):
        return client.get(path, base_url='https://beta.example')

    def post(self, client, path, value):
        csrf = self.get(client, '/api/session').json['csrf']
        return client.post(path, json=value, base_url='https://beta.example',
            headers={'Origin': 'https://beta.example', 'X-CSRF-Token': csrf})

    def register(self, client, email, **extra):
        return self.post(client, '/api/register', dict(email=email,
            password='long enough test password', **extra))

    def test_public_account_isolation_login_and_recovery(self):
        self.assertTrue(self.get(self.visitor, '/api/session').json['public_registration'])
        self.assertEqual(self.get(self.visitor, '/trial').headers['Location'], '/login?signup=1')
        # Public registration must never bypass first-owner setup.
        self.assertEqual(self.register(self.visitor, 'random@example.net').status_code, 400)
        self.assertTrue(self.register(self.owner, 'owner@example.com',
            setup_code=os.environ['FM_SETUP_CODE']).json['user']['owner'])
        result = self.register(self.visitor, 'random@example.net', owner=True)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertFalse(result.json['user']['owner'])
        self.assertEqual(self.get(self.visitor, '/trial').headers['Location'], '/workspace#billing')
        self.assertFalse(self.get(self.visitor, '/api/plans').json['access_review_required'])
        state = self.get(self.visitor, '/api/state')
        self.assertEqual(state.status_code, 200)
        for key in ('inquiries', 'inquiry_notifications', 'backups'):
            self.assertNotIn(key, state.json)
        self.assertEqual(state.json['profiles'], [])
        self.assertEqual(self.post(self.visitor, '/api/inquiries/delete', {'id':'x'}).status_code, 403)
        token = self.hub.accounts.issue_password_reset('random@example.net')
        self.assertTrue(self.post(self.visitor, '/api/password-reset/check', {'token':token}).json['valid'])
        with patch('recovery.send_email'):
            result = self.post(self.visitor, '/api/password-reset/complete', {
                'token':token, 'password':'new long enough password', 'confirmation':'new long enough password'})
            self.assertEqual(result.status_code, 200, result.json)
        self.assertIsNone(self.get(self.visitor, '/api/session').json['user'])
        self.assertEqual(self.post(self.visitor, '/api/login', {
            'email':'random@example.net', 'password':'new long enough password'}).status_code, 200)
        self.assertFalse(self.get(self.visitor, '/api/session').json['user']['owner'])


if __name__ == '__main__': unittest.main()
