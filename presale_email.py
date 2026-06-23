#!/usr/bin/env python3
"""
GoldCoast Presale Email
=======================
Automated daily presale digest. Pulls the full presale list straight from the
Flare API (no CSV / Excel), scores every event with the same model the Presale
Analyzer UI uses, filters to the events whose presale opens on the TARGET day
(default: tomorrow), splits them into two buckets, and emails the digest:

  1. CHICAGOLAND  — local events (Chicago + suburbs) we can work in person
  2. NATIONWIDE   — everything else worth flipping online (uFlip)

Designed to run at ~4pm Central the day before, when the next day's presales are
posted. Reuses presale_analyzer.py for the API pull + scoring (no duplication).

Run:
  py presale_email.py                 # DRY RUN by default -> writes _presale-email-preview.html
  SEND=1 py presale_email.py          # actually send via SendGrid
  DAYS_AHEAD=2 py presale_email.py    # target 2 days out instead of tomorrow

Env (same names as the venue watcher so it slots into that Railway service):
  SENDGRID_API_KEY, FROM_EMAIL, ALERT_EMAIL
Optional: DAYS_AHEAD (default 1), MAX_NATIONWIDE (default 60), SEND=1, TZ handled as America/Chicago.
"""

import os, json, html, urllib.request
from collections import Counter
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import presale_analyzer as PA

CENTRAL = ZoneInfo("America/Chicago")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "1"))
MAX_NATIONWIDE = int(os.environ.get("MAX_NATIONWIDE", "60"))
SEND = os.environ.get("SEND") == "1"
# Stopgap when the Flare API is rate-limited: read the presale list from a Flare
# CSV/Excel export instead of the API. Same columns the Analyzer's upload uses.
PRESALE_FILE = os.environ.get("PRESALE_FILE")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "noreply@sportscardnetwork.ai")
# Presale recipients have their own var so they don't collide with the venue
# watcher's ALERT_EMAIL on the shared Railway service. Falls back to ALERT_EMAIL.
ALERT_EMAIL = os.environ.get("PRESALE_EMAIL_TO") or os.environ.get("ALERT_EMAIL", "")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")

# ── Chicagoland: Chicago + suburbs (driving radius). Downstate IL excluded. ──
CHICAGOLAND = {
    "chicago", "rosemont", "tinley park", "hoffman estates", "schaumburg",
    "naperville", "joliet", "skokie", "highland park", "evanston", "aurora",
    "cicero", "oak park", "des plaines", "arlington heights", "elgin",
    "waukegan", "berwyn", "oak lawn", "orland park", "downers grove",
    "elmhurst", "wheaton", "bolingbrook", "palatine", "mount prospect",
    "glenview", "northbrook", "lombard", "addison", "bridgeview", "lisle",
    "hometown", "cary", "crystal lake", "st. charles", "st charles",
    "geneva", "lincolnshire", "prospect heights", "rockford",
    "tinley", "homewood", "country club hills", "matteson", "midlothian",
}
# Downstate IL we explicitly do NOT count as Chicagoland
DOWNSTATE_IL = {"springfield", "bloomington", "normal", "champaign", "peoria",
                "carbondale", "moline", "decatur", "rock island", "quincy",
                "urbana", "edwardsville", "collinsville"}


def is_chicagoland(ev):
    city = (ev.get("city") or "").lower().strip()
    state = (ev.get("state") or "").upper().strip()
    # Chicagoland is all Illinois — guard against Flare's mislabeled rows
    # (e.g. "Joliet, CA" / "Berkeley→Joliet, CA"), which is bad data on their end.
    if state and state != "IL":
        return False
    if city in DOWNSTATE_IL:
        return False
    if city in CHICAGOLAND:
        return True
    # Anything Flare's metro map already folds into Chicago
    return PA.get_metro(city) == "chicago"


def parse_presale_windows(s):
    """presale_start is 'YYYY-MM-DD HH:MM:SS' values joined by '|'. Return datetimes."""
    out = []
    for chunk in str(s or "").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(datetime.strptime(chunk[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    return out


def load_from_file(path):
    """Read the presale list from a Flare CSV/Excel export and shape each event
    exactly like fetch_presale_api returns, so scoring/filtering are identical.
    Columns match the Analyzer's upload: EventName, URL, VenueName, VenueLocation,
    EventDate, PresaleName, PresaleStartLocalDate, Segment."""
    import pandas as pd
    df = pd.read_excel(path) if str(path).lower().endswith((".xlsx", ".xls")) else pd.read_csv(path)
    US = {'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA',
          'ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK',
          'OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY','DC'}
    CA = {'ON','BC','QC','AB','MB','SK','NS','NB','NL','PE','NT','YT','NU'}
    events = {}
    for _, row in df.iterrows():
        name = str(row.get("EventName", "") or "").strip()
        if not name or name == "nan":
            continue
        location = str(row.get("VenueLocation", "") or "").strip()
        city, state, country = PA.parse_location(location)
        if country == "CA" or state in CA:
            continue
        if country and country not in ("US", ""):
            continue
        if not country and state and state.strip().upper() not in US:
            continue
        url = str(row.get("URL", "") or "").strip()
        key = url or name
        ev = events.get(key)
        if ev is None:
            event_date = str(row.get("EventDate", "") or "").strip()
            try:
                event_date = pd.to_datetime(event_date).strftime("%Y-%m-%dT%H:%M:%S")
            except Exception:
                pass
            ev = events[key] = {
                "name": name, "url": url,
                "venue": str(row.get("VenueName", "") or "").strip(),
                "city": city, "state": state, "location": location or f"{city}, {state}",
                "event_date": event_date, "days_out": PA.days_until(event_date),
                "segment": str(row.get("Segment", "") or "").strip(),
                "presales": [], "_starts": set(),
            }
        pn = str(row.get("PresaleName", "") or "").strip()
        if pn and pn != "nan":
            ev["presales"].append(pn)
        # Use the GMT column and convert to Central so ALL times are one tz
        # (PresaleStartLocalDate is each venue's local tz — not comparable).
        ps = row.get("PresaleStartGMT")
        try:
            dt = pd.to_datetime(ps, utc=True).tz_convert("America/Chicago")
            if pd.notna(dt):
                ev["_starts"].add(dt.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    out = []
    for ev in events.values():
        pl = [p.lower() for p in ev["presales"]]
        ev["presale_start"] = "|".join(sorted(ev.pop("_starts")))
        ev["presale_count"] = len(set(ev["presales"]))
        ev["has_spotify"] = any("spotify" in p for p in pl)
        ev["has_platinum"] = any("platinum" in p for p in pl)
        ev["has_artist"] = any("artist" in p for p in pl)
        ev["has_ln"] = any("live nation" in p for p in pl)
        ev["has_vip"] = any("vip" in p for p in pl)
        ev["has_citi"] = any("citi" in p for p in pl)
        ev["has_amex"] = any("amex" in p or "american express" in p for p in pl)
        ev["has_chase"] = any("chase" in p for p in pl)
        ev["capacity"] = PA.get_cap(ev["venue"], ev["city"])
        ev["category"] = PA.categorize_event(ev["name"], ev["segment"])
        out.append(ev)
    return out


def score_all(events):
    """Apply the Presale Analyzer scoring model to every event (no API calls)."""
    tour_counts = Counter(e["name"] for e in events)
    city_counts = Counter((e["name"], PA.get_metro(e.get("city") or "")) for e in events)
    for ev in events:
        score = 0
        if ev.get("has_spotify"):  score += 10
        if ev.get("has_platinum"): score += 10
        if ev.get("has_artist"):   score += 7
        if ev.get("has_ln"):       score += 5
        if ev.get("has_vip"):      score += 4
        if ev.get("has_citi"):     score += 3
        if ev.get("has_amex"):     score += 3
        if ev.get("has_chase"):    score += 2

        pc = ev.get("presale_count") or len(set(ev.get("presales") or []))
        if pc >= 8:   score += 10
        elif pc >= 6: score += 8
        elif pc >= 4: score += 6
        elif pc >= 3: score += 4
        elif pc >= 2: score += 2

        import re as _re
        src = "sports" if _re.search(r"\b(vs\.?|@)\b", ev.get("name") or "", _re.I) else "concert"
        score += PA.day_of_week_score(ev.get("event_date") or "", ev.get("name") or "", src)
        score += PA.days_out_score(ev.get("days_out"))
        score += PA.tour_size_score(tour_counts[ev["name"]])
        score += PA.city_saturation_score(city_counts[(ev["name"], PA.get_metro(ev.get("city") or ""))])
        score += PA.city_market_score(ev.get("city") or "")

        # GCT sales history — the strongest signal (our own avg margin / win rate)
        hist = PA.lookup_artist_history(ev.get("name") or "", getattr(PA, "_history_db", {}) or {})
        hs, _ = PA.history_score(hist)
        score += hs
        ev["hist"] = hist

        score = min(99, max(1, score))
        ev["score"] = score
        ev["rec"] = "buy" if score >= 40 else "watch" if score >= 25 else "skip"
    return events


def fmt_time(dt):
    return dt.strftime("%-I:%M %p") if os.name != "nt" else dt.strftime("%#I:%M %p")


def fmt_event_date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").strftime("%a %b %-d" if os.name != "nt" else "%a %b %#d")
    except Exception:
        return s or "—"


def collect_for_day(events, target_day):
    """Return events that have a presale window opening on target_day, with the
    earliest such window time attached for display."""
    out = []
    for ev in events:
        windows = [w for w in parse_presale_windows(ev.get("presale_start")) if w.date() == target_day]
        if not windows:
            continue
        ev = dict(ev)
        ev["_open_time"] = min(windows)
        out.append(ev)
    out.sort(key=lambda e: (-e.get("score", 0), e["_open_time"]))
    return out


REC_COLOR = {"buy": "#1a9c4c", "watch": "#c98a00", "skip": "#888"}


def hist_line(ev):
    """Our GCT sales track record for this artist, if we have one."""
    h = ev.get("hist")
    if not h or not h.get("events"):
        return ""
    m = h.get("avg_margin", 0) or 0
    n = h.get("events", 0)
    p = h.get("profitable_pct", 0) or 0
    sell = h.get("avg_sell_price", 0) or 0
    cost = h.get("avg_cost_price", 0) or 0
    color = "#1a9c4c" if m >= 15 else "#c98a00" if m >= 0 else "#c0392b"
    money = ""
    if sell:
        money = f" · avg sale ${sell:,.0f}" + (f" / cost ${cost:,.0f}" if cost else "")
    return (f'<div style="font-size:11px;font-weight:700;color:{color}">'
            f'\U0001F4CA {n} past show{"s" if n != 1 else ""}{money} · {m:+.0f}% margin · {p:.0f}% profitable</div>')


def buyunder_line(ev):
    """Buy-under price + venue/artist comps from our av/vo/ao history tables
    (local lookup, no API). Picks the tier we have the most data on."""
    try:
        data = PA.get_smart_buy_under(ev.get("name") or "", ev.get("venue") or "")
    except Exception:
        data = None
    if not data:
        return ""
    tier, d = max(data.items(), key=lambda kv: kv[1].get("count", 0))
    if not d.get("buy_under"):
        return ""
    return (f'<div style="font-size:11px;color:#1156cc">'
            f'\U0001F4B0 {html.escape(tier)}: buy under <b>${d["buy_under"]:,.0f}</b> '
            f'· {d.get("count", 0):,} comps @ avg ${d.get("avg_sell", 0):,.0f}</div>')


def row_html(ev):
    rec = ev.get("rec", "skip")
    color = REC_COLOR.get(rec, "#888")
    name = html.escape(ev.get("name") or "")
    url = ev.get("url") or ""
    name_cell = f'<a href="{html.escape(url)}" style="color:#1156cc;text-decoration:none;font-weight:600">{name}</a>' if url else f'<b>{name}</b>'
    venue = html.escape(ev.get("venue") or "")
    loc = html.escape(ev.get("location") or ev.get("city") or "")
    cap = ev.get("capacity")
    cap_s = f"{cap:,}" if isinstance(cap, int) else "—"
    presales = ", ".join(ev.get("presales") or []) or "—"
    return f"""<tr>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;text-align:center">
    <div style="font-size:18px;font-weight:800;color:{color}">{ev.get('score','')}</div>
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:{color}">{rec}</div>
  </td>
  <td style="padding:8px 10px;border-bottom:1px solid #eee">
    {name_cell}
    {hist_line(ev)}
    {buyunder_line(ev)}
    <div style="font-size:11px;color:#666">{html.escape(presales[:90])}</div>
  </td>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:12px;color:#333">{venue}<div style="font-size:11px;color:#888">{loc}</div></td>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:12px;color:#333;white-space:nowrap">{fmt_event_date(ev.get('event_date'))}</td>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:12px;font-weight:700;color:#1156cc;white-space:nowrap">{fmt_time(ev['_open_time'])}</td>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:12px;color:#333;text-align:right">{cap_s}</td>
</tr>"""


def section_html(title, subtitle, rows, accent):
    if not rows:
        body = '<tr><td style="padding:14px;color:#999;font-size:13px">No presales opening this day.</td></tr>'
    else:
        body = "".join(row_html(e) for e in rows)
    return f"""
  <div style="margin:0 0 28px">
    <div style="background:{accent};color:#fff;padding:10px 14px;border-radius:8px 8px 0 0;font-size:15px;font-weight:800">{title}
      <span style="font-weight:500;opacity:.85;font-size:12px"> — {subtitle}</span></div>
    <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #eee;border-top:none;border-radius:0 0 8px 8px;overflow:hidden">
      <tr style="background:#fafafa">
        <th style="padding:6px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#888;text-align:center">Score</th>
        <th style="padding:6px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#888;text-align:left">Event / Presales</th>
        <th style="padding:6px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#888;text-align:left">Venue</th>
        <th style="padding:6px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#888;text-align:left">Event date</th>
        <th style="padding:6px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#888;text-align:left">Opens</th>
        <th style="padding:6px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#888;text-align:right">Cap</th>
      </tr>
      {body}
    </table>
  </div>"""


def build_email(target_day, chi, nat, nat_total):
    day_label = target_day.strftime("%A, %B %-d" if os.name != "nt" else "%A, %B %#d")
    nat_note = f" (top {len(nat)} of {nat_total})" if nat_total > len(nat) else ""
    sections = section_html(f"🏙️ Chicagoland — {len(chi)}", "local, in-person", chi, "#0b3d91")
    sections += section_html(f"🌎 Nationwide / uFlip — {nat_total}{nat_note}", "online flips", nat, "#1a7a4c")
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f4f5f7;font-family:-apple-system,Segoe UI,Arial,sans-serif">
  <div style="max-width:760px;margin:0 auto;padding:20px">
    <div style="padding:6px 2px 16px">
      <div style="font-size:20px;font-weight:800;color:#111">GoldCoast Presale Digest</div>
      <div style="font-size:13px;color:#666">Presales opening <b>{day_label}</b> · {len(chi)} Chicagoland · {nat_total} nationwide</div>
    </div>
    {sections}
    <div style="font-size:11px;color:#999;padding:8px 2px 24px">
      Scored by the Presale Analyzer model (buy ≥40 · watch ≥25 · skip). Pulled live from Flare. Times Central.
    </div>
  </div>
</body></html>"""


def send_email(subject, html_body):
    if not (SENDGRID_API_KEY and ALERT_EMAIL):
        raise RuntimeError("SENDGRID_API_KEY and ALERT_EMAIL must be set to send.")
    payload = {
        "personalizations": [{"to": [{"email": e.strip()} for e in ALERT_EMAIL.split(",") if e.strip()]}],
        "from": {"email": FROM_EMAIL, "name": "GoldCoast Presales"},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
        "categories": ["presale-digest"],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main():
    now = datetime.now(CENTRAL)
    target_day = (now + timedelta(days=DAYS_AHEAD)).date()
    print(f"[presale-email] target day: {target_day} (today {now.date()} Central, +{DAYS_AHEAD})")

    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_flare_presale_cache.json")
    if PRESALE_FILE:
        events = load_from_file(PRESALE_FILE)
        print(f"[presale-email] loaded {len(events)} events from file: {PRESALE_FILE}")
    elif os.environ.get("USE_CACHE") == "1" and os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            events = json.load(f)
        print(f"[presale-email] loaded {len(events)} events from cache")
    else:
        events = PA.fetch_presale_api()
        if isinstance(events, dict):
            print("[presale-email] Flare API error:", events)
            return 1
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(events, f)
        except Exception:
            pass
    score_all(events)

    todays = collect_for_day(events, target_day)
    chi = [e for e in todays if is_chicagoland(e)]
    # Nationwide = online flips worth it — buys + watches only (skip the skips)
    nat_all = [e for e in todays if not is_chicagoland(e) and e.get("rec") in ("buy", "watch")]
    nat = nat_all[:MAX_NATIONWIDE]
    print(f"[presale-email] {len(todays)} presales open {target_day}: {len(chi)} Chicagoland, {len(nat_all)} nationwide")

    if not todays:
        print("[presale-email] nothing opening that day — no email sent.")
        return 0

    body = build_email(target_day, chi, nat, len(nat_all))
    subject = f"Presales {target_day.strftime('%b %-d' if os.name!='nt' else '%b %#d')}: {len(chi)} Chicagoland · {len(nat_all)} nationwide"

    if SEND:
        status = send_email(subject, body)
        print(f"[presale-email] sent ({status}) to {ALERT_EMAIL}")
    else:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_presale-email-preview.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"[presale-email] DRY RUN — wrote preview -> {out}")
        print(f"[presale-email] subject: {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
