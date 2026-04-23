#!/usr/bin/env python3
"""
FrontGate Tickets — New Event Watcher

Runs hourly via GitHub Actions. Scrapes frontgatetickets.com/events, compares
against the last snapshot in state/events.json, and emails an alert if any
new events appear. Pure stdlib — no pip install needed.

Required env vars:
  SENDGRID_API_KEY  — SendGrid API key
  ALERT_EMAIL       — recipient (e.g. zfinkel1@gmail.com)
  FROM_EMAIL        — verified SendGrid sender (e.g. alerts@sportscardnetwork.ai)
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

EVENTS_URL = "https://www.frontgatetickets.com/events"
BASE_URL = "https://www.frontgatetickets.com"
STATE_FILE = Path(__file__).parent / "state" / "events.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def fetch_html():
    req = urllib.request.Request(EVENTS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def parse_events(html):
    """
    Each event is wrapped in a div with class "event-item-wrap". Inside we pull:
      - slug   from href="/events/{slug}"
      - name   from the first h3 with class "heading-style-h6 is-main"
      - location from the div with fs-cmsfilter-field="location"
      - date   from fs-cmssort-field="festival-date"  (e.g. "April 25, 2026")
    """
    events = []
    # Split on event wrapper boundaries to process one event block at a time
    blocks = re.split(r'class="event-item-wrap', html)[1:]  # skip text before first event
    for block in blocks:
        # Truncate block at next event boundary (approx) to avoid cross-leak
        block = block[:8000]

        slug_m = re.search(r'href="/events/([a-z0-9-]+)"', block)
        if not slug_m:
            continue
        slug = slug_m.group(1)

        name_m = re.search(
            r'<h3[^>]*class="heading-style-h6 is-main"[^>]*>([^<]+)</h3>',
            block,
        )
        name = (name_m.group(1).strip() if name_m else slug.replace("-", " ").title())

        loc_m = re.search(r'fs-cmsfilter-field="location"[^>]*>([^<]+)<', block)
        location = (loc_m.group(1).strip() if loc_m else "")

        date_m = re.search(
            r'fs-cmssort-field="festival-date"[^>]*>([^<]+)<',
            block,
        )
        date = (date_m.group(1).strip() if date_m else "")

        events.append({
            "slug": slug,
            "name": name,
            "location": location,
            "date": date,
            "url": f"{BASE_URL}/events/{slug}",
        })

    # De-dupe by slug (page sometimes lists the same event twice in different sections)
    seen = set()
    unique = []
    for e in events:
        if e["slug"] in seen:
            continue
        seen.add(e["slug"])
        unique.append(e)
    return unique


def load_state():
    if not STATE_FILE.exists():
        return {"events": [], "last_run": None}
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(events):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_run": datetime.utcnow().isoformat() + "Z",
        "count": len(events),
        "events": events,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_email_via_sendgrid(to_addr, from_addr, subject, html_body, api_key):
    payload = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": from_addr, "name": "FrontGate Watcher"},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def build_email_body(new_events, total):
    rows = []
    for e in new_events:
        name = e["name"]
        date = e["date"] or "TBA"
        loc = e["location"] or ""
        url = e["url"]
        rows.append(f"""
          <tr>
            <td style="padding:14px 16px;border-bottom:1px solid #eee;">
              <div style="font-size:15px;font-weight:700;color:#0d1b3e;margin-bottom:4px;">
                <a href="{url}" style="color:#0d1b3e;text-decoration:none;">{name} →</a>
              </div>
              <div style="font-size:13px;color:#666;">{date} · {loc}</div>
            </td>
          </tr>
        """)
    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#0d1b3e;margin:0 0 6px;font-size:22px;">
        {len(new_events)} new event{'s' if len(new_events) != 1 else ''} on FrontGate
      </h2>
      <p style="color:#666;margin:0 0 20px;font-size:13px;">
        Scraped {datetime.utcnow().strftime('%b %d, %Y at %H:%M UTC')} · Total tracked: {total}
      </p>
      <table style="width:100%;border-collapse:collapse;background:#fafbff;border-radius:10px;overflow:hidden;border:1px solid #eee;">
        {''.join(rows)}
      </table>
      <p style="color:#999;font-size:11px;margin-top:20px;">
        You're receiving this because your ticket-tools-gct watcher found new events.
      </p>
    </div>
    """


def main():
    api_key = os.environ.get("SENDGRID_API_KEY")
    alert_email = os.environ.get("ALERT_EMAIL", "zfinkel1@gmail.com")
    from_email = os.environ.get("FROM_EMAIL", "alerts@sportscardnetwork.ai")

    if not api_key:
        print("[ERROR] SENDGRID_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    print(f"[info] Fetching {EVENTS_URL}")
    html = fetch_html()
    current = parse_events(html)
    print(f"[info] Parsed {len(current)} events")

    state = load_state()
    known = {e["slug"] for e in state.get("events", [])}
    new_events = [e for e in current if e["slug"] not in known]

    is_first_run = not state.get("last_run")

    if is_first_run:
        print(f"[info] First run — baselining {len(current)} events, no email sent")
    elif new_events:
        print(f"[info] Found {len(new_events)} new event(s), sending email")
        body = build_email_body(new_events, len(current))
        try:
            status = send_email_via_sendgrid(
                to_addr=alert_email,
                from_addr=from_email,
                subject=f"[FrontGate] {len(new_events)} new event(s)",
                html_body=body,
                api_key=api_key,
            )
            print(f"[info] SendGrid responded {status}")
        except urllib.error.HTTPError as e:
            print(f"[ERROR] SendGrid {e.code}: {e.read().decode('utf-8', errors='ignore')}", file=sys.stderr)
            sys.exit(1)
        print("[info] New events:")
        for e in new_events:
            print(f"  - {e['name']} | {e['date']} | {e['location']} | {e['url']}")
    else:
        print("[info] No new events")

    save_state(current)
    print(f"[info] State saved. Total tracked: {len(current)}")


if __name__ == "__main__":
    main()
