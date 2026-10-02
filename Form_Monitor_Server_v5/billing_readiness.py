"""Read-only Stripe deployment checks. Never return credentials or account PII."""
import json
import logging
import os
import secrets
import string
import time
import stripe
from billing import PLANS, EVENTS


def inspect_billing(billing):
    result = dict(mode=billing.mode(), enabled=billing.enabled(),
                  key_present=bool(billing.key()),
                  webhook_secret_present=bool(os.environ.get('STRIPE_WEBHOOK_SECRET')),
                  prices={}, errors={})
    if not billing.key():
        return result
    client = stripe.StripeClient(billing.key(), max_network_retries=0,
                                http_client=stripe.RequestsClient(timeout=8))

    def read(name, function):
        try:
            value = function()
            return value.to_dict() if hasattr(value, 'to_dict') else value
        except stripe.StripeError as error:
            # Status/class only: provider error text may contain request values.
            result['errors'][name] = dict(type=type(error).__name__, status=error.http_status)
            return None

    account = read('account', lambda: client.v1.accounts.retrieve_current())
    if account is not None:
        result['account'] = {key: bool(account.get(key)) for key in ('charges_enabled', 'payouts_enabled', 'details_submitted')}
        result['account']['requirements_due'] = len((account.get('requirements') or {}).get('currently_due') or [])
    for plan, expected in PLANS.items():
        price_id = billing.price_id(plan)
        result['prices'][plan] = dict(configured=bool(price_id), valid=False)
        if not price_id:
            continue
        price = read('price_' + plan, lambda: client.v1.prices.retrieve(price_id))
        if price is not None:
            recurring = price.get('recurring') or {}
            result['prices'][plan]['valid'] = bool(price.get('active') and price.get('currency') == 'eur'
                and price.get('unit_amount') == expected['amount'] and recurring.get('interval') == 'month'
                and recurring.get('interval_count') == 1 and price.get('livemode') == (billing.mode() == 'live'))
    endpoints = read('webhooks', lambda: client.v1.webhook_endpoints.list({'limit':100}))
    if endpoints is not None:
        result['webhook_endpoint_ready'] = any(e.get('url') == billing.origin + '/api/billing/webhook'
            and e.get('status') == 'enabled'
            and ('*' in e.get('enabled_events', []) or EVENTS.issubset(e.get('enabled_events', []))) for e in endpoints.get('data', []))
    portal = read('portal', lambda: client.v1.billing_portal.configurations.list({'limit':100, 'active':True}))
    if portal is not None:
        result['portal_ready'] = any(p.get('is_default') and
            (p.get('features') or {}).get('subscription_cancel', {}).get('enabled') for p in portal.get('data', []))
    return result


def log_readiness(billing):
    try:
        logging.getLogger(__name__).warning('Billing readiness: %s', json.dumps(inspect_billing(billing), sort_keys=True))
    except Exception as error:
        logging.getLogger(__name__).warning('Billing readiness could not finish: %s', type(error).__name__)


def log_network_readiness(origin):
    """Exercise the production browser transport on our own public login page only."""
    from playwright.sync_api import sync_playwright
    from server_network import browser_routes
    stage = 'browser_start'
    observations = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=['--disable-dev-shm-usage',
                '--host-resolver-rules=MAP * ~NOTFOUND', '--disable-quic',
                '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
            try:
                context = browser.new_context(service_workers='block')
                context.route('**/*', browser_routes(origin))
                context.route_web_socket('**/*', lambda ws: ws.close())
                page = context.new_page()
                from urllib.parse import urlsplit
                page.on('response', lambda response: observations.append((urlsplit(response.url).path, response.status)))
                page.on('requestfailed', lambda request: observations.append((urlsplit(request.url).path, request.failure)))
                page.on('pageerror', lambda error: observations.append(('javascript', str(error)[:180])))
                stage = 'public_page'
                response = page.goto(origin + '/login', wait_until='domcontentloaded', timeout=30000)
                stage = 'javascript_and_api'
                page.wait_for_function("document.querySelector('#local-note')?.textContent.includes('Create your own account')", timeout=15000)
                logging.getLogger(__name__).warning('Network readiness: public HTTPS page=%s; browser JavaScript and session API loaded.', response.status)
            finally:
                browser.close()
    except Exception as error:
        logging.getLogger(__name__).warning('Network readiness failed at %s: %s; resources=%s', stage, type(error).__name__, json.dumps(observations[:20]))


def checkout_preflight(billing):
    """Create and immediately expire an unpaid session. No customer or card data."""
    if not all(billing.price_id(plan) for plan in PLANS):
        raise ValueError('Prices must be configured before the checkout preflight.')
    client = stripe.StripeClient(billing.key(), max_network_retries=1,
                                http_client=stripe.RequestsClient(timeout=8))
    label = 'sentleaf_probe_' + ''.join(secrets.choice(string.ascii_lowercase) for _ in range(8))
    session = client.v1.checkout.sessions.create(dict(mode='subscription',
        integration_identifier=label, line_items=[dict(price=billing.price_id('solo'),quantity=1)],
        success_url=billing.origin+'/workspace#billing/return', cancel_url=billing.origin+'/workspace#billing',
        billing_address_collection='required',tax_id_collection={'enabled':True},
        automatic_tax={'enabled':os.environ.get('STRIPE_AUTOMATIC_TAX')=='1'},
        metadata={'sentleaf_purpose':'launch_preflight_no_charge'}, expires_at=int(time.time())+1860),
        options={'idempotency_key':label}).to_dict()
    result = dict(created=True, mode='live' if session.get('livemode') else 'test', expired=False)
    try:
        result['expired'] = client.v1.checkout.sessions.expire(session['id']).to_dict().get('status') == 'expired'
    except stripe.StripeError:
        # No URL is published and it automatically expires in 31 minutes.
        result['expires_automatically'] = True
    return result


def log_checkout_preflight(billing):
    try:
        logging.getLogger(__name__).warning('Checkout preflight: %s', json.dumps(checkout_preflight(billing)))
    except stripe.StripeError as error:
        logging.getLogger(__name__).warning('Checkout preflight failed: %s; status=%s; code=%s', type(error).__name__,error.http_status,error.code)
    except Exception as error:
        logging.getLogger(__name__).warning('Checkout preflight failed: %s',type(error).__name__)
