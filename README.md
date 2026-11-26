# Sentleaf: Contact Form Monitoring

Sentleaf checks that a website's contact form still works from end to end. It submits a real test enquiry, then confirms that the matching email actually arrives in the owner's Gmail inbox. If the form breaks or the email never arrives, the owner gets an alert.

> **Status:** personal project, ran as a closed beta on [Render](https://render.com) in 2026. Development is paused.

## The problem

A broken contact form fails silently. A plugin update, an expired SMTP password or a DNS change can stop enquiries from reaching the inbox, and the site owner usually finds out weeks later from a customer. Uptime monitors don't catch this, because the page itself still loads.

## How it works

1. **Connect a form.** The user adds the URL of a contact form page and proves they own the site by adding a hidden verification token to it.
2. **Connect Gmail.** The user links the inbox that receives enquiries through Google OAuth. Access is read-only (`gmail.readonly`).
3. **Run a check.** A headless Chromium browser (Playwright) fills in and submits the form. The message carries a unique reference such as `MONITOR-3f9a…`.
4. **Confirm delivery.** The server searches the inbox through the Gmail API for that reference and waits up to 45 seconds.
5. **Report.** Each check is saved to the monitor's history with a status, a screenshot and logs. If a check fails, an email alert is sent through Resend.

Checks can run on a schedule (every 6 hours by default). Schedules survive server restarts.

## Features

- Accounts with registration, login, password recovery and isolated per-user data
- Three-step setup for a monitor: page, inbox, schedule
- Monitor history with screenshots, error hints and expandable technical logs
- Site ownership verification, so users can only test forms on their own sites
- Gmail OAuth tokens encrypted at rest (Fernet)
- Daily encrypted backups of core data, downloadable by the owner
- Two subscription plans through Stripe Checkout (test mode): **Solo** for 1 form and **Studio** for 5 forms with client groups, CSV export and weekly reports
- Responsive dashboard and landing page with their own visual identity
- 86 automated tests covering auth, CSRF, data isolation, billing, backups and recovery

## Tech stack

| Area | Technology |
| --- | --- |
| Backend | Python, Flask, Gunicorn |
| Browser automation | Playwright (headless Chromium) |
| Email | Gmail API (OAuth 2.0), Resend for alerts |
| Payments | Stripe |
| Security | `cryptography` (Fernet), CSRF protection, secure cookies |
| Frontend | HTML, CSS, vanilla JavaScript |
| Deployment | Docker on Render with a persistent disk, custom domain with HTTPS |
| Tests | `unittest` |

## Project structure

```text
Form_Monitor_Server_v5/
├── server.py            # Flask app, routes and API
├── monitor.py           # Form submission and delivery check
├── google_mail.py       # Gmail OAuth, encrypted token storage, inbox search
├── accounts.py          # Users, sessions, password recovery
├── site_verification.py # Website ownership check
├── billing.py           # Plans and Stripe integration
├── email_sender.py      # Alerts through Resend
├── backups.py           # Encrypted daily backups
├── dashboard_assets/    # Dashboard and landing page (HTML/CSS/JS)
├── tests/               # Automated tests
├── Dockerfile
└── render.yaml          # Render Blueprint
```

## Running it

The app is built to run as a single Docker container on Render. `render.yaml` describes the service and the persistent disk. Secrets are never stored in the repository; they are set as environment variables:

`FM_ENCRYPTION_KEY`, `FM_SETUP_CODE`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `RESEND_API_KEY`, and optionally Stripe keys.

To run the tests locally:

```bash
cd Form_Monitor_Server_v5
pip install -r requirements.txt
python -m unittest discover tests
```

Full deployment notes (in Russian) are in [`README_SERVER.md`](Form_Monitor_Server_v5/README_SERVER.md).

## What I learned

- Deploying a Dockerised Python app with a persistent disk, a custom domain, DNS and HTTPS
- Implementing Google OAuth 2.0 for a web application and handling token expiry
- Diagnosing email delivery problems in WordPress forms (Contact Form 7)
- Designing a product end to end, from the landing page to billing and backups

Built with AI-assisted development tools.

## Author

Nazarii Tymenko, Software Development student at Munster Technological University, Cork.
