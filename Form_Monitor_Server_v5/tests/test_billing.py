"""Payment safety, persisted access and signed callbacks; no real provider calls."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stripe
from accounts import Accounts
from billing import Billing, BillingUnavailable, BillingAccessError, DailyLimit


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            'FM_BILLING_ENABLED':'1', 'STRIPE_SECRET_KEY':'sk_test_fixture',
            'STRIPE_WEBHOOK_SECRET':'whsec_fixture',
            'STRIPE_PRICE_SOLO':'price_solo', 'STRIPE_PRICE_STUDIO':'price_studio'}, clear=False)
        self.env.start(); self.addCleanup(self.env.stop)
        self.accounts = Accounts(Path(self.temp.name))
        self.billing = Billing(self.accounts, 'https://beta.example')
        self.user = dict(id='a'*32, email='user@example.com', owner=False)
        self.other = dict(id='b'*32, email='other@example.com', owner=False)
        self.billing.row(self.user['id'])
        # A missing mock fails immediately rather than reaching the network.
        self.network = patch.object(self.billing, 'api', side_effect=AssertionError('Unexpected provider request'))
        self.api = self.network.start(); self.addCleanup(self.network.stop)

    def provider(self, **overrides):
        result = dict(id='sub_test', status='active', customer='cus_test', created=10,
                      items={'data':[{'quantity':1, 'price':{'id':'price_solo'}, 'current_period_end':time.time()+86400}]})
        result.update(overrides)
        return result

    def sync(self, subscriptions):
        self.billing.update(self.user['id'], customer_id='cus_test')
        self.api.side_effect = None
        self.api.return_value = {'data':subscriptions, 'has_more':False}
        return self.billing.sync(self.user['id'])

    def signed(self, event, timestamp=None):
        raw = json.dumps(event).encode()
        timestamp = int(time.time()) if timestamp is None else timestamp
        signature = hmac.new(b'whsec_fixture', str(timestamp).encode()+b'.'+raw, hashlib.sha256).hexdigest()
        return raw, f't={timestamp},v1={signature}'

    def event(self, **override):
        return dict({'id':'evt_test','type':'customer.subscription.updated','livemode':False,
                     'data':{'object':{'customer':'cus_test','status':'active'}}}, **override)

    def test_disabled_preserves_beta_and_owner_access(self):
        with patch.dict(os.environ, {'FM_BILLING_ENABLED':'0','FM_TRIALS_ENABLED':'0'}):
            self.assertTrue(self.billing.state(self.user)['active'])
            self.assertEqual(self.billing.state(self.user)['plan'], 'beta')
            with self.assertRaises(BillingUnavailable):self.billing.checkout(self.user, 'solo')
        self.assertEqual(self.billing.state(dict(self.user, owner=True))['plan'], 'owner')

    def test_sdk_resources_are_converted_recursively(self):
        result = stripe.StripeObject.construct_from({'data':[{'id':'sub_1','items':{'data':[{'quantity':1}]}}]}, 'sk_test_fixture')
        value = Billing.api(self.billing, lambda **kwargs: result)
        self.assertEqual(value.get('data')[0].get('items').get('data')[0].get('quantity'), 1)

    def test_trial_once_persists_and_expires_without_renewing(self):
        self.assertFalse(self.billing.state(self.user)['active'])
        state = self.billing.start_trial(self.user)
        self.assertTrue(state['active']); self.assertEqual(state['monitor_limit'], 1)
        again = Billing(self.accounts, 'https://beta.example')
        self.assertEqual(again.state(self.user)['trial_end'], state['trial_end'])
        with self.assertRaises(ValueError):again.start_trial(self.user)
        self.billing.update(self.user['id'], trial_end=time.time()-1)
        self.assertFalse(self.billing.state(self.user)['active'])
        self.assertFalse(self.billing.state(self.user)['trial_available'])
        self.api.assert_not_called()

    def test_plan_limits_are_enforced_at_save_and_run(self):
        self.billing.start_trial(self.user)
        profile = dict(id='monitor1', interval=360)
        with self.assertRaises(ValueError):self.billing.check(self.user, [profile], data={'interval':360})
        with self.assertRaises(ValueError):self.billing.check(self.user, [], data={'interval':15})
        with self.assertRaises(BillingAccessError):self.billing.check(self.user, [profile,dict(id='monitor2',interval=360)], profile=dict(id='monitor2',interval=360))
        self.billing.check(self.user, [profile], data=dict(id='monitor1',interval=360))

    def test_daily_allowance_is_persistent_and_user_scoped(self):
        self.billing.start_trial(self.user); self.billing.start_trial(self.other)
        for _ in range(6):self.billing.reserve_run(self.user)
        with self.assertRaises(DailyLimit):self.billing.reserve_run(self.user)
        self.billing.reserve_run(self.other)
        self.assertEqual(self.billing.state(self.other)['runs_today'],1)
        self.assertEqual(Billing(self.accounts,'https://beta.example').state(self.user)['runs_today'],6)

    def test_cancellation_keeps_access_until_period_end(self):
        self.sync([self.provider(cancel_at_period_end=True)])
        state = self.billing.state(self.user)
        self.assertTrue(state['active']); self.assertGreater(state['cancel_at'],time.time())
        self.billing.update(self.user['id'],period_end=time.time()-1)
        self.assertFalse(self.billing.state(self.user)['active'])

    def test_failed_payment_canceled_unknown_price_and_wrong_quantity_do_not_grant_access(self):
        for status in ('past_due','unpaid','incomplete','canceled','paused','incomplete_expired'):
            with self.subTest(status=status):
                self.sync([self.provider(status=status)])
                self.assertFalse(self.billing.state(self.user)['active'])
        for item in ({'quantity':1,'price':{'id':'unknown'}}, {'quantity':2,'price':{'id':'price_solo'}}):
            item['current_period_end']=time.time()+86400
            self.sync([self.provider(items={'data':[item]})])
            self.assertFalse(self.billing.state(self.user)['active'])

    def test_duplicate_subscriptions_require_review(self):
        with self.assertRaises(BillingUnavailable):self.sync([self.provider(),self.provider(id='sub_two')])

    def test_signed_webhook_reconciles_current_state_not_old_event_data(self):
        self.billing.update(self.user['id'],customer_id='cus_test')
        self.api.side_effect=None
        self.api.return_value={'data':[self.provider(status='canceled')],'has_more':False}
        raw, signature=self.signed(self.event())
        self.billing.webhook(raw,signature)
        self.assertFalse(self.billing.state(self.user)['active'])
        self.billing.webhook(raw,signature)
        self.assertEqual(self.api.call_count,1)

    def test_invalid_expired_and_wrong_mode_webhooks_rejected(self):
        raw,signature=self.signed(self.event())
        with self.assertRaises(ValueError):self.billing.webhook(raw,signature+'bad')
        raw,signature=self.signed(self.event(),int(time.time())-600)
        with self.assertRaises(ValueError):self.billing.webhook(raw,signature)
        raw,signature=self.signed(self.event(livemode=True))
        with self.assertRaises(ValueError):self.billing.webhook(raw,signature)
        self.api.assert_not_called()

    def test_failed_reconciliation_leaves_event_retryable(self):
        self.billing.update(self.user['id'],customer_id='cus_test')
        self.api.side_effect=BillingUnavailable('Unavailable')
        raw,signature=self.signed(self.event())
        with self.assertRaises(BillingUnavailable):self.billing.webhook(raw,signature)
        with self.accounts.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM billing_events').fetchone()[0],0)

    def test_checkout_retries_reuse_key_and_never_grant_access_from_redirect(self):
        self.billing.update(self.user['id'],customer_id='cus_test')
        self.api.side_effect=[{'data':[],'has_more':False},BillingUnavailable('timeout'),
                              {'data':[],'has_more':False},{'id':'cs_test','url':'https://checkout.stripe.com/c/pay/test'}]
        with patch.object(self.billing,'validate_price'):
            with self.assertRaises(BillingUnavailable):self.billing.checkout(self.user,'solo')
            first_key=self.billing.row(self.user['id'])['checkout_key']
            self.assertEqual(self.billing.checkout(self.user,'solo'),'https://checkout.stripe.com/c/pay/test')
        self.assertEqual(self.billing.row(self.user['id'])['checkout_key'],first_key)
        self.assertEqual(self.api.call_args_list[1].kwargs['idempotency_key'],self.api.call_args_list[3].kwargs['idempotency_key'])
        self.assertFalse(self.billing.state(self.user)['active'])
        self.assertEqual(self.api.call_args.kwargs['line_items'],[{'price':'price_solo','quantity':1}])

    def test_open_session_is_reused_and_existing_subscription_blocks_checkout(self):
        self.billing.update(self.user['id'],customer_id='cus_test',checkout_id='cs_open',checkout_plan='solo')
        self.api.side_effect=[{'data':[],'has_more':False}, {'status':'open','url':'https://checkout.stripe.com/c/pay/existing'}]
        with patch.object(self.billing,'validate_price'):
            self.assertIn('existing',self.billing.checkout(self.user,'solo'))
        self.api.side_effect=[{'data':[self.provider()],'has_more':False}]
        with patch.object(self.billing,'validate_price'), self.assertRaisesRegex(ValueError,'already have'):
            self.billing.checkout(self.user,'studio')

    def test_checkout_price_must_match_advertised_price_currency_mode_and_cadence(self):
        good=dict(active=True,currency='eur',unit_amount=500,livemode=False,recurring={'interval':'month','interval_count':1})
        for changed in ({'unit_amount':1000},{'currency':'usd'},{'livemode':True},{'active':False},{'recurring':{'interval':'year','interval_count':1}}):
            self.api.side_effect=None;self.api.return_value=dict(good,**changed)
            with self.subTest(changed=changed),self.assertRaises(BillingUnavailable):self.billing.validate_price('solo')
        self.api.return_value=good;self.billing.validate_price('solo')

    def test_only_stripe_hosted_redirects_and_own_customer_portal(self):
        for url in ('https://evil.example','http://checkout.stripe.com/pay','https://checkout.stripe.com.evil.example/pay','https://user@checkout.stripe.com/pay'):
            with self.assertRaises(BillingUnavailable):self.billing.hosted_url(url,'checkout.stripe.com')
        self.billing.update(self.user['id'],customer_id='cus_owner')
        self.api.side_effect=None;self.api.return_value={'url':'https://billing.stripe.com/p/session/test'}
        self.billing.portal(self.user)
        self.assertEqual(self.api.call_args.kwargs['customer'],'cus_owner')
        with self.assertRaises(ValueError):self.billing.portal(self.other)


if __name__ == '__main__':
    unittest.main()
