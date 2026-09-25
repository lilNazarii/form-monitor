import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from accounts import Accounts
from inquiries import Inquiries
from email_sender import send_email, EmailDeliveryError


class InquiryEmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'RESEND_API_KEY': 'fake-test-key',
            'FM_EMAIL_FROM': 'Sentleaf <notifications@example.com>',
            'FM_INQUIRY_EMAILS_ENABLED': '1', 'ALERT_EMAIL': 'wrong@example.com'})
        env.start(); self.addCleanup(env.stop)
        self.accounts = Accounts(Path(self.temp.name))
        with self.accounts.connect() as db:
            db.execute('INSERT INTO users VALUES (?,?,?,?,?)', ('a'*32, 'owner@example.com', b's', b'd', 1))
        self.service = Inquiries(self.accounts, 'https://sentleaf.example')
        self.addCleanup(self.service.close)
        self.value = dict(email='visitor@example.com', kind='support',
                          website='https://example.com/contact', message='Could you help?\nThank you.', consent=True)

    def row(self):
        with self.accounts.connect() as db:
            return dict(db.execute('SELECT * FROM inquiries LIMIT 1').fetchone())

    def test_owner_only_with_reply_to_and_safe_content(self):
        value = dict(self.value, message='<img src=x onerror=alert(1)>\nQuestion', recipient='attacker@example.com')
        self.service.submit(value)
        with patch('inquiries.send_email', return_value='provider-id') as mail:
            self.assertTrue(self.service.deliver_next())
            self.assertFalse(self.service.deliver_next())
        payload = mail.call_args.kwargs
        self.assertEqual(payload['recipient'], 'owner@example.com')
        self.assertEqual(payload['reply_to'], 'visitor@example.com')
        self.assertIn(value['message'], payload['text'])
        self.assertNotIn('<img src=x', payload['html'])
        self.assertIn('&lt;img', payload['html'])
        self.assertIn('https://sentleaf.example/workspace#requests', payload['html'])
        self.assertEqual(self.row()['email_status'], 'accepted')
        self.assertEqual(self.row()['email_id'], 'provider-id')
        self.assertNotIn('email_payload', self.service.listing()[0])

    def test_all_request_types_queue_and_bots_duplicates_do_not(self):
        self.service.submit(dict(self.value, company_fax='bot'))
        self.assertEqual(self.service.listing(), [])
        for kind in ('support', 'access', 'data'):
            self.service.submit(dict(self.value, kind=kind))
            self.service.submit(dict(self.value, kind=kind))
        with patch('inquiries.send_email', return_value='id') as mail:
            for _ in range(3): self.assertTrue(self.service.deliver_next())
            self.assertFalse(self.service.deliver_next())
        self.assertEqual(mail.call_count, 3)
        self.assertEqual(len({call.kwargs['idempotency_key'] for call in mail.call_args_list}), 3)

    def test_retry_survives_restart_and_reuses_exact_envelope(self):
        self.service.submit(self.value)
        with patch('inquiries.send_email', side_effect=EmailDeliveryError('timeout', retryable=True)) as first:
            self.service.deliver_next()
        self.assertEqual(self.row()['email_status'], 'retry')
        self.assertFalse(self.service.deliver_next())
        with self.accounts.connect() as db: db.execute('UPDATE inquiries SET email_next=0')
        restarted = Inquiries(self.accounts, 'https://changed.example')
        with patch.dict(os.environ, {'FM_EMAIL_FROM': 'changed@example.com'}), patch('inquiries.send_email', return_value='id') as second:
            restarted.deliver_next()
        self.assertEqual(first.call_args.kwargs, second.call_args.kwargs)
        self.assertEqual(self.row()['email_status'], 'accepted')

    def test_interrupted_delivery_lease_and_expired_keys(self):
        self.service.submit(self.value)
        with self.accounts.connect() as db:
            db.execute("UPDATE inquiries SET email_status='sending',email_first_attempt=?,email_next=?", (time.time(), time.time()+60))
        with patch('inquiries.send_email', return_value='id') as mail:
            self.assertFalse(self.service.deliver_next())
            with self.accounts.connect() as db:
                db.execute('UPDATE inquiries SET email_next=0,email_first_attempt=?', (time.time()-24*3600,))
            self.assertTrue(self.service.deliver_next())
            mail.assert_not_called()
        self.assertEqual(self.row()['email_status'], 'failed')

    def test_missing_configuration_retains_request_and_permanent_error_stops_retry(self):
        self.service.submit(self.value)
        with patch.dict(os.environ, {'RESEND_API_KEY': ''}), patch('inquiries.send_email') as mail:
            self.assertFalse(self.service.deliver_next()); mail.assert_not_called()
        self.assertEqual(self.row()['email_status'], 'pending')
        with patch('inquiries.send_email', side_effect=EmailDeliveryError('forbidden')) as mail:
            self.service.deliver_next()
            self.assertFalse(self.service.deliver_next())
            self.assertEqual(mail.call_count, 1)
        self.assertEqual(self.row()['email_status'], 'failed')

    def test_delete_removes_pending_delivery_and_email_address_injection_is_rejected(self):
        for address in ('a@example.com,b@example.com', 'a@example.com\r\nBcc:b@example.com', 'a@example.com;b@example.com'):
            with self.assertRaises(ValueError): self.service.submit(dict(self.value,email=address))
        self.service.submit(self.value)
        self.service.delete(self.row()['id'])
        with patch('inquiries.send_email') as mail:
            self.assertFalse(self.service.deliver_next()); mail.assert_not_called()

    def test_worker_does_not_block_submission_and_excludes_concurrent_sender(self):
        entered, release = threading.Event(), threading.Event()
        def slow(**kwargs):
            entered.set(); release.wait(3); return 'id'
        with patch('inquiries.send_email', side_effect=slow) as mail:
            self.service.start()
            try:
                self.service.submit(self.value)
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.service.deliver_next())
                self.assertEqual(len(self.service.listing()), 1)
            finally:
                release.set(); self.service.close()
        self.assertEqual(mail.call_count, 1)
        self.assertEqual(self.row()['email_status'], 'accepted')

    def test_old_schema_is_migrated_without_sending_previous_requests(self):
        with self.accounts.connect() as db:
            db.execute('DROP TABLE inquiries')
            db.execute('CREATE TABLE inquiries(id TEXT PRIMARY KEY,email TEXT,website TEXT,kind TEXT,message TEXT,created REAL)')
            db.execute('INSERT INTO inquiries VALUES(?,?,?,?,?,?)', ('old','visitor@example.com','','support','old message',time.time()))
        upgraded = Inquiries(self.accounts, 'https://sentleaf.example')
        with patch('inquiries.send_email') as mail:
            self.assertFalse(upgraded.deliver_next()); mail.assert_not_called()
        self.assertEqual(upgraded.listing()[0]['email_status'], 'not_queued')

    def test_sender_provider_headers_and_failure_classification(self):
        with patch('email_sender.urlopen', return_value=io.BytesIO(b'{"id":"id"}')) as network:
            send_email('New request', 'message', recipient='owner@example.com', reply_to='visitor@example.com',
                       idempotency_key='inquiry/abc', sender='Sentleaf <notifications@example.com>')
        request = network.call_args.args[0]
        self.assertEqual(request.get_header('Idempotency-key'), 'inquiry/abc')
        self.assertEqual(json.loads(request.data)['reply_to'], 'visitor@example.com')
        for code, name, retryable in ((429,'rate_limit_exceeded',True),(503,'error',True),
                                     (403,'forbidden',False),(409,'invalid_idempotent_request',False),
                                     (409,'concurrent_idempotent_requests',True)):
            error = HTTPError('https://api.resend.com/emails',code,'error',{},io.BytesIO(json.dumps({'name':name,'message':'PRIVATE'}).encode()))
            with patch('email_sender.urlopen', side_effect=error), self.assertRaises(EmailDeliveryError) as caught:
                send_email('New request','PRIVATE',recipient='owner@example.com',sensitive=True)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn('PRIVATE', str(caught.exception))


if __name__ == '__main__': unittest.main()
