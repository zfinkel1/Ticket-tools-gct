#!/usr/bin/env python3
"""
Weekly summary — sends a "system is alive" email every Monday morning showing
what's being watched and what's currently tracked, even if no new events were
detected during the week. Hits the same recipients as the alert emails.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"


def send_email(to_addrs, from_addr, subject, html_body, api_key):
    if isinstance(to_addrs, str):
        recipients = [a.strip() for a in to_addrs.split(",") if a.strip()]
    else:
        recipients = [a.strip() for a in to_addrs if a.strip()]
    payload = {
        "personalizations": [{"to": [{"email": addr} for addr in recipients]}],
        "from": {"email": from_addr, "name": "Ticket Tools GCT"},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def load_states():
    sites = []
    for p in sorted(STATE_DIR.glob("*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        last_run = d.get("last_run") or ""
        try:
            last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00")) if last_run else None
        except Exception:
            last_dt = None
        age_hrs = None
        if last_dt:
            age_hrs = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        sites.append({
            "name": d.get("site") or p.stem,
            "count": d.get("count", 0),
            "last_run": last_run,
            "age_hrs": age_hrs,
            "events": d.get("events", []),
        })
    return sites


def build_email(sites):
    total = sum(s["count"] for s in sites)
    sections = []
    for s in sites:
        # Take up to 5 sample upcoming events
        sample_rows = []
        for e in s["events"][:5]:
            name = e.get("name", "Unnamed")
            date = e.get("date") or "TBA"
            url = e.get("url", "#")
            loc = e.get("location") or ""
            sample_rows.append(f"""
              <tr><td style="padding:10px 14px;border-bottom:1px solid #eee;">
                <div style="font-size:13px;font-weight:700;color:#0d1b3e;margin-bottom:2px;">
                  <a href="{url}" style="color:#0d1b3e;text-decoration:none;">{name}</a>
                </div>
                <div style="font-size:11px;color:#666;">{date}{' &middot; ' + loc if loc else ''}</div>
              </td></tr>
            """)

        # Health indicator
        if s["age_hrs"] is None:
            health = '<span style="color:#dc2626;font-weight:700;">Never run</span>'
        elif s["age_hrs"] > 48:
            health = f'<span style="color:#dc2626;font-weight:700;">Stale ({int(s["age_hrs"])}h ago)</span>'
        elif s["age_hrs"] > 2:
            health = f'<span style="color:#f59e0b;font-weight:700;">{int(s["age_hrs"])}h ago</span>'
        else:
            mins = int(s["age_hrs"] * 60)
            health = f'<span style="color:#16a34a;font-weight:700;">{mins}m ago</span>'

        sections.append(f"""
          <div style="margin-bottom:24px;">
            <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">
              <h3 style="margin:0;font-size:14px;letter-spacing:0.05em;color:#c9a227;text-transform:uppercase;">
                {s['name']} &mdash; {s['count']} events
              </h3>
              <span style="font-size:11px;">{health}</span>
            </div>
            <table style="width:100%;border-collapse:collapse;background:#fafbff;border-radius:10px;overflow:hidden;border:1px solid #eee;">
              {''.join(sample_rows) if sample_rows else '<tr><td style="padding:14px;font-size:12px;color:#999;">No events tracked</td></tr>'}
            </table>
          </div>
        """)

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#0d1b3e;margin:0 0 6px;font-size:22px;">Ticket Watcher &mdash; Weekly Health Check</h2>
      <p style="color:#666;margin:0 0 6px;font-size:13px;">
        {datetime.utcnow().strftime('%A, %B %d, %Y &middot; %H:%M UTC')}
      </p>
      <p style="color:#666;margin:0 0 20px;font-size:13px;">
        Watching <strong>{len(sites)}</strong> sites &middot; <strong>{total}</strong> total events tracked &middot;
        You'll get individual alert emails when new events show up. This summary is just a "system is alive" check.
      </p>
      {''.join(sections)}
      <p style="color:#999;font-size:11px;margin-top:24px;">
        Edit watched sites in <code>sites.py</code>. Cancel weekly summary by removing the workflow at <code>.github/workflows/weekly.yml</code>.
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

    sites = load_states()
    if not sites:
        print("[info] No state files found, nothing to summarize")
        return

    html = build_email(sites)
    total = sum(s["count"] for s in sites)
    try:
        status = send_email(
            to_addrs=alert_email,
            from_addr=from_email,
            subject=f"[Tickets] Weekly summary — {len(sites)} sites, {total} events",
            html_body=html,
            api_key=api_key,
        )
        print(f"[info] Sent weekly summary, SendGrid returned {status}")
    except urllib.error.HTTPError as e:
        print(f"[ERROR] SendGrid {e.code}: {e.read().decode('utf-8', errors='ignore')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
