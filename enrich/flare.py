#!/usr/bin/env python3
"""
Flare (TicketFlipping) enrichment for new event alerts.

For each new event detected by the watcher, look up past shows by the
same artist that GCT has previously tracked in Flare and surface:
  - count of past shows tracked
  - last show date / venue
  - aggregate sold count and average resale price (if StubHub data exists)
  - GCT-specific resale-margin signal vs face value

The all-events list is large and Flare rate-limits this endpoint to 4
calls/day, so we snapshot it once a day to state/flare-events-cache.json
and reuse the snapshot for every watcher run that day. Sold-data
lookups (limit 1000/day) are cached per event_id with a 7-day TTL.

Usage:
    from enrich.flare import enrich_event_with_history
    enrich_event_with_history(event)  # mutates event in place
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .spotify import extract_artist  # reuse the artist-name parser

FLARE_BASE = "https://flare.ticketflipping.com"
FLARE_TOKEN_DEFAULT = "db2fa5131eb5750f1fae2fe167a09219a95e86fc"

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
EVENTS_CACHE_PATH = STATE_DIR / "flare-events-cache.json"
SOLD_CACHE_PATH = STATE_DIR / "flare-sold-cache.json"

EVENTS_TTL_SECONDS = 24 * 3600       # All-events: refresh once a day
SOLD_TTL_SECONDS = 7 * 24 * 3600     # Sold data per event: 1 week
HISTORY_LOOKBACK_DAYS = 365 * 3      # 3 years of past shows


def _token():
    return os.environ.get("FLARE_TOKEN") or FLARE_TOKEN_DEFAULT


def _read_json(path):
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "TicketWatcher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_all_events(force=False):
    """
    Returns the cached Flare all-events list. Refreshes from Flare if cache
    is older than EVENTS_TTL_SECONDS or missing. Returns [] on failure.
    """
    cache = _read_json(EVENTS_CACHE_PATH) or {}
    if not force and cache.get("fetched_at"):
        if time.time() - cache["fetched_at"] < EVENTS_TTL_SECONDS:
            return cache.get("events", [])

    url = f"{FLARE_BASE}/api/all-events?token={_token()}&axs_marketplace=true"
    try:
        data = _http_get(url, timeout=60)
    except Exception as e:
        print(f"[flare] all-events fetch failed: {e}")
        # Fall back to stale cache rather than nothing
        return cache.get("events", [])

    events = data.get("data") if isinstance(data, dict) else None
    if not events:
        # Some Flare responses use the top-level list shape
        events = data if isinstance(data, list) else []

    _write_json(EVENTS_CACHE_PATH, {"fetched_at": time.time(), "events": events})
    print(f"[flare] cached {len(events)} all-events records")
    return events


def fetch_sold_data(sh_id):
    """Returns sold-data list for a StubHub event, cached locally."""
    if not sh_id:
        return []
    cache = _read_json(SOLD_CACHE_PATH) or {}
    key = str(sh_id)
    entry = cache.get(key)
    if entry and time.time() - entry.get("fetched_at", 0) < SOLD_TTL_SECONDS:
        return entry.get("data", [])

    url = f"{FLARE_BASE}/api/get-sold-data?token={_token()}&site_event_id={sh_id}&market=SH"
    try:
        data = _http_get(url, timeout=20)
    except Exception as e:
        print(f"[flare] sold-data fetch failed for sh_id={sh_id}: {e}")
        return entry.get("data", []) if entry else []

    sold = data.get("data") if isinstance(data, dict) else (data or [])
    cache[key] = {"fetched_at": time.time(), "data": sold or []}
    _write_json(SOLD_CACHE_PATH, cache)
    return sold or []


def _normalize(s):
    """Lowercase + strip non-alphanumeric for fuzzy matching."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _event_artist(event):
    """Extract artist name from a Flare event record."""
    name = event.get("name") or event.get("event_name") or ""
    return extract_artist(name)


def _event_date(event):
    """Parse event_date to a datetime, or None."""
    raw = event.get("event_date") or event.get("date") or ""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)[:19].replace("Z", ""))
    except Exception:
        return None


def find_history(artist_name, max_shows=20):
    """
    Returns a list of past Flare events for this artist, sorted newest
    first. Only includes shows whose event_date is in the past and
    within HISTORY_LOOKBACK_DAYS.
    """
    if not artist_name:
        return []
    events = fetch_all_events()
    if not events:
        return []

    target = _normalize(artist_name)
    now = datetime.utcnow()
    cutoff = now.timestamp() - HISTORY_LOOKBACK_DAYS * 86400

    matches = []
    for ev in events:
        ev_artist = _event_artist(ev)
        if not ev_artist:
            continue
        if _normalize(ev_artist) != target:
            continue
        edate = _event_date(ev)
        # Past shows only — we want a track record, not other future shows
        if edate is None or edate > now:
            continue
        if edate.timestamp() < cutoff:
            continue
        matches.append((edate, ev))

    matches.sort(key=lambda x: x[0], reverse=True)
    return [ev for _, ev in matches[:max_shows]]


def aggregate_history(past_events, fetch_sold=True):
    """
    Roll up sold counts / avg prices across the matched past events.
    Pulls sold-data for the most recent N events with sh_ids.
    Returns dict of summary stats or None if past_events is empty.
    """
    if not past_events:
        return None

    venues = []
    last_event = past_events[0]
    last_date = _event_date(last_event)

    total_sold = 0
    sold_prices = []
    margin_pcts = []
    shows_with_sold_data = 0
    enriched = 0
    SOLD_FETCH_LIMIT = 5  # cap calls per artist to keep within Flare quota

    for ev in past_events:
        v = ev.get("event_location_name") or ev.get("venue_name") or ev.get("venue")
        if v and v not in venues:
            venues.append(v)

        if not fetch_sold or enriched >= SOLD_FETCH_LIMIT:
            continue
        sh_id = ev.get("sh_id")
        if not sh_id:
            continue
        enriched += 1
        try:
            sold = fetch_sold_data(sh_id)
        except Exception:
            sold = []
        if not sold:
            continue

        prices = [float(s["price"]) for s in sold if s.get("price") and float(s["price"]) > 0]
        if not prices:
            continue
        shows_with_sold_data += 1
        total_sold += len(prices)
        sold_prices.extend(prices)

        face = ev.get("min_price")
        try:
            face = float(face) if face else None
        except Exception:
            face = None
        if face and face > 0:
            avg = sum(prices) / len(prices)
            margin_pcts.append((avg - face) / face * 100)

    avg_price = round(sum(sold_prices) / len(sold_prices), 2) if sold_prices else None
    avg_margin = round(sum(margin_pcts) / len(margin_pcts)) if margin_pcts else None

    return {
        "past_show_count": len(past_events),
        "shows_with_sold_data": shows_with_sold_data,
        "last_show_date": last_date.strftime("%Y-%m-%d") if last_date else None,
        "last_show_venue": last_event.get("event_location_name")
            or last_event.get("venue_name")
            or last_event.get("venue"),
        "venues": venues[:5],
        "total_sold": total_sold,
        "avg_resale_price": avg_price,
        "avg_resale_margin_pct": avg_margin,
    }


def enrich_event_with_history(event, fetch_sold=True):
    """
    Mutates event to add a 'gct_history' key. Safe to call multiple
    times; no-op if history already populated.
    """
    if "gct_history" in event:
        return event
    artist = extract_artist(event.get("name", ""))
    if not artist:
        return event
    past = find_history(artist)
    summary = aggregate_history(past, fetch_sold=fetch_sold)
    if summary:
        summary["artist_query"] = artist
        event["gct_history"] = summary
    else:
        event["gct_history"] = {"artist_query": artist, "past_show_count": 0}
    return event


# ───────────────────────── Email-rendering helpers ─────────────────────────

def history_html(event):
    """
    Return an inline HTML block for the alert email row.
    Empty string when no history is available.
    """
    h = event.get("gct_history") or {}
    count = h.get("past_show_count") or 0
    if count == 0:
        return (
            '<div style="margin-top:6px;font-size:11px;color:#999;">'
            'GCT history: <span style="color:#aaa;">none tracked</span>'
            '</div>'
        )

    bits = [f'<strong style="color:#0d1b3e;">{count} past show{"s" if count != 1 else ""}</strong>']
    if h.get("last_show_date"):
        venue = h.get("last_show_venue") or ""
        suffix = f" at {venue}" if venue else ""
        bits.append(f'last on {h["last_show_date"]}{suffix}')
    if h.get("total_sold"):
        bits.append(f'{h["total_sold"]:,} tix sold')
    if h.get("avg_resale_price"):
        bits.append(f'avg ${h["avg_resale_price"]:.0f}')
    margin = h.get("avg_resale_margin_pct")
    if margin is not None:
        sign = "+" if margin >= 0 else ""
        color = "#16a34a" if margin >= 25 else ("#f59e0b" if margin >= 0 else "#dc2626")
        bits.append(
            f'<span style="color:{color};font-weight:700;">ROI {sign}{margin}%</span>'
        )

    return (
        '<div style="margin-top:6px;font-size:11px;color:#666;">'
        + 'GCT history: ' + ' &middot; '.join(bits)
        + '</div>'
    )
