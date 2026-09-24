"""Account-scoped reports from existing monitoring records; no extra Gmail access."""
import csv
import io
from datetime import datetime, timezone, timedelta


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0


def weekly_summary(snapshot, weeks_ago=0, now=None):
    """Calendar weeks, Monday 00:00 UTC. Counts completed cycles, not retry attempts."""
    now = now or datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start = monday - timedelta(weeks=weeks_ago)
    end = min(start + timedelta(days=7), now)
    profiles = {p['id']:p for p in snapshot['profiles']}
    rows = {}
    for event in snapshot['events']:
        if not start.timestamp() <= timestamp(event.get('time_utc')) < end.timestamp():
            continue
        ident = event['profile_id']
        profile = profiles.get(ident, {})
        row = rows.setdefault(ident, dict(id=ident, name=event['profile_name'],
            group=profile.get('group',''), archived=event.get('archived',False),
            checks=0, passed=0, failed=0, recovered=0))
        row['checks'] += 1
        row['passed'] += event.get('final_status') == 'PASS'
        row['failed'] += event.get('final_status') != 'PASS'
        row['recovered'] += event.get('notification') == 'recovery_accepted'
    for ident, profile in profiles.items():
        rows.setdefault(ident, dict(id=ident,name=profile['name'],group=profile.get('group',''),
                                   archived=False,checks=0,passed=0,failed=0,recovered=0))
    total = sum(r['checks'] for r in rows.values())
    passed = sum(r['passed'] for r in rows.values())
    return dict(start=start.isoformat(), end=end.isoformat(), weeks_ago=weeks_ago,
                checks=total, passed=passed, failed=total-passed,
                success_rate=round(100*passed/total,1) if total else None,
                rows=sorted(rows.values(),key=lambda r:(-r['failed'],r['name'].casefold())))


def history_csv(reports):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['Started (UTC)','Monitor','Result','Stage','Form confirmed','Inbox confirmed','Reference'])
    def safe(value):
        value = str(value if value is not None else '')
        # Spreadsheet formula injection, including leading whitespace/control chars.
        return "'"+value if value.lstrip().startswith(('=','+','-','@')) or value[:1] in ('\t','\r','\n') else value
    for report in reports:
        writer.writerow([safe(report.get(k,'')) for k in
                         ('started_utc','profile_name','status','stage','form_confirmed','email_confirmed','run_id')])
    return '\ufeff'+output.getvalue()
