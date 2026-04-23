#!/usr/bin/env python3
"""
Multi-site Ticket Watcher.

For each site in sites.py:
  1. Call the appropriate parser to get current events
  2. Diff against state/{slug}.json
  3. Email (one combined email) if any new events across all sites
  4. Save updated state

Required env vars:
  SENDGRID_API_KEY
  ALERT_EMAIL
  FROM_EMAIL
"""

import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from parsers import PARSERS
from sites import SITES

STATE_DIR = Path(__file__).parent / "state"


def slugify(name):
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")


def state_path(site):
    return STATE_DIR / f"{slugify(site['name'])}.json"


def load_state(site):
    p = state_path(site)
    if not p.exists():
        return {"events": [], "last_run": None}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_state(site, events):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "site": site["name"],
        "last_run": datetime.utcnow().isoformat() + "Z",
        "count": len(events),
        "events": events,
    }
    with open(state_path(site), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def send_email(to_addr, from_addr, subject, html_body, api_key):
    payload = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": from_addr, "name": "Ticket Tools GCT"},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def build_email(by_site):
    """by_site: {site_name: [new_events]}"""
    total = sum(len(v) for v in by_site.values())
    sections = []
    for site_name, events in by_site.items():
        rows = []
        for e in events:
            name = e.get("name", "Unnamed")
            date = e.get("date") or "TBA"
            loc = e.get("location") or ""
            url = e.get("url", "#")
            rows.append(f"""
              <tr><td style="padding:14px 16px;border-bottom:1px solid #eee;">
                <div style="font-size:15px;font-weight:700;color:#0d1b3e;margin-bottom:4px;">
                  <a href="{url}" style="color:#0d1b3e;text-decoration:none;">{name} →</a>
                </div>
                <div style="font-size:13px;color:#666;">{date}{' · ' + loc if loc else ''}</div>
              </td></tr>
            """)
        sections.append(f"""
          <h3 style="font-size:13px;letter-spacing:0.05em;color:#c9a227;text-transform:uppercase;margin:24px 0 10px;">
            {site_name} — {len(events)} new
          </h3>
          <table style="width:100%;border-collapse:collapse;background:#fafbff;border-radius:10px;overflow:hidden;border:1px solid #eee;">
            {''.join(rows)}
          </table>
        """)
    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#0d1b3e;margin:0 0 6px;font-size:22px;">
        {total} new event{'s' if total != 1 else ''} across {len(by_site)} site{'s' if len(by_site) != 1 else ''}
      </h2>
      <p style="color:#666;margin:0 0 12px;font-size:13px;">
        Scanned {datetime.utcnow().strftime('%b %d, %Y %H:%M UTC')}
      </p>
      {''.join(sections)}
      <p style="color:#999;font-size:11px;margin-top:24px;">
        Ticket Tools GCT · edit <code>sites.py</code> to add or remove watched sites
      </p>
    </div>
    """


def main():
    api_key = os.environ.get("SENDGRID_API_KEY")
    alert_email = os.environ.get("ALERT_EMAIL", "zfinkel1@gmail.com")
    from_email = os.environ.get("FROM_EMAIL", "noreply@sportscardnetwork.ai")

    if not api_key:
        print("[ERROR] SENDGRID_API_KEY missing", file=sys.stderr)
        sys.exit(1)

    new_by_site = {}
    exit_code = 0

    for site in SITES:
        name = site["name"]
        parser_type = site["parser"]
        parser = PARSERS.get(parser_type)
        if not parser:
            print(f"[ERROR] Unknown parser '{parser_type}' for {name}", file=sys.stderr)
            exit_code = 1
            continue

        print(f"\n[info] Checking {name} ({parser_type})")
        try:
            current = parser(site)
        except Exception as e:
            print(f"[ERROR] {name} parser failed: {e}", file=sys.stderr)
            traceback.print_exc()
            exit_code = 1
            continue

        print(f"[info] {name}: parsed {len(current)} events")

        state = load_state(site)
        known = {e["slug"] for e in state.get("events", [])}
        new_events = [e for e in current if e["slug"] not in known]
        is_first_run = not state.get("last_run")

        if is_first_run:
            print(f"[info] {name}: first run, baselining {len(current)} events")
        elif new_events:
            print(f"[info] {name}: {len(new_events)} NEW event(s)")
            new_by_site[name] = new_events
            for e in new_events:
                print(f"    + {e['name']} — {e.get('date','')} — {e['url']}")
        else:
            print(f"[info] {name}: no new events")

        save_state(site, current)

    if new_by_site:
        total = sum(len(v) for v in new_by_site.values())
        html = build_email(new_by_site)
        try:
            status = send_email(
                to_addr=alert_email,
                from_addr=from_email,
                subject=f"[Tickets] {total} new event{'s' if total != 1 else ''} across {len(new_by_site)} site{'s' if len(new_by_site) != 1 else ''}",
                html_body=html,
                api_key=api_key,
            )
            print(f"\n[info] Sent alert email, SendGrid returned {status}")
        except urllib.error.HTTPError as e:
            print(f"[ERROR] SendGrid {e.code}: {e.read().decode('utf-8', errors='ignore')}", file=sys.stderr)
            exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
