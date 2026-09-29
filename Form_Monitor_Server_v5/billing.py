"""Stripe-backed entitlements. Browser redirects never grant paid access."""
import logging
import os
import threading
import time
import uuid
from urllib.parse import urlsplit

import stripe

PLANS = {
    'solo': dict(name='Solo', amount=500, currency='eur', monitors=1, interval=360,
                 history_days=7, groups=False, csv_export=False, weekly_report=False),
    'studio': dict(name='Studio', amount=1200, currency='eur', monitors=5, interval=360,
                   history_days=90, groups=True, csv_export=True, weekly_report=True),
}
TRIAL_DAYS = 7
TERMINAL = {'canceled', 'incomplete_expired'}
EVENTS = {'checkout.session.completed', 'checkout.session.async_payment_succeeded',
          'customer.subscription.created', 'customer.subscription.updated',
          'customer.subscription.deleted', 'invoice.paid', 'invoice.payment_failed'}
stripe.default_http_client = stripe.RequestsClient(timeout=10)


class BillingUnavailable(ValueError):
    pass


class DailyLimit(ValueError):
    pass


class BillingAccessError(ValueError):
    pass


class Billing:
    def __init__(self, accounts, origin):
        self.accounts, self.origin = accounts, origin
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        self.price_cache = {}
        with accounts.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS billing (
                user_id TEXT PRIMARY KEY, customer_id TEXT UNIQUE,
                subscription_id TEXT, status TEXT NOT NULL DEFAULT 'none',
                plan TEXT NOT NULL DEFAULT '', period_end REAL NOT NULL DEFAULT 0,
                cancel_at REAL NOT NULL DEFAULT 0, trial_end REAL NOT NULL DEFAULT 0,
                synced REAL NOT NULL DEFAULT 0, checkout_id TEXT,
                checkout_key TEXT, checkout_plan TEXT, checkout_started REAL)''')
            db.execute('CREATE TABLE IF NOT EXISTS billing_events (id TEXT PRIMARY KEY, received REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS billing_usage (user_id TEXT, day INTEGER, runs INTEGER NOT NULL, PRIMARY KEY(user_id,day))')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(billing)')}
            if 'stripe_mode' not in columns:
                db.execute("ALTER TABLE billing ADD COLUMN stripe_mode TEXT NOT NULL DEFAULT 'legacy'")
            db.execute('''CREATE TABLE IF NOT EXISTS billing_mode_archive (
                user_id TEXT, stripe_mode TEXT, record TEXT, archived REAL)''')

    @staticmethod
    def trials_enabled():
        return os.environ.get('FM_TRIALS_ENABLED', '1') == '1'

    @classmethod
    def mode(cls):
        return 'live' if cls.key().startswith(('sk_live_', 'rk_live_')) else 'test'

    @staticmethod
    def enabled():
        return os.environ.get('FM_BILLING_ENABLED') == '1'

    @staticmethod
    def key():
        return os.environ.get('STRIPE_SECRET_KEY', '').strip()

    @staticmethod
    def price_id(plan):
        return os.environ.get('STRIPE_PRICE_' + plan.upper(), '').strip()

    @classmethod
    def configured(cls):
        return bool(cls.enabled() and cls.key() and os.environ.get('STRIPE_WEBHOOK_SECRET')
                    and all(cls.price_id(p) for p in PLANS))

    @classmethod
    def catalog(cls):
        return dict(plans=[dict(id=k, **v) for k, v in PLANS.items()], trial_days=TRIAL_DAYS,
                    checkout_ready=cls.configured(), billing_enabled=cls.enabled(),
                    trials_enabled=cls.trials_enabled(), access_review_required=os.environ.get('FM_PUBLIC_REGISTRATION')!='1',
                    test_mode=not cls.key().startswith(('sk_live_', 'rk_live_')))

    def row(self, user_id):
        with self.accounts.connect() as db:
            db.execute('INSERT OR IGNORE INTO billing (user_id,stripe_mode) VALUES (?,?)', (user_id,self.mode()))
            return dict(db.execute('SELECT * FROM billing WHERE user_id=?', (user_id,)).fetchone())

    def update(self, user_id, **values):
        # Column names are internal constants, never request keys.
        with self.accounts.connect() as db:
            db.execute('UPDATE billing SET ' + ','.join(k+'=?' for k in values) + ' WHERE user_id=?',
                       (*values.values(), user_id))

    def state(self, user):
        row = self.row(user['id'])
        now = time.time()
        if user['owner']:
            plan, label, limit, interval, active = 'owner', 'Owner workspace', 20, 15, True
        elif not self.enabled() and not self.trials_enabled():
            plan, label, limit, interval, active = 'beta', 'Complimentary access', 20, 15, True
        elif row['stripe_mode'] == self.mode() and row['status'] in ('active', 'trialing') and row['plan'] in PLANS and row['period_end'] > now:
            plan = row['plan']; label = PLANS[plan]['name']
            limit, interval, active = PLANS[plan]['monitors'], PLANS[plan]['interval'], True
        elif row['status'] == 'none' and row['trial_end'] > now:
            plan, label, limit, interval, active = 'trial', '7-day trial', 1, 360, True
        else:
            plan, label, limit, interval, active = '', 'Choose a plan', 0, 360, False
        day = int(now // 86400)
        with self.accounts.connect() as db:
            usage = db.execute('SELECT runs FROM billing_usage WHERE user_id=? AND day=?', (user['id'],day)).fetchone()
        features = PLANS.get(plan, PLANS['studio'] if plan in ('owner','beta') else PLANS['solo'])
        # Existing history remains readable after expiry, within the last plan's window.
        history_days = PLANS.get(row['plan'], features)['history_days'] if not active else features['history_days']
        return dict(plan=plan, label=label, monitor_limit=limit, minimum_interval=interval,
                    history_days=history_days,
                    features={k:bool(active and features[k]) for k in ('groups','csv_export','weekly_report')},
                    daily_limit=limit*6 if plan not in ('owner','beta') else None,
                    runs_today=usage['runs'] if usage else 0,
                    active=active, status=row['status'], period_end=row['period_end'],
                    cancel_at=row['cancel_at'], trial_end=row['trial_end'],
                    trial_available=(self.enabled() or self.trials_enabled()) and not user['owner'] and not row['trial_end']
                                    and row['status'] == 'none',
                    can_manage=bool(row['customer_id'] and self.configured()), **self.catalog())

    def start_trial(self, user):
        if not (self.enabled() or self.trials_enabled()) or user['owner']:
            raise ValueError('This workspace already has access.')
        with self.lock:
            row = self.row(user['id'])
            if row['trial_end'] or row['status'] != 'none':
                raise ValueError('The trial is available once per account.')
            self.update(user['id'], trial_end=time.time()+TRIAL_DAYS*86400)
        return self.state(user)

    def check(self, user, profiles, *, data=None, profile=None):
        access = self.state(user)
        if not access['active']:
            raise BillingAccessError('Monitoring is paused. Open Plan & billing to start a trial or manage your subscription.')
        active = [p for p in profiles if not p.get('deleted')]
        limit = access['monitor_limit']
        if data is not None:
            if not data.get('id') and len(active) >= limit:
                raise ValueError('Your plan limit is reached. Remove a monitor or change your plan in Plan & billing.')
            if type(data.get('interval')) is not int or data['interval'] < access['minimum_interval']:
                raise ValueError('This plan supports checks every 6 hours or less often.' if not user['owner']
                                 else 'Choose an interval of at least 15 minutes.')
        if profile is not None:
            if profile['id'] not in {p['id'] for p in active[:limit]}:
                raise BillingAccessError('This monitor exceeds your current plan limit. Remove another monitor or change your plan.')
            if profile['interval'] < access['minimum_interval']:
                raise BillingAccessError('Update this monitor to an interval allowed by your current plan.')

    def api(self, function, **params):
        try:
            result = function(api_key=self.key(), max_network_retries=1, **params)
            return result.to_dict()
        except stripe.StripeError as error:
            logging.getLogger(__name__).error('Billing provider request failed (%s).', type(error).__name__)
            raise BillingUnavailable('Billing is temporarily unavailable. No access has been changed; please try again.') from None

    def reserve_run(self, user):
        with self.lock:
            access = self.state(user)
            if not access['active']:
                raise BillingAccessError('Open Plan & billing to activate monitoring.')
            if access['daily_limit'] is None:
                return
            day = int(time.time() // 86400)
            with self.accounts.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR IGNORE INTO billing_usage VALUES (?,?,0)', (user['id'],day))
                used = db.execute('SELECT runs FROM billing_usage WHERE user_id=? AND day=?', (user['id'],day)).fetchone()['runs']
                if used >= access['daily_limit']:
                    raise DailyLimit('Today’s check allowance is used. It resets at 00:00 UTC; scheduled checks will resume automatically.')
                db.execute('UPDATE billing_usage SET runs=runs+1 WHERE user_id=? AND day=?', (user['id'],day))
                db.execute('DELETE FROM billing_usage WHERE day<?', (day-32,))

    def ensure_ready(self):
        if not self.configured():
            raise BillingUnavailable('Payments are not open yet. Your existing workspace access is unchanged.')

    def ensure_customer_mode(self, user_id):
        """Never carry sandbox entitlements or customer IDs into live Checkout."""
        import json
        row = self.row(user_id)
        if row['stripe_mode'] == self.mode():
            return row
        # Restore a previously identified mode instead of creating duplicate customers
        # when a deployment switches from live to test and back.
        with self.accounts.connect() as db:
            saved = db.execute('''SELECT record FROM billing_mode_archive
                WHERE user_id=? AND stripe_mode=? ORDER BY archived DESC LIMIT 1''',
                (user_id,self.mode())).fetchone()
            if saved:
                db.execute('INSERT INTO billing_mode_archive VALUES (?,?,?,?)',
                           (user_id,row['stripe_mode'],json.dumps(row),time.time()))
                restored = json.loads(saved['record'])
                restored.pop('user_id',None)
                # A free trial belongs to the account, across provider modes.
                restored['trial_end'] = max(row['trial_end'],restored.get('trial_end',0))
                db.execute('UPDATE billing SET '+','.join(k+'=?' for k in restored)+' WHERE user_id=?',
                           (*restored.values(),user_id))
                return dict(db.execute('SELECT * FROM billing WHERE user_id=?',(user_id,)).fetchone())
        if row['customer_id']:
            # A legacy record must be identified, not guessed from the current API key.
            try:
                customer = stripe.Customer.retrieve(row['customer_id'], api_key=self.key(), max_network_retries=1)
            except stripe.InvalidRequestError as error:
                if error.code != 'resource_missing':
                    raise BillingUnavailable('The existing billing profile needs review.') from None
            except stripe.StripeError:
                raise BillingUnavailable('Could not verify the existing billing profile. Please try again.') from None
            else:
                if not customer.get('deleted') and bool(customer.get('livemode')) == (self.mode() == 'live'):
                    self.update(user_id, stripe_mode=self.mode())
                    return self.row(user_id)
                raise BillingUnavailable('The existing billing profile needs review.')
        with self.accounts.connect() as db:
            db.execute('INSERT INTO billing_mode_archive VALUES (?,?,?,?)',
                       (user_id,row['stripe_mode'],json.dumps(row),time.time()))
        self.update(user_id, customer_id=None, subscription_id=None, status='none', plan='',
                    period_end=0, cancel_at=0, synced=0, checkout_id=None, checkout_key=None,
                    checkout_plan=None, checkout_started=None, stripe_mode=self.mode())
        return self.row(user_id)

    def validate_price(self, plan):
        price_id = self.price_id(plan)
        if self.price_cache.get(price_id, 0) > time.time():
            return
        price = self.api(stripe.Price.retrieve, id=price_id)
        recurring = price.get('recurring') or {}
        expected = PLANS[plan]
        if (not price.get('active') or price.get('currency') != expected['currency']
                or price.get('unit_amount') != expected['amount']
                or recurring.get('interval') != 'month' or recurring.get('interval_count') != 1
                or price.get('livemode') != self.key().startswith(('sk_live_', 'rk_live_'))):
            raise BillingUnavailable('The payment plan needs configuration. Please contact support before paying.')
        self.price_cache[price_id] = time.time()+300

    @staticmethod
    def hosted_url(url, hostname):
        parsed = urlsplit(url or '')
        if parsed.scheme != 'https' or parsed.hostname != hostname or parsed.username or parsed.port not in (None, 443):
            raise BillingUnavailable('The billing provider returned an unexpected address.')
        return url

    def sync(self, user_id):
        self.ensure_ready()
        with self.lock:
            row = self.ensure_customer_mode(user_id)
            if not row['customer_id']:
                return row
            result = self.api(stripe.Subscription.list, customer=row['customer_id'], status='all', limit=100)
            subscriptions = result.get('data', [])
            current = [s for s in subscriptions if s.get('status') not in TERMINAL]
            if len(current) > 1 or result.get('has_more'):
                raise BillingUnavailable('More than one subscription needs review. Please contact support.')
            sub = current[0] if current else max(subscriptions, key=lambda s:s.get('created', 0), default=None)
            values = dict(subscription_id=None, status='none', plan='', period_end=0, cancel_at=0, synced=time.time())
            if sub:
                items = (sub.get('items') or {}).get('data', [])
                item = items[0] if len(items) == 1 else {}
                price = item.get('price') or {}
                plan = next((key for key in PLANS if self.price_id(key) == price.get('id')), '')
                if item.get('quantity') != 1:
                    plan = ''
                end = item.get('current_period_end') or sub.get('current_period_end') or 0
                values.update(subscription_id=sub['id'], status=sub['status'], plan=plan,
                              period_end=end, cancel_at=sub.get('cancel_at') or (end if sub.get('cancel_at_period_end') else 0))
            self.update(user_id, **values)
            return self.row(user_id)

    def checkout(self, user, plan):
        self.ensure_ready()
        if user['owner']:
            raise ValueError('The owner workspace does not need a paid subscription.')
        if plan not in PLANS:
            raise ValueError('Choose one of the available plans.')
        with self.lock:
            self.validate_price(plan)
            row = self.ensure_customer_mode(user['id'])
            if not row['customer_id']:
                customer = self.api(stripe.Customer.create, email=user['email'], metadata={'sentleaf_user':user['id']},
                                    idempotency_key='sentleaf-customer-'+user['id'])
                self.update(user['id'], customer_id=customer['id'])
            row = self.sync(user['id'])
            if row['status'] not in TERMINAL | {'none'}:
                raise ValueError('You already have a subscription. Use Manage subscription to make changes.')
            if row['checkout_id']:
                session = self.api(stripe.checkout.Session.retrieve, id=row['checkout_id'])
                if session.get('status') == 'complete' and row['status'] not in TERMINAL:
                    self.sync(user['id'])
                    raise ValueError('Your previous payment is being confirmed. Refresh Plan & billing shortly.')
                if session.get('status') == 'open':
                    if row['checkout_plan'] == plan:
                        return self.hosted_url(session.get('url'), 'checkout.stripe.com')
                    self.api(stripe.checkout.Session.expire, id=row['checkout_id'])
                self.update(user['id'], checkout_id=None, checkout_key=None)
                row = self.row(user['id'])
            # Persist the idempotency key before the network call, including timeouts.
            if row['checkout_key'] and row['checkout_plan'] != plan:
                raise ValueError('Finish opening your previous plan first, then return to change it.')
            if row['checkout_key'] and time.time() - (row['checkout_started'] or 0) > 23*3600:
                raise BillingUnavailable('An earlier payment attempt needs review. Contact support to avoid a duplicate subscription.')
            if not row['checkout_key']:
                self.update(user['id'], checkout_key=uuid.uuid4().hex, checkout_plan=plan, checkout_started=time.time())
                row = self.row(user['id'])
            session = self.api(stripe.checkout.Session.create, mode='subscription', customer=row['customer_id'],
                client_reference_id=user['id'], line_items=[{'price':self.price_id(plan), 'quantity':1}],
                subscription_data={'metadata':{'sentleaf_user':user['id']}},
                success_url=self.origin+'/workspace#billing/return', cancel_url=self.origin+'/workspace#billing',
                billing_address_collection='required', tax_id_collection={'enabled':True},
                automatic_tax={'enabled':os.environ.get('STRIPE_AUTOMATIC_TAX') == '1'},
                customer_update={'address':'auto','name':'auto'},
                idempotency_key='sentleaf-checkout-'+row['checkout_key'])
            self.update(user['id'], checkout_id=session['id'])
            return self.hosted_url(session.get('url'), 'checkout.stripe.com')

    def portal(self, user):
        self.ensure_ready()
        row = self.ensure_customer_mode(user['id'])
        if not row['customer_id']:
            raise ValueError('This account has no billing profile yet.')
        session = self.api(stripe.billing_portal.Session.create, customer=row['customer_id'],
                           return_url=self.origin+'/workspace#billing/return')
        return self.hosted_url(session.get('url'), 'billing.stripe.com')

    def webhook(self, raw, signature):
        self.ensure_ready()
        try:
            event = stripe.Webhook.construct_event(raw, signature, os.environ['STRIPE_WEBHOOK_SECRET'], tolerance=300).to_dict()
        except (ValueError, stripe.SignatureVerificationError):
            raise ValueError('Invalid webhook signature.') from None
        if event.get('livemode') != self.key().startswith(('sk_live_', 'rk_live_')):
            raise ValueError('Incorrect webhook mode.')
        if event.get('type') not in EVENTS:
            return
        with self.lock:
            with self.accounts.connect() as db:
                if db.execute('SELECT 1 FROM billing_events WHERE id=?', (event['id'],)).fetchone():
                    return
                customer = event['data']['object'].get('customer')
                row = db.execute('SELECT user_id FROM billing WHERE customer_id=?', (customer,)).fetchone()
            if row:
                # Fetch current provider state: delivery order and event snapshots are not trusted for entitlements.
                self.sync(row['user_id'])
            with self.accounts.connect() as db:
                db.execute('INSERT OR IGNORE INTO billing_events VALUES (?,?)', (event['id'], time.time()))
                db.execute('DELETE FROM billing_events WHERE received<?', (time.time()-90*86400,))

    def reconcile(self):
        while not self.stop.wait(600):
            if not self.configured():
                continue
            with self.accounts.connect() as db:
                users = db.execute('SELECT user_id FROM billing WHERE customer_id IS NOT NULL').fetchall()
            for user in users:
                if self.stop.is_set():
                    break
                try:
                    self.sync(user['user_id'])
                except Exception:
                    logging.getLogger(__name__).error('Subscription reconciliation failed; provider retry is pending.')

    def start(self):
        self.worker = threading.Thread(target=self.reconcile, daemon=True, name='billing-reconciliation')
        self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=22)
