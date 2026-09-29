# Sentleaf subscriptions

The integration is shipped **disabled by default**. The private-beta email and
website allowlists remain enforced. An owner workspace never needs to pay.
Do not enable public sign-up just because billing is configured: Gmail production
authorization, site ownership verification, customer-facing legal documents,
support contact, and external pilot checks are separate release requirements.

## Offer

| Plan | Monthly price before applicable tax | Monitored forms | Minimum interval | Daily check cycles |
| --- | --- | --- | --- | --- |
| Solo | EUR 5.00 | 1 | 6 hours | 6 |
| Studio | EUR 12.00 | 5 across sites | 6 hours | 30 |

The allowance includes manual and scheduled cycles; each cycle can include the
existing failure retry. It resets at 00:00 UTC. No setup fee. A single 7-day,
one-form trial is started explicitly after Gmail is connected. It requires no
card and never auto-converts. This is a low launch price, not a verified claim
of the lowest price in the entire market.

## Stripe account and sandbox

The owner must create the Stripe account and complete Stripe's requested
identity/business and payout details. Never put credentials into source control,
screenshots or chat. Store keys directly in Render's secret environment settings.

1. Start in Stripe's sandbox. Create two products, Solo and Studio, each with one
   monthly recurring EUR price from the table. Use quantity 1. Disable promotions
   initially. Any later price change must update `billing.PLANS` and the Price ID
   together; the server refuses Checkout if the advertised and provider prices
   do not match.
2. Configure the customer portal: invoices, payment method updates, cancellation
   **at the end of the current period**, and optional switching only between
   those two prices (quantity 1). Keep immediate/prorated changes disabled until
   the desired upgrade/downgrade behavior has been tested. Without switching,
   keep subscription updates disabled; cancellation and invoices still work.
3. Add an event destination for
   `https://sentleaf.app/api/billing/webhook`, using snapshot events:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `invoice.payment_failed`
4. Set these Render environment values without replacing unrelated settings:

| Key | Value |
| --- | --- |
| `STRIPE_SECRET_KEY` | Sandbox secret API key initially |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for this exact event destination |
| `STRIPE_PRICE_SOLO` | Solo monthly EUR 5 Price ID |
| `STRIPE_PRICE_STUDIO` | Studio monthly EUR 12 Price ID |
| `STRIPE_AUTOMATIC_TAX` | `0` until tax settings and obligations are reviewed; `1` only after configuration |
| `FM_BILLING_ENABLED` | `1` only in the environment being tested |

Enabling billing applies to all non-owner accounts in that environment. Test in
an isolated deployment first, or use explicitly prepared test accounts with no
live customer subscriptions. Never swap a paying production account to sandbox
keys. SDK: stripe-python 15.6.1; event payloads are verified, then the current
subscription is fetched, so event ordering and snapshots cannot grant access.

## Required provider integration checks before taking real money

Use a separate invited test account. Owner accounts are exempt from billing.

- Hosted Checkout succeeds with sandbox data; the webhook updates the correct
  customer; the return link by itself grants no access.
- Double-click/retry returns the same open Checkout; a timeout reuses the saved
  idempotency key. Changing plans expires the earlier open Checkout.
- A failed payment and required authentication behave correctly.
- Cancel in the portal; access remains until period end and then checks stop.
- Renewal and payment recovery resume saved schedules automatically.
- Retried and out-of-order webhooks reconcile the current provider state.
- The displayed EUR amount, tax handling, business identity, cancellation terms
  and receipts match the actual Checkout offer.

The automated suite uses signed synthetic webhook payloads and simulated API
responses. It is not a substitute for these sandbox checks.

## Activate live payments

Complete account verification and the launch requirements above. Create the
live products/prices, live webhook and portal configuration separately. Put the
live key, live price IDs and live signing secret into Render together. Confirm
the public catalog has `checkout_ready=true` and `test_mode=false`, then verify
with the owner's explicit authorization before any real charge.

## Operations and rollback

Billing records are in `accounts.sqlite3`, included in existing encrypted
backups. No card details are stored by Sentleaf. Customer IDs are bound to the
authenticated account, never supplied by the browser. Webhook signatures have
a five-minute tolerance; accepted event IDs are retained for 90 days. A
ten-minute reconciliation loop repairs missed delivery. It grants no access
past a recorded period end when the provider cannot be reached.

Disabled billing with FM_TRIALS_ENABLED=0 preserves the legacy private beta; it does **not** cancel
Stripe subscriptions. Do not use `FM_BILLING_ENABLED=0` as a cancellation tool.
If rollback is required after live sales, retain the webhook/reconciliation
service or resolve subscriptions in Stripe before disabling it.

Daily usage is retained for 32 days. At quota exhaustion a saved schedule waits
until the next UTC day. Inactive or reduced-plan schedules keep their saved
intent and are retried every ten minutes; no submission runs without current
entitlement. A portal downgrade does not delete monitors or history.

## Sources

- https://docs.stripe.com/billing/quickstart
- https://docs.stripe.com/webhooks
- https://stripe.com/ie/pricing
- https://stripe.com/billing/pricing

Current limitation: per-instance runner and SQLite store; keep one Gunicorn
worker and one Render instance. Server capacity and support costs still need
measurement on external pilot sites before increasing customer volume.

## Public registration (September 29)

Set `FM_PUBLIC_REGISTRATION=1` to allow any valid email to create a normal
account, sign in, and recover its password without email allowlist approval.
The default remains restricted. Set it to `0` to restore `FM_ALLOWED_EMAILS`
enforcement (accounts remain stored, but non-allowlisted sessions are rejected).
Owner setup still requires its secret; registration cannot grant owner rights.
The public `/trial` link opens signup, or billing for an existing session.
Registration alone does not start a trial or charge the user. Website host
restrictions, OAuth configuration, plan limits and payment switches are unchanged.
This setting supersedes the email-review portions of the September 24 notes below.

## Contact and trial requests

The homepage links to `/trial` and `/contact`. Visitors can request access, ask
questions, or submit data requests. Each new request is saved in the existing
database and queued for email to the registered owner account (never the visitor
or a submitted recipient field). Access requests remain available only to the
owner. Reply-To uses the visitor's unverified address, allowing a manual reply
from the owner's inbox. No automatic customer reply or account approval is sent.

The worker uses the existing `RESEND_API_KEY` and `FM_EMAIL_FROM`. It runs by
default; `FM_INQUIRY_EMAILS_ENABLED=0` pauses delivery without losing requests.
Pending deliveries survive restarts; temporary errors retry with a persisted
envelope and Resend idempotency key. Retries stop after eight attempts or 23 hours
from the first attempt, before the provider's 24-hour key expiry. Permanent
errors are shown as failed in the owner's request list. "Accepted" means accepted
by the provider, not guaranteed inbox delivery; confirm delivery in Resend.
Existing requests from before this release are not emailed retroactively.

Requests and their queued envelopes expire after 90 days or owner deletion. Mail
already accepted by the provider cannot be recalled by deleting a workspace row;
copies in the owner's inbox, provider records, and backups need separate handling.
Deleting during an in-flight send may not stop that email. Verify account ownership
before handling a data request. The public `/privacy` page describes this behavior.

## September 24 experience release

Trials are now independent from paid Checkout. `FM_TRIALS_ENABLED` defaults to
`1`; an approved non-owner account can start its single seven-day, one-form trial
after connecting Gmail even while `FM_BILLING_ENABLED=0`. The owner retains access.
The existing email and website allowlists stay enforced. `/trial` clearly requests
review; it does not create an account or start the trial clock. Registration and
site approval remain operator tasks using the existing allowlists. No real
payment switch is enabled by this release.

Solo includes seven days of history. Studio includes 90 days, client/site groups,
paginated history, CSV export and calendar-week reports inside the workspace.
Weekly reports use existing completed-cycle events; retries are not counted as
separate cycles. They are not an email subscription and claim no continuous uptime.
History windows also apply to screenshot URLs. Downgrades do not delete records;
an expired paid account retains read access to its last plan's history window.
Exports contain check metadata, not Gmail message contents, and escape spreadsheet
formula prefixes. All routes operate on the authenticated user's workspace.

Legacy Stripe customer records are identified against the active API mode before
use. Unknown-mode paid records never grant paid entitlement. A customer missing
in the current mode is archived before resetting that mode's billing state.
Known-mode archived records are restored when switching back, preventing duplicate
customers/subscriptions. Provider failures leave records intact. Trial expiry is
preserved across mode changes. Database migrations are additive; archives are
included in the existing encrypted core backups.
