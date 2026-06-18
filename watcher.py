#!/usr/bin/env python3
"""
Multi-site Ticket Watcher.

For each site in sites.py:
  1. Call the appropriate parser to get current events
  2. Diff against state/{slug}.json
  3. Email (one combined email) if any new events across all sites
  4. Save updated state — ONLY for sites where the email succeeded.
     Sites with no new events save immediately. Sites with new events
     defer their save until SendGrid confirms delivery, so a transient
     SendGrid failure doesn't bake unsent alerts into state.

Optional: --dry-run skips the SendGrid call and the state writes
(diff and enrichment still run). Useful when iterating on parsers.

Required env vars:
  SENDGRID_API_KEY
  ALERT_EMAIL
  FROM_EMAIL
"""

import argparse
import html
import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from parsers import PARSERS
from sites import SITES
from enrich.spotify import enrich_event, format_followers, popularity_label
from enrich.scoring import score_event, predict_tiers
from enrich.flare import (
    enrich_event_with_history,
    history_html,
    enrich_event_with_current_sold,
    current_sold_html,
)
from enrich.stubhub import enrich_event_with_listing, current_listing_html
from enrich.sales import sales_status_html, price_range_html
from enrich.recommend import recommendation_html

# State lives next to the code by default (GitHub Actions commits it back), but
# on Railway a VOLUME is mounted and we write state there so it survives
# redeploys. Set RAILWAY_VOLUME_MOUNT_PATH (Railway does this automatically when
# a volume is attached) to switch.
STATE_DIR = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or str(Path(__file__).parent)) / "state"
HEALTH_PATH = STATE_DIR / "health.json"

# A site is "stale" if its last successful run is older than this. Triggers
# a watcher-health alert email. Threshold matches "missed ~48 cron cycles
# at 15-min interval" — strong signal a parser is broken, not transient.
STALE_HOURS = 12
# Only re-send the same stale alert once per this window so a broken parser
# doesn't spam the inbox every 15 min.
HEALTH_ALERT_COOLDOWN_HOURS = 12


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
        "last_run": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(events),
        "events": events,
    }
    with open(state_path(site), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def send_email(to_addrs, from_addr, subject, html_body, api_key):
    """to_addrs: comma-separated string OR list of email addresses."""
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
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def _score_block_html(e):
    """Render the buy/watch/skip badge + a 'why' sentence for the email."""
    sd = e.get("score_data")
    if not sd: return ""
    score = sd.get("score", 0)
    rec = sd.get("rec", "skip")
    comp = sd.get("components", {})
    bg = {"buy": "#16a34a", "watch": "#f59e0b", "skip": "#dc2626"}.get(rec, "#666")
    label = {"buy": "BUY", "watch": "WATCH", "skip": "SKIP"}[rec]
    why = comp.get("why", "")
    # Compact breakdown of non-zero components for the curious reader
    breakdown = []
    for k, v in comp.items():
        if k in ("history_label", "pattern_labels", "why"): continue
        if isinstance(v, (int, float)) and v != 0:
            breakdown.append(f"{k}:{v:+d}")
    return (
        f'<div style="margin-top:6px;">'
        f'<span style="display:inline-block;background:{bg};color:#fff;font-size:11px;font-weight:800;'
        f'padding:3px 9px;border-radius:4px;letter-spacing:0.04em;">{label} {score}/99</span>'
        f'</div>'
        + (f'<div style="font-size:12px;color:#333;margin-top:5px;line-height:1.45;">{html.escape(why)}</div>' if why else "")
        + (f'<div style="font-size:10px;color:#999;margin-top:3px;">Signals: {" · ".join(breakdown)}</div>' if breakdown else "")
    )


def _tier_predictions_html(e):
    """Render the tier-level resale predictions table for the email."""
    preds = e.get("tier_predictions") or []
    if not preds: return ""
    rows = []
    for p in preds:
        margin = p["predicted_margin"]
        mcolor = "#16a34a" if margin >= 25 else "#f59e0b" if margin >= 10 else "#dc2626"
        rows.append(
            f'<tr><td style="padding:3px 6px;font-size:11px;color:#333;">{html.escape(p["tier"])}</td>'
            f'<td style="padding:3px 6px;font-size:11px;color:#666;text-align:right;">${p["predicted_face"]:.0f}</td>'
            f'<td style="padding:3px 6px;font-size:11px;color:#333;text-align:right;">${p["predicted_resale"]:.0f}</td>'
            f'<td style="padding:3px 6px;font-size:11px;color:{mcolor};font-weight:700;text-align:right;">{margin:+.0f}%</td>'
            f'<td style="padding:3px 6px;font-size:10px;color:#999;text-align:right;">n={p["count"]}</td></tr>'
        )
    return (
        '<div style="margin-top:8px;">'
        '<div style="font-size:10px;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:0.04em;margin-bottom:3px;">'
        'Best tiers (predicted face / resale / margin)</div>'
        '<table style="width:100%;border-collapse:collapse;">'
        + "".join(rows)
        + '</table></div>'
    )


def _enrichment_html(e):
    enr = e.get("enrichment") or {}
    if not enr:
        return ""
    pop = enr.get("popularity")
    followers = enr.get("followers")
    label_color = popularity_label(pop)
    parts = []
    if label_color:
        label, color = label_color
        parts.append(
            f'<span style="display:inline-block;background:{color};color:#fff;'
            f'font-size:10px;font-weight:800;padding:2px 7px;border-radius:4px;'
            f'letter-spacing:0.04em;text-transform:uppercase;">{label} {pop}</span>'
        )
    if followers:
        parts.append(
            f'<span style="font-size:11px;color:#666;font-weight:600;">'
            f'{format_followers(followers)} followers</span>'
        )
    genres = enr.get("genres") or []
    if genres:
        parts.append(
            f'<span style="font-size:11px;color:#999;">{", ".join(genres[:2])}</span>'
        )
    if not parts:
        return ""
    return (
        '<div style="margin-top:6px;display:flex;gap:8px;align-items:center;'
        'flex-wrap:wrap;">' + "".join(parts) + "</div>"
    )


def build_email(by_site, baselined_sites=None):
    """by_site: {site_name: [new_events]}; baselined_sites: {site_name: [events]}"""
    baselined_sites = baselined_sites or {}
    total = sum(len(v) for v in by_site.values())
    sections = []
    for site_name, events in by_site.items():
        # Sort by score (highest first), falling back to popularity for events
        # that didn't get scored. Coerce popularity to int — Flare returns it
        # as a string sometimes ('75' not 75), which crashed the sort.
        def _sort_key(e):
            score = (e.get("score_data") or {}).get("score") or 0
            pop_raw = (e.get("enrichment") or {}).get("popularity") or 0
            try:
                pop = int(pop_raw)
            except (TypeError, ValueError):
                pop = 0
            return -(int(score) * 100 + pop)
        events = sorted(events, key=_sort_key)
        # Collapse same-name shows (multi-date runs + same-date dupes) into ONE
        # row, so a 4-night residency or a 1pm+7pm Monster Jam doesn't fire 4-5
        # near-identical alerts. Keep the best-scored instance's enrichment and
        # list all its dates. Per-date tracking still happens via slug, so a
        # genuinely new date added later still alerts (grouped under the name).
        groups = {}
        for e in events:  # already sorted by score desc, so [0] is the best
            gkey = re.sub(r"[^a-z0-9]", "", (e.get("name", "") or "").lower())
            groups.setdefault(gkey, []).append(e)
        rows = []
        for evs in groups.values():
            e = evs[0]
            name = html.escape(html.unescape(e.get("name", "Unnamed")))
            _dates = []
            for x in evs:
                dd = (x.get("date") or "").strip()
                if dd and dd not in _dates:
                    _dates.append(dd)
            if len(_dates) <= 1:
                date = html.escape(_dates[0] if _dates else "TBA")
            else:
                date = html.escape(f"{len(_dates)} dates · " + " · ".join(_dates[:3]) + (" …" if len(_dates) > 3 else ""))
            loc = html.escape(e.get("location") or "")
            url = html.escape(e.get("url", "#"), quote=True)
            enrichment_html = _enrichment_html(e)
            score_block = _score_block_html(e)
            tier_preds_block = _tier_predictions_html(e)
            # The old recommendation_html "STRONG WATCH / STRONG BUY" badge is
            # now duplicated by score_block. Removed to avoid 3 verdicts in 3
            # places (badge + score + why-sentence).
            sales_block = sales_status_html(e)
            price_block = price_range_html(e)
            history_block = history_html(e)
            current_sold_block = current_sold_html(e)
            current_listing_block = current_listing_html(e)
            rows.append(f"""
              <tr><td style="padding:14px 16px;border-bottom:1px solid #eee;">
                <div style="font-size:15px;font-weight:700;color:#0d1b3e;margin-bottom:4px;">
                  <a href="{url}" style="color:#0d1b3e;text-decoration:none;">{name} &rarr;</a>
                </div>
                <div style="font-size:13px;color:#666;">{date}{' &middot; ' + loc if loc else ''}</div>
                {score_block}
                {sales_block}
                {price_block}
                {enrichment_html}
                {history_block}
                {tier_preds_block}
                {current_sold_block}
                {current_listing_block}
              </td></tr>
            """)
        sections.append(f"""
          <h3 style="font-size:13px;letter-spacing:0.05em;color:#c9a227;text-transform:uppercase;margin:24px 0 10px;">
            {html.escape(site_name)} &mdash; {len(groups)} new
          </h3>
          <table style="width:100%;border-collapse:collapse;background:#fafbff;border-radius:10px;overflow:hidden;border:1px solid #eee;">
            {''.join(rows)}
          </table>
        """)

    # Append a "first-run baseline" section so adding a venue produces a
    # one-time visible notice rather than silent log lines only.
    if baselined_sites:
        baseline_rows = []
        for site_name, evs in baselined_sites.items():
            baseline_rows.append(
                f'<li style="margin-bottom:6px;font-size:13px;color:#444;">'
                f'<strong>{html.escape(site_name)}</strong> &mdash; now watching {len(evs)} events'
                f'</li>'
            )
        sections.append(f"""
          <div style="margin-top:24px;padding:14px 18px;background:#eef4ff;border-left:3px solid #4361ee;border-radius:6px;">
            <div style="font-size:12px;letter-spacing:0.05em;color:#4361ee;text-transform:uppercase;font-weight:700;margin-bottom:8px;">
              First run baselined
            </div>
            <ul style="margin:0;padding-left:18px;">{''.join(baseline_rows)}</ul>
            <div style="font-size:11px;color:#666;margin-top:8px;">
              Future runs will alert only on events added after this snapshot.
            </div>
          </div>
        """)

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#0d1b3e;margin:0 0 6px;font-size:22px;">
        {total} new event{'s' if total != 1 else ''} across {len(by_site)} site{'s' if len(by_site) != 1 else ''}
      </h2>
      <p style="color:#666;margin:0 0 12px;font-size:13px;">
        Scanned {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}
      </p>
      {''.join(sections)}
      <p style="color:#999;font-size:11px;margin-top:24px;">
        Ticket Tools GCT &middot; edit <code>sites.py</code> to add or remove watched sites
      </p>
    </div>
    """


# ───────────────────────── Health / stale-parser alerts ─────────────────────

def _load_health():
    if not HEALTH_PATH.exists():
        return {}
    try:
        with open(HEALTH_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_health(data):
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HEALTH_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def find_stale_sites():
    """Return list of {name, age_hrs} for sites whose last_run is too old."""
    stale = []
    for site in SITES:
        s = load_state(site)
        last = s.get("last_run")
        if not last:
            # Never run successfully — treat as stale only if state file exists,
            # because a brand-new venue has no state yet and shouldn't alert.
            if state_path(site).exists():
                stale.append({"name": site["name"], "age_hrs": None})
            continue
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if age >= STALE_HOURS:
                stale.append({"name": site["name"], "age_hrs": age})
        except Exception:
            continue
    return stale


def maybe_send_health_alert(stale, api_key, alert_email, from_email, dry_run):
    """Send (at most once per cooldown window) when sites are stale."""
    if not stale:
        return
    health = _load_health()
    last_alert = health.get("last_stale_alert_at")
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if last_alert:
        try:
            last_dt = datetime.fromisoformat(last_alert.replace("Z", "+00:00"))
            hrs_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hrs_since < HEALTH_ALERT_COOLDOWN_HOURS:
                print(f"[health] {len(stale)} stale sites, but cooldown active ({hrs_since:.1f}h since last alert)")
                return
        except Exception:
            pass

    rows = []
    for s in stale:
        age = s.get("age_hrs")
        age_text = f"{int(age)}h ago" if age is not None else "never run"
        rows.append(f'<li style="margin-bottom:6px;"><strong>{html.escape(s["name"])}</strong> &mdash; {age_text}</li>')
    body = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#dc2626;margin:0 0 6px;font-size:22px;">Watcher health: {len(stale)} stale site{'s' if len(stale) != 1 else ''}</h2>
      <p style="color:#666;margin:0 0 12px;font-size:13px;">
        These parsers haven't successfully run in &gt; {STALE_HOURS}h. Likely a parser break or upstream change.
      </p>
      <ul style="font-size:13px;color:#444;">{''.join(rows)}</ul>
      <p style="color:#999;font-size:11px;margin-top:24px;">
        Re-alert cooldown: {HEALTH_ALERT_COOLDOWN_HOURS}h. Investigate logs at github.com/zfinkel1/Ticket-tools-gct/actions.
      </p>
    </div>
    """
    if dry_run:
        print(f"[dry-run] would send health alert for {len(stale)} stale sites")
        return
    try:
        status = send_email(
            to_addrs=alert_email,
            from_addr=from_email,
            subject=f"[Tickets] Watcher health: {len(stale)} stale site{'s' if len(stale) != 1 else ''}",
            html_body=body,
            api_key=api_key,
        )
        print(f"[health] sent stale-sites alert, SendGrid {status}")
        health["last_stale_alert_at"] = now_iso
        _save_health(health)
    except Exception as e:
        print(f"[health] failed to send stale alert: {e}", file=sys.stderr)


# ───────────────────────────── Main ─────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and diff, but skip email + state writes")
    parser.add_argument("--priority", choices=["fast", "normal", "all"], default="all",
                        help="fast = sites tagged priority=fast (every 5 min cron); "
                             "normal = everything else (every 15 min cron); all = no filter")
    args = parser.parse_args()

    # Filter sites by priority tag. Sites without an explicit "priority" key
    # default to "normal" so they keep running on the existing 15-min cron.
    if args.priority == "fast":
        sites_to_run = [s for s in SITES if s.get("priority") == "fast"]
    elif args.priority == "normal":
        sites_to_run = [s for s in SITES if s.get("priority", "normal") != "fast"]
    else:
        sites_to_run = list(SITES)
    if not sites_to_run:
        print(f"[INFO] No sites match priority={args.priority}; nothing to do.")
        return

    api_key = os.environ.get("SENDGRID_API_KEY")
    alert_email = os.environ.get("ALERT_EMAIL", "zfinkel1@gmail.com")
    # Error/health alerts go to a separate recipient list (default: just the
    # operator) so noisy parser-failure or stale-site alerts don't spam the
    # team list that gets the new-event emails. Fall back to ALERT_EMAIL if
    # ERROR_EMAIL isn't configured.
    error_email = os.environ.get("ERROR_EMAIL") or "zfinkel1@gmail.com"
    from_email = os.environ.get("FROM_EMAIL", "noreply@sportscardnetwork.ai")

    if not api_key and not args.dry_run:
        print("[ERROR] SENDGRID_API_KEY missing", file=sys.stderr)
        sys.exit(1)

    new_by_site = {}
    baselined_sites = {}
    pending_saves = []   # [(site, current_events)] — defer until email succeeds
    exit_code = 0

    for site in sites_to_run:
        name = site["name"]
        parser_type = site["parser"]
        site_parser = PARSERS.get(parser_type)
        if not site_parser:
            print(f"[ERROR] Unknown parser '{parser_type}' for {name}", file=sys.stderr)
            exit_code = 1
            continue

        # Throttle: skip sites that ran recently. DEFAULT 1h (hourly) — new-show
        # announcements appear daily/weekly, not minute-to-minute, so hourly
        # catches them while keeping credit use tiny. Override per-site (TAO uses
        # 0.25 = 15 min for late add-on drops).
        min_hrs = site.get("min_interval_hours", 1.0)
        if min_hrs:
            state = load_state(site)
            last = state.get("last_run")
            if last:
                try:
                    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    age_hrs = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                    if age_hrs < min_hrs:
                        print(f"[info] Skipping {name}: last run {age_hrs:.1f}h ago, throttle {min_hrs}h")
                        continue
                except Exception:
                    pass

        print(f"\n[info] Checking {name} ({parser_type})")
        try:
            current = site_parser(site)
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
            baselined_sites[name] = current
            # Defer save until email — keeps the baseline notice + state save atomic
            pending_saves.append((site, current))
        elif new_events:
            print(f"[info] {name}: {len(new_events)} NEW event(s)")
            new_by_site[name] = new_events
            for e in new_events:
                print(f"    + {e['name']} - {e.get('date','')} - {e['url']}")
            # Defer save until email succeeds, so a SendGrid failure doesn't
            # silently consume these new events.
            pending_saves.append((site, current))
        else:
            print(f"[info] {name}: no new events")
            # Safe to save immediately — nothing depends on email delivery.
            if not args.dry_run:
                save_state(site, current)

    # Send email if anything new (or if first-run baselines need to be reported)
    if new_by_site or baselined_sites:
        # Enrich new events with all the signals
        for events in new_by_site.values():
            for ev in events:
                try:
                    enrich_event(ev)
                except Exception as e:
                    print(f"[warn] spotify enrich failed for {ev.get('name','?')}: {e}")
                try:
                    # Score the event using historical data + derived signals.
                    # Lightweight — no API calls, just reads cached JSON.
                    ev["score_data"] = score_event(
                        ev.get("name", ""), ev.get("location", ""), ev.get("date", "")
                    )
                    ev["tier_predictions"] = predict_tiers(
                        ev.get("name", ""), ev.get("location", "")
                    )
                except Exception as e:
                    print(f"[warn] scoring failed for {ev.get('name','?')}: {e}")
                try:
                    enrich_event_with_history(ev)
                except Exception as e:
                    print(f"[warn] gct history failed for {ev.get('name','?')}: {e}")
                try:
                    enrich_event_with_current_sold(ev)
                except Exception as e:
                    print(f"[warn] current sold lookup failed for {ev.get('name','?')}: {e}")
                try:
                    enrich_event_with_listing(ev)
                except Exception as e:
                    print(f"[warn] stubhub listing scrape failed for {ev.get('name','?')}: {e}")

        body = build_email(new_by_site, baselined_sites=baselined_sites)
        total = sum(len(v) for v in new_by_site.values())
        if baselined_sites and not new_by_site:
            subject = f"[Tickets] Now watching {len(baselined_sites)} new site{'s' if len(baselined_sites) != 1 else ''}"
        else:
            n_sites = len(new_by_site)
            subject = f"[Tickets] {total} new event{'s' if total != 1 else ''} across {n_sites} site{'s' if n_sites != 1 else ''}"

        if args.dry_run:
            print(f"\n[dry-run] would send email: {subject}")
            print(f"[dry-run] body length: {len(body)} chars")
            print(f"[dry-run] would save state for {len(pending_saves)} sites")
        else:
            try:
                status = send_email(alert_email, from_email, subject, body, api_key)
                print(f"\n[info] Sent alert email, SendGrid returned {status}")
                # Email confirmed delivered; commit the deferred saves now.
                for (site, current) in pending_saves:
                    save_state(site, current)
                print(f"[info] Saved state for {len(pending_saves)} sites after email confirm")
            except urllib.error.HTTPError as e:
                print(f"[ERROR] SendGrid {e.code}: {e.read().decode('utf-8', errors='ignore')}", file=sys.stderr)
                # State NOT saved for sites with new events — they will re-trigger
                # on the next cron run, which is the correct fail-open behavior.
                exit_code = 1
            except Exception as e:
                print(f"[ERROR] SendGrid call failed: {e}", file=sys.stderr)
                exit_code = 1
    else:
        # No new events anywhere — nothing was deferred, all state is current.
        pass

    # Health check: alert if any parser hasn't run successfully in too long
    if not args.dry_run:
        try:
            stale = find_stale_sites()
            if stale:
                print(f"[health] {len(stale)} stale site(s): {[s['name'] for s in stale]}")
                # Health alerts go to error_email only, not the team list.
                maybe_send_health_alert(stale, api_key, error_email, from_email, args.dry_run)
        except Exception as e:
            print(f"[warn] health check failed: {e}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
