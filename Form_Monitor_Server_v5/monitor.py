import json
import os
import imaplib
import ssl
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from urllib.parse import urlsplit
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from google_mail import Store, GmailAPI


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get('FM_DATA_ROOT', str(ROOT)))
CONFIG = {}
if os.environ.get("FM_PROFILE"):
    CONFIG = json.loads(Path(os.environ["FM_PROFILE"]).read_text(encoding="utf-8"))
SITE_URL = CONFIG.get("url", "http://form-monitor-lab.local/contact/")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
SCOPE_DESCRIPTION = "WordPress form to Gmail INBOX (read-only)."
MAIL_WAIT_SECONDS = 45
RESULTS = (DATA_ROOT / "results" / "monitors" / CONFIG["id"] / "reports"
           if CONFIG else DATA_ROOT / "results" / "wordpress")
FORM_SELECTOR = CONFIG.get("selector", "form.wpcf7-form")
FIELD_NAMES = CONFIG.get("fields", {
    "name": "your-name", "email": "your-email",
    "subject": "your-subject", "message": "your-message",
})


class CheckFailed(Exception):
    """Проверка выполнена, но ожидаемый результат не подтверждён."""


def gmail_connect():
    global GMAIL_ADDRESS
    if CONFIG.get("connection_id"):
        client = GmailAPI(DATA_ROOT, CONFIG["connection_id"])
        GMAIL_ADDRESS = client.store.load(CONFIG["connection_id"])["email"]
        return client
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not GMAIL_ADDRESS or not password:
        raise RuntimeError(
            "Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in this terminal first."
        )
    client = imaplib.IMAP4_SSL(
        "imap.gmail.com", 993, ssl_context=ssl.create_default_context(), timeout=10
    )
    try:
        try:
            client.login(GMAIL_ADDRESS, password)
        except imaplib.IMAP4.error:
            raise RuntimeError(
                "Gmail login failed. Check GMAIL_ADDRESS and the Google app password."
            ) from None
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Gmail INBOX could not be opened in read-only mode.")
        return client
    except Exception:
        try:
            client.logout()
        except Exception:
            pass
        raise


def matching_message(raw, token, expected_body, recipient):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if token not in str(message.get("Subject", "")):
        return False
    addresses = {address.lower() for _, address in getaddresses(
        [str(value) for value in message.get_all("To", [])]
    )}
    if recipient.lower() not in addresses:
        return False
    # The current CF7 template sends plain text. Attachments are excluded.
    expected = " ".join(expected_body.split())
    for part in message.walk():
        if part.get_content_type() != "text/plain" or part.get_content_disposition() == "attachment":
            continue
        content = part.get_content()
        if isinstance(content, str) and expected in " ".join(content.split()):
            return True
    return False


def find_email(client, page, token, expected_body):
    started = time.monotonic()
    deadline = started + MAIL_WAIT_SECONDS
    inspected = set()
    last_log = -15
    polls = 0
    while time.monotonic() < deadline:
        # Refresh the selected mailbox before searching for newly arrived mail.
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Gmail INBOX refresh failed.")
        polls += 1
        status, data = client.uid("search", None, "SUBJECT", '"' + token + '"')
        if status != "OK":
            raise RuntimeError("Gmail search failed; inbox delivery could not be checked.")
        candidates = (data[0] or b"").split()
        elapsed = int(time.monotonic() - started)
        if elapsed - last_log >= 15 or candidates:
            print(
                f"[Gmail] {elapsed}s: {len(candidates)} subject candidate(s); "
                f"search #{polls}.", flush=True,
            )
            last_log = elapsed
        for uid in candidates:
            if uid in inspected:
                continue
            # PEEK fetches the message without setting the Seen flag.
            status, parts = client.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError("Gmail could not return the matching test message.")
            for part in parts:
                if isinstance(part, tuple) and isinstance(part[1], bytes):
                    if matching_message(part[1], token, expected_body, GMAIL_ADDRESS):
                        return {
                            "id": uid.decode("ascii"),
                            "recipient": GMAIL_ADDRESS,
                            "mailbox": "INBOX",
                            "body_verified": True,
                            "wait_seconds": round(time.monotonic() - started, 2),
                            "search_count": polls,
                        }
            print(
                "[Gmail] Candidate did not match the expected recipient/plain-text body, "
                "or no message content was returned.", flush=True,
            )
            # Retry candidates if Gmail did not return message content yet.
            if any(isinstance(part, tuple) and isinstance(part[1], bytes) for part in parts):
                inspected.add(uid)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            page.wait_for_timeout(min(2000, remaining * 1000))
    return None


def browser_options():
    address = urlsplit(SITE_URL)
    if address.scheme not in {"http", "https"} or not address.hostname or address.username:
        raise RuntimeError("SITE_URL must be an HTTP(S) URL without embedded credentials.")
    if address.hostname.endswith(".localsite.io"):
        if address.scheme != "https":
            raise RuntimeError("Use HTTPS for the Live Link.")
        username = os.environ.get("LIVE_LINK_USER", "").strip()
        password = os.environ.get("LIVE_LINK_PASSWORD", "")
        if not username or not password:
            raise RuntimeError("Set LIVE_LINK_USER and LIVE_LINK_PASSWORD for this Live Link.")
        return {"http_credentials": {
            "username": username, "password": password,
            "origin": f"{address.scheme}://{address.netloc}",
        }}
    return {}


def main():
    started = datetime.now(timezone.utc)
    token = "MONITOR-" + uuid.uuid4().hex
    expected_body = f"Automatic monitoring test. Reference: {token}"

    run_folder = RESULTS / token
    run_folder.mkdir(parents=True, exist_ok=False)

    report = {
        "started_utc": started.isoformat(),
        "run_id": token,
        "profile_id": CONFIG.get("id"),
        "revision": CONFIG.get("revision"),
        "site": SITE_URL,
        "scope": "wordpress_to_gmail_inbox",
        "status": "ERROR",
        "stage": "startup",
        "form_confirmed": False,
        "email_confirmed": False,
        "details": "",
        "screenshot": None,
    }

    print("Run:", token, flush=True)

    try:
        with sync_playwright() as p:
            browser = None
            client = None
            context = None
            page = None

            try:
                report["stage"] = "gmail_connection"
                client = gmail_connect()
                print("[1/4] Gmail INBOX is available (read-only).", flush=True)

                report["stage"] = "open_site"
                options = browser_options()
                from server_network import validate_url, browser_routes
                validate_url(SITE_URL)
                expected_host = os.environ.get('FM_VERIFIED_FORM_HOST')
                if expected_host and urlsplit(SITE_URL).hostname != expected_host:
                    raise CheckFailed('The form hostname does not match its verified website.')
                browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage",
                    "--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
                context = browser.new_context(service_workers="block", **options)
                context.route("**/*", browser_routes(SITE_URL))
                context.route_web_socket("**/*", lambda ws: ws.close())
                page = context.new_page()
                page.set_default_timeout(15000)

                response = page.goto(
                    SITE_URL,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                if response is None or not response.ok:
                    code = response.status if response else "no response"
                    raise CheckFailed(f"Website returned: {code}")

                form = page.locator(FORM_SELECTOR)
                expect(form).to_have_count(1)
                expect(form).to_be_visible()
                print("[2/4] WordPress form is available.", flush=True)

                report["stage"] = "submit_form"
                form.locator('[name="' + FIELD_NAMES['name'] + '"]').fill("TEST Monitor")
                form.locator('[name="' + FIELD_NAMES['email'] + '"]').fill(
                    "monitor@example.com"
                )
                form.locator('[name="' + FIELD_NAMES['subject'] + '"]').fill(token)
                form.locator('[name="' + FIELD_NAMES['message'] + '"]').fill(expected_body)

                # Перехватываем ответ сервера на отправку формы.
                with page.expect_response(
                    lambda r: (
                        r.request.method == "POST"
                        and "/contact-forms/" in r.url
                        and "/feedback" in r.url
                    ),
                    timeout=20000,
                ) as submission:
                    form.locator('[type="submit"]').click()

                feedback_response = submission.value
                if not feedback_response.ok:
                    raise CheckFailed(
                        "Form endpoint returned HTTP "
                        + str(feedback_response.status)
                    )

                feedback = feedback_response.json()
                report["form_response"] = {
                    "status": feedback.get("status"),
                    "message": feedback.get("message"),
                }

                if feedback.get("status") != "mail_sent":
                    raise CheckFailed(
                        "Contact Form 7 returned: "
                        + str(feedback.get("status"))
                        + ". "
                        + str(feedback.get("message"))
                    )

                report["stage"] = "form_confirmation"
                try:
                    expect(form).to_have_attribute(
                        "data-status", "sent", timeout=10000
                    )
                except AssertionError as error:
                    raise CheckFailed(
                        "Server reported success, but the page did not "
                        "show the expected form state."
                    ) from error

                report["form_confirmed"] = True
                print("[3/4] Form confirmed submission.", flush=True)

                report["stage"] = "email_confirmation"
                print(
                    f"[Gmail] Waiting up to {MAIL_WAIT_SECONDS}s for this run's email.",
                    flush=True,
                )
                email = find_email(client, page, token, expected_body)

                if email is None:
                    raise CheckFailed(
                        "Delivery not confirmed: no matching email "
                        "with the expected recipient and body was found "
                        f"in Gmail INBOX within {MAIL_WAIT_SECONDS} seconds. Check Spam or mail filters; "
                        "messages outside INBOX are not counted as success."
                    )

                report["email"] = email
                report["email_confirmed"] = True
                report["status"] = "PASS"
                report["stage"] = "complete"
                report["details"] = (
                    "Form submission and receipt in Gmail INBOX "
                    "are confirmed."
                )
                print("[4/4] Matching email received in Gmail INBOX.", flush=True)

            finally:
                # Снимок сохраняется и при неуспешной проверке.
                if page is not None and not page.is_closed():
                    try:
                        screenshot = run_folder / "page.png"
                        page.screenshot(
                            path=str(screenshot),
                            full_page=True,
                            timeout=5000,
                        )
                        report["screenshot"] = str(screenshot)
                    except Exception as error:
                        report["screenshot_error"] = str(error)

                if context is not None:
                    try:
                        context.close()
                    except Exception as error:
                        report["context_cleanup_error"] = type(error).__name__

                if browser is not None:
                    try:
                        browser.close()
                    except Exception as error:
                        report["browser_cleanup_error"] = str(error)

                if client is not None:
                    try:
                        client.logout()
                    except Exception as error:
                        report["gmail_cleanup_error"] = type(error).__name__

    except CheckFailed as error:
        report["status"] = "FAIL"
        report["details"] = str(error)

    except Exception as error:
        report["status"] = "ERROR"
        report["details"] = f"{type(error).__name__}: {error}"

    report["finished_utc"] = datetime.now(timezone.utc).isoformat()

    serialized = json.dumps(report, ensure_ascii=False)
    for name in ("GMAIL_APP_PASSWORD", "LIVE_LINK_PASSWORD", "RESEND_API_KEY"):
        secret = os.environ.get(name, "")
        for value in {secret, secret.replace(" ", "")}:
            if value:
                escaped = json.dumps(value, ensure_ascii=False)[1:-1]
                serialized = serialized.replace(escaped, "[REDACTED]")
    report = json.loads(serialized)

    report_path = run_folder / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (RESULTS / "history.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(report, ensure_ascii=False) + "\n")

    print(f"\n{report['status']}: {report['details']}")
    print("Stage:", report["stage"])
    print("Report:", report_path)

    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
