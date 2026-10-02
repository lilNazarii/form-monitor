import json
import unittest
from unittest.mock import Mock, patch
import stripe
from billing import EVENTS
from billing_readiness import inspect_billing, checkout_preflight


class BillingReadinessTests(unittest.TestCase):
    def test_unpaid_checkout_probe_expires_without_customer_or_payment_details(self):
        billing=Mock(origin='https://sentleaf.app');billing.key.return_value='rk_test_fixture'
        billing.price_id.side_effect=lambda plan:'price_'+plan
        with patch('billing_readiness.stripe.StripeClient') as constructor:
            sessions=constructor.return_value.v1.checkout.sessions
            sessions.create.return_value=stripe.checkout.Session.construct_from(dict(id='cs_fixture',livemode=False), 'rk_test_fixture')
            sessions.expire.return_value=stripe.checkout.Session.construct_from(dict(status='expired'),'rk_test_fixture')
            result=checkout_preflight(billing)
            sessions.expire.assert_called_once_with('cs_fixture')
            data=sessions.create.call_args.args[0]
            self.assertEqual(data['line_items'],[dict(price='price_solo',quantity=1)])
            for key in ('customer','customer_email','payment_method_types','payment_method'):
                self.assertNotIn(key,data)
        self.assertEqual(result,dict(created=True,mode='test',expired=True))

    def test_reports_configuration_without_secrets_or_customer_information(self):
        billing=Mock(origin='https://sentleaf.app')
        billing.mode.return_value='live';billing.enabled.return_value=False
        billing.key.return_value='rk_live_hidden';billing.price_id.side_effect=lambda p:'price_'+p
        with patch('billing_readiness.stripe.StripeClient') as constructor:
            client=constructor.return_value
            client.v1.accounts.retrieve_current.return_value=stripe.Account.construct_from(dict(charges_enabled=True,payouts_enabled=True,
                details_submitted=True, email='private@example.com',requirements={'currently_due':[]}), 'rk_test_fixture')
            client.v1.prices.retrieve.side_effect=lambda p:dict(active=True,currency='eur',unit_amount=500 if p=='price_solo' else 1200,
                livemode=True,recurring=dict(interval='month',interval_count=1))
            client.v1.webhook_endpoints.list.return_value={'data':[dict(url='https://sentleaf.app/api/billing/webhook',status='enabled',enabled_events=list(EVENTS))]}
            client.v1.billing_portal.configurations.list.return_value={'data':[dict(is_default=True,features={'subscription_cancel':{'enabled':True}})]}
            result=inspect_billing(billing)
        self.assertTrue(result['account']['charges_enabled'])
        self.assertTrue(result['prices']['studio']['valid'])
        self.assertTrue(result['webhook_endpoint_ready']);self.assertTrue(result['portal_ready'])
        self.assertFalse(result['enabled'])
        self.assertNotIn('private@example.com',json.dumps(result));self.assertNotIn('rk_live_hidden',json.dumps(result))

    def test_permission_errors_are_redacted_and_do_not_skip_remaining_checks(self):
        billing=Mock(origin='https://sentleaf.app');billing.mode.return_value='live'
        billing.enabled.return_value=False;billing.key.return_value='rk_live_hidden';billing.price_id.return_value=''
        with patch('billing_readiness.stripe.StripeClient') as constructor:
            client=constructor.return_value
            client.v1.accounts.retrieve_current.side_effect=stripe.PermissionError('Secret request info',http_status=403)
            client.v1.webhook_endpoints.list.return_value={'data':[]}
            client.v1.billing_portal.configurations.list.return_value={'data':[]}
            result=inspect_billing(billing)
        self.assertEqual(result['errors']['account']['status'],403)
        self.assertFalse(result['portal_ready']);self.assertFalse(result['prices']['solo']['configured'])
        self.assertNotIn('Secret request info',json.dumps(result))
