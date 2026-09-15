import json
import os
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def send_email(subject, text):
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    recipient = os.environ.get("ALERT_EMAIL", "").strip()

    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not configured.")
    if not recipient:
        raise RuntimeError("ALERT_EMAIL is not configured.")

    payload = {
        "from": "Sentleaf <onboarding@resend.dev>",
        "to": [recipient],
        "subject": subject,
        "text": text,
    }

    request = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "FormMonitor/0.1",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)

    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        details = details.replace(api_key, "[REDACTED]")
        raise RuntimeError(
            f"Resend HTTP {error.code}: {details}"
        ) from None

    except (URLError, TimeoutError):
        raise RuntimeError(
            "Could not confirm Resend's response. "
            "Check the Resend dashboard before sending again."
        ) from None

    message_id = result.get("id")
    if not message_id:
        raise RuntimeError("Resend did not return a message ID.")

    print("Resend accepted the email.")
    print("Message ID:", message_id)
    return message_id


if __name__ == "__main__":
    reference = uuid.uuid4().hex[:12]
    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        send_email(
            f"[Sentleaf] Email connection test {reference}",
            "This is a test notification from Sentleaf.\n\n"
            "If you can read it in your mailbox, "
            "this test notification has reached you.\n\n"
            f"Reference: {reference}\n"
            f"Time (UTC): {timestamp}\n\n"
            "Website form delivery is a separate check.",
        )
        print("Now check your inbox and spam folder.")
    except Exception as error:
        print(f"EMAIL ERROR: {error}")
        raise SystemExit(1)
