"""Read-only Stripe deployment checks. Never return credentials or account PII."""
import json
import logging
import os
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
            return function()
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
