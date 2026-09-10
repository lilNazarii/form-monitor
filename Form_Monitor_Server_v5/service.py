import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from monitor import SITE_URL, SCOPE_DESCRIPTION, CONFIG
from email_sender import send_email


ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get('FM_DATA_ROOT', str(ROOT)))
DATA = (DATA_ROOT / "results" / "monitors" / CONFIG["id"] / "service" / str(CONFIG["revision"])
        if CONFIG else DATA_ROOT / "results" / "service_gmail")
STATE_FILE = DATA / "state.json"
RETRY_SECONDS = 30


def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print(f"[{now()}] {message}", flush=True)


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


def load_state():
    if not STATE_FILE.exists():
        return {"notified_state": "UNKNOWN"}

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if state.get("notified_state") not in {"UNKNOWN", "UP", "DOWN"}:
        raise ValueError("Invalid state.json")
    return state


def run_check():
    log("Running monitor.py...")

    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"

    # Проверяющему скрипту ключ отправки уведомлений не нужен.
    environment.pop("RESEND_API_KEY", None)

    try:
        process = subprocess.run(
            [sys.executable, str(ROOT / "monitor.py")],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
        )

        if process.stdout:
            print(process.stdout, end="", flush=True)

        paths = [
            line[len("Report:"):].strip()
            for line in process.stdout.splitlines()
            if line.startswith("Report:")
        ]

        if not paths:
            raise RuntimeError(
                "Monitor did not produce a report. "
                + process.stderr[-1500:]
            )

        report = json.loads(
            Path(paths[-1]).read_text(encoding="utf-8")
        )

        expected_codes = {"PASS": 0, "FAIL": 1, "ERROR": 2}
        status = report.get("status")

        if status not in expected_codes:
            raise RuntimeError("Unknown monitor status.")
        if process.returncode != expected_codes[status]:
            raise RuntimeError("Exit code and report disagree.")

        report["report_path"] = paths[-1]
        return report

    except Exception as error:
        return {
            "status": "ERROR",
            "stage": "monitor_execution",
            "details": f"{type(error).__name__}: {error}",
            "site": SITE_URL,
        }


def notification_text(report, recovered=False):
    if recovered:
        summary = (
            "The latest check passed. The form submitted successfully "
            "and its matching test email was received in Gmail INBOX."
        )
    elif report["status"] == "ERROR":
        summary = (
            "Monitoring could not confirm normal operation in two "
            "consecutive attempts. This may be a monitoring or "
            "connectivity problem, not necessarily a broken form."
        )
    else:
        summary = (
            "Two consecutive checks did not confirm the expected "
            "form-to-email workflow. Please investigate."
        )

    return (
        "Form Monitor\n\n"
        f"Website: {SITE_URL}\n"
        f"Time (UTC): {now()}\n\n"
        f"{summary}\n\n"
        f"Latest result: {report['status']}\n"
        f"Details: {report.get('details', '')}\n\n"
        f"Monitoring scope: {SCOPE_DESCRIPTION}\n"
        "This alert is sent separately through Resend."
    )


def run_cycle(state):
    first = run_check()
    final = first
    attempts = 1

    if first["status"] != "PASS":
        log(f"Retrying in {RETRY_SECONDS} seconds...")
        time.sleep(RETRY_SECONDS)
        final = run_check()
        attempts = 2

    current = "UP" if final["status"] == "PASS" else "DOWN"
    previous = state["notified_state"]

    event = {
        "time_utc": now(),
        "profile_id": CONFIG.get("id"),
        "revision": CONFIG.get("revision"),
        "attempts": attempts,
        "first_status": first["status"],
        "final_status": final["status"],
        "details": final.get("details", ""),
        "report_path": final.get("report_path"),
        "transport": "resend",
        "notification": "not_required",
    }

    try:
        if current == "DOWN" and previous != "DOWN":
            event["email_id"] = send_email(
                "[Form Monitor] Check needs attention",
                notification_text(final),
            )
            event["notification"] = "problem_accepted"

        elif current == "UP" and previous == "DOWN":
            event["email_id"] = send_email(
                "[Form Monitor] Monitoring check recovered",
                notification_text(final, recovered=True),
            )
            event["notification"] = "recovery_accepted"

        updated = {
            "notified_state": current,
            "updated_utc": now(),
            "last_status": final["status"],
        }
        save_state(updated)
        state.clear()
        state.update(updated)

    except Exception as error:
        event["notification"] = "notification_or_state_error"
        event["notification_error"] = str(error)
        log(f"Notification/state error: {error}")
        log("A later cycle may retry the notification.")

    with (DATA / "events.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")

    log(
        f"Cycle complete: {final['status']}; "
        f"notification: {event['notification']}"
    )

    if event["notification"] == "notification_or_state_error":
        return 2
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[final["status"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--interval-minutes", type=int, default=60)
    args = parser.parse_args()

    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")

    for setting in ("RESEND_API_KEY", "ALERT_EMAIL"):
        if not os.environ.get(setting, "").strip():
            raise RuntimeError(
                f"{setting} is missing. Configure it in this terminal."
            )

    DATA.mkdir(parents=True, exist_ok=True)

    if args.test_email:
        send_email(
            "[Form Monitor] Service email test",
            "External email notifications are connected to service.py.\n\n"
            f"Website: {SITE_URL}\n"
            f"Time (UTC): {now()}",
        )
        return 0

    state = load_state()
    log("Email transport: Resend.")

    while True:
        result = run_cycle(state)

        if args.once:
            return result

        log(
            f"Next cycle in {args.interval_minutes} minutes. "
            "Press Ctrl+C to stop."
        )
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nService stopped.", flush=True)
        raise SystemExit(0)
    except Exception as error:
        print(f"\nSERVICE ERROR: {type(error).__name__}: {error}")
        raise SystemExit(2)
