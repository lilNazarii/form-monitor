import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import Mock, patch
from accounts import Accounts
from server_network import public_request, browser_routes, PublicHTTPSConnection, validate_url
from site_verification import SiteVerification

PUBLIC = [(2, 1, 6, '', ('93.184.216.34', 443))]


class SiteVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'FM_ENCRYPTION_KEY':secrets.token_hex(32), 'FM_ALLOWED_FORM_HOSTS':'legacy.example'})
        self.env.start(); self.addCleanup(self.env.stop)
        dns = patch('server_network.socket.getaddrinfo', return_value=PUBLIC)
        dns.start(); self.addCleanup(dns.stop)
        self.accounts = Accounts(Path(self.temp.name))
        self.sites = SiteVerification(self.accounts)
        self.user = dict(id='a'*32, owner=False)
        self.other = dict(id='b'*32, owner=False)
        self.url = 'https://customer.example/contact/'

    def test_account_bound_html_proof_survives_restart_and_does_not_grant_other_hosts(self):
        value = self.sites.challenge(self.user, self.url)
        self.assertFalse(value['verified'])
        self.assertNotEqual(value['token'], self.sites.challenge(self.other, self.url)['token'])
        with patch('site_verification.public_request', side_effect=[(404, {}, b''), (200, {}, value['snippet'].encode())]):
            self.assertTrue(self.sites.verify(self.user, self.url)['verified'])
        SiteVerification(self.accounts).require(self.user, self.url)
        for user, url in ((self.other, self.url), (self.user, 'https://www.customer.example/contact/')):
            with self.assertRaises(ValueError): self.sites.require(user, url)

    def test_file_proof_exact_match_and_no_redirect_trust(self):
        token = self.sites.challenge(self.user, self.url)['token']
        with patch('site_verification.public_request', side_effect=[(301, {'Location':'http://127.0.0.1/'}, token.encode()), (200, {}, ('<!--sentleaf-verification='+token+'-->').encode())]) as request:
            with self.assertRaises(ValueError): self.sites.verify(self.user, self.url)
            self.assertEqual(request.call_count, 2)
        self.sites.attempts.clear()
        with patch('site_verification.public_request', return_value=(200, {}, (token+'\n').encode())):
            self.assertTrue(self.sites.verify(self.user, self.url)['verified'])

    def test_legacy_sites_are_owner_only(self):
        self.sites.require(dict(self.user, owner=True), 'https://legacy.example/')
        with self.assertRaises(ValueError): self.sites.require(self.user, 'https://legacy.example/')

    def test_rejects_private_mixed_dns_and_credentials(self):
        for url in ('http://customer.example/', 'https://a:b@customer.example/', 'https://customer.example:8443/', 'https://customer.example/\nsecret'):
            with self.assertRaises(ValueError): validate_url(url)
        for ip in ('127.0.0.1', '10.1.2.3', '169.254.169.254', '::1', 'fd00::1', '100.64.0.1'):
            with patch('server_network.socket.getaddrinfo', return_value=PUBLIC+[(2,1,6,'',(ip,443))]):
                with self.assertRaises(ValueError): public_request(self.url)

    def test_socket_uses_validated_address_with_original_tls_name(self):
        connection = PublicHTTPSConnection('customer.example', '93.184.216.34')
        connection._context = Mock()
        with patch('server_network.socket.create_connection') as socket:
            connection.connect()
            socket.assert_called_once_with(('93.184.216.34',443), 10)
            connection._context.wrap_socket.assert_called_once_with(socket.return_value, server_hostname='customer.example')

    def test_transport_does_not_follow_redirect_or_allow_oversized_response(self):
        with patch('server_network.PublicHTTPSConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status=302; response.getheaders.return_value=[('Location','http://127.0.0.1')]
            response.read.return_value=b'redirect'
            self.assertEqual(public_request(self.url)[0],302)
            connection.assert_called_once_with('customer.example','93.184.216.34')
            response.read.return_value=b'12345'
            with self.assertRaises(ValueError): public_request(self.url, limit=4)

    def test_browser_allows_assets_but_blocks_cross_site_submission_and_navigation(self):
        route = Mock(); route.request.url='https://cdn.example/asset.js'; route.request.method='GET'
        route.request.post_data_buffer=None; route.request.all_headers.return_value={}
        route.request.is_navigation_request.return_value=False
        policy=browser_routes(self.url)
        with patch('server_network.public_request', return_value=(200, {}, b'asset')) as transport:
            policy(route); route.fulfill.assert_called_once(); route.abort.assert_not_called()
            route.request.method='POST'; policy(route)
            route.abort.assert_called_once(); self.assertEqual(transport.call_count,1)
            route.request.method='GET'; route.request.is_navigation_request.return_value=True
            policy(route); self.assertEqual(transport.call_count,1)
            route.request.url='https://customer.example/wp-json/contact-form-7/v1/contact-forms/1/feedback'
            route.request.method='POST'; policy(route); self.assertEqual(transport.call_count,2)


if __name__ == '__main__': unittest.main()
