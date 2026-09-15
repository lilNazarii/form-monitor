# Sentleaf password recovery

The sign-in page offers **Forgot password?**. The user enters their Sentleaf account email, receives a link, and sets a new password twice. This email can differ from the Gmail connection used for form checks. Finishing a reset ends that account's existing sessions and returns the user to sign-in. Monitors, schedules, Gmail connections, account identity, and owner permissions are retained.

## Configuration and delivery

The existing `RESEND_API_KEY` sends account emails. The recipient comes from the registered account, not `ALERT_EMAIL`. Only accounts in `FM_ALLOWED_EMAILS` can request or redeem a reset link. Invitations without a registered account receive no mail.

`FM_EMAIL_FROM` can optionally select a sender authorized in Resend. If unset, the established `Sentleaf <onboarding@resend.dev>` sender remains in use. Resend's allowed-recipient and sender restrictions still apply; this update does not expand the provider's permissions. Check delivery with an enabled account before extending the beta.

Requests return the same HTTP 202 message for existing, unknown, disabled, and throttled addresses. A bounded background queue keeps mail-provider latency out of this response. A response is not proof of delivery. If no email arrives, wait one minute and request again; inspect the provider and sanitized Render logs if needed. A missing API key produces a generic 503 availability message for everyone.

## Link and password handling

- Tokens have 32 random bytes; only their SHA-256 hash is stored in SQLite.
- Issued links last 15 minutes and survive a process restart until expiry.
- Links bind to the account and its current password digest. One successful reset atomically changes the password and invalidates all issued links for that account.
- Opening or validating a link does not consume it. Mail previews cannot perform a password change.
- The HTTPS origin comes from server configuration, not a client-supplied Host or forwarded header.
- The token travels in a URL fragment. The page removes the fragment and keeps the token only in memory until POST submission. Reloading requires reopening the email link. It is not placed in browser storage or query strings.
- No automatic login follows a reset. The policy is 12–256 characters, with confirmation and the existing scrypt hash settings.
- The site uses `no-store`, `no-referrer`, and the existing restrictive CSP. Error logs do not include tokens or provider response bodies for account mail.
- Requests allow one per email per minute, three per email per hour, ten globally per minute and sixty globally per hour. Token validation/redemption allows sixty attempts per minute on this single-instance beta. These counters are separate from login and reset on restart.
- Provider failures discard the newly issued link. Earlier unused links remain valid until expiry or a successful reset.
- Successful password changes queue an informational account email. Provider failures do not roll back the password change.

Pending, not-yet-sent queue entries are transient and may be lost on restart; request again if necessary. The deployed service must retain its existing single Gunicorn worker configuration, which also owns browser sessions and schedules.

## Backups and migration

Startup adds a `password_resets` table without altering the users table or existing password hashes. Backups copy the account database and clear reset records from the copy; restoration also clears those records before installing the staged database. Live links are unaffected by making a backup. Restoring an archive never revives reset links.

## Verification

Run `python -m unittest discover -s tests -v` in this directory with project dependencies installed. The 33 tests include existing server/backup checks and recovery cases: legacy schema migration, password policy, allowlist, expiry/replay, simultaneous redemption, session revocation, preserved workspace data, provider failures, request/token throttling, sanitized errors, canonical origin, and backup exclusions. Local DOM checks also cover request/retry, mismatched confirmation, token removal, CSRF renewal, expired-link screens, and successful completion.

After deployment, use an enabled account to request one email, follow its link, set a new password, sign in, and verify the used link cannot reset it again. This final check uses the user's actual mailbox and a password chosen by the user.

Design references: [OWASP password recovery guidance](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html), [Resend Send Email API](https://resend.com/docs/api-reference/emails/send-email).
