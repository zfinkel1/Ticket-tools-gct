#!/usr/bin/env python3
"""
Flare (TicketFlipping) enrichment for new event alerts.

GCT-history lookup: reads state/gct-history-db.json — Gold Coast's own
3-year sales database, extracted from flare_analyzer.py. This is the
authoritative history source. Flare's API doesn't expose past events
(favorites/all-events both contain only upcoming).

Current-sold lookup (separate concern): for the new event itself, hit
Flare's get-sold-data endpoint to surface what's already changed hands.

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
HISTORY_DB_PATH = STATE_DIR / "gct-history-db.json"
VENUE_HISTORY_DB_PATH = STATE_DIR / "gct-venue-history-db.json"
ARTIST_VENUES_DB_PATH = STATE_DIR / "gct-artist-venues-db.json"

EVENTS_TTL_SECONDS = 6 * 3600        # All-events: refresh every 6h (Flare allows 4/day, this exactly fits)
SOLD_TTL_SECONDS = 7 * 24 * 3600     # Sold data per event: 1 week


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


# ─────────────── GCT history database (from flare_analyzer.py) ───────────────

_history_db_cache = None


def _load_history_db():
    global _history_db_cache
    if _history_db_cache is not None:
        return _history_db_cache
    db = _read_json(HISTORY_DB_PATH)
    if db is None:
        print(f"[gct-history] DB not found at {HISTORY_DB_PATH} — run tools/extract_history_db.py")
        _history_db_cache = {}
    else:
        _history_db_cache = db
    return _history_db_cache


def _history_normalize(s):
    """
    Normalize event name to history-DB key form. Mirrors the algorithm
    in flare_analyzer.py:lookup_artist_history so matches stay consistent
    with the analyzer's UI.
    """
    if not s:
        return ""
    s = s.lower()
    for sep in [" - ", ": ", " – "]:
        if sep in s:
            s = s.split(sep)[0].strip()
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"[^\w\s]", "", s).strip()
    return s


def lookup_gct_history(event_name):
    """
    Returns history dict for the event, or None.

    Algorithm (stricter than flare_analyzer.py's substring match — that one
    produces too many false positives in this automated context, e.g.
    "Cold, University Drive" matching DB key 'ive'):

      1. Exact normalized match wins
      2. Word-level prefix match: db_key's words must align as the
         leading words of the event's normalized name (catches "Olivia
         Rodrigo - The Unraveled Tour" -> "olivia rodrigo")
      3. Skip multi-artist concert keys (start with quote, contain comma)
         since their aggregated stats don't represent the headlining
         artist alone.

    Audited against the full corpus of tracked events: 134/777 hits with
    no obvious false positives, vs 245/777 with the old substring match
    (mostly bogus 'ive', 'iu', 'live' fragment matches).
    """
    db = _load_history_db()
    if not db:
        return None
    key = _history_normalize(event_name)
    if not key:
        return None
    if key in db:
        return db[key]

    key_words = key.split()
    if not key_words:
        return None
    best = None
    best_len = 0
    for db_key, data in db.items():
        if not db_key:
            continue
        if db_key.startswith('"') or "," in db_key:
            continue
        db_words = db_key.split()
        if not db_words or len(db_words) > len(key_words):
            continue
        if key_words[: len(db_words)] != db_words:
            continue
        # Prefer the longest matching prefix — most specific signal
        if len(db_key) > best_len:
            best, best_len = data, len(db_key)
    return best


def enrich_event_with_history(event):
    """
    Mutates event to add 'gct_history' (artist aggregate) and
    'gct_venue_history' (per-artist+venue+section, when available).
    Safe to call multiple times.
    """
    if "gct_history" in event and "gct_venue_history" in event:
        return event
    name = event.get("name", "")
    venue = event.get("location", "")

    # Artist-level aggregate
    if "gct_history" not in event:
        hist = lookup_gct_history(name)
        if hist:
            event["gct_history"] = {
                "events": hist.get("events"),
                "avg_margin": hist.get("avg_margin"),
                "profitable_pct": hist.get("profitable_pct"),
                "avg_sell_price": hist.get("avg_sell_price"),
                "avg_cost_price": hist.get("avg_cost_price"),
                "display_name": hist.get("display_name"),
            }
        else:
            event["gct_history"] = None

    # Per-venue breakdown (much richer signal when GCT has section-level data)
    if "gct_venue_history" not in event:
        venue_hist = lookup_venue_history(name, venue)
        event["gct_venue_history"] = venue_hist

    # Full venue list for this artist (drives the "GCT has bought at X, Y, Z"
    # email line that gives concrete events behind the aggregate margin).
    if "gct_artist_venues" not in event:
        event["gct_artist_venues"] = lookup_artist_venues(name)
    return event


# ─────────────── GCT venue-specific history (from concert_historical.py) ────

_venue_db_cache = None


def _load_venue_history_db():
    global _venue_db_cache
    if _venue_db_cache is not None:
        return _venue_db_cache
    db = _read_json(VENUE_HISTORY_DB_PATH)
    if db is None:
        print(f"[gct-venue-history] DB not found at {VENUE_HISTORY_DB_PATH}")
        _venue_db_cache = {}
    else:
        _venue_db_cache = db
    return _venue_db_cache


def _venue_normalize(s):
    """Normalize a venue or artist string for DB key matching."""
    if not s:
        return ""
    s = s.lower().strip()
    for sep in [" - ", ": ", " – "]:
        if sep in s:
            s = s.split(sep)[0].strip()
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"[^\w\s]", "", s).strip()
    return s


_artist_venues_cache = None


def _load_artist_venues_db():
    global _artist_venues_cache
    if _artist_venues_cache is not None:
        return _artist_venues_cache
    db = _read_json(ARTIST_VENUES_DB_PATH)
    if db is None:
        print(f"[gct-artist-venues] DB not found at {ARTIST_VENUES_DB_PATH}")
        _artist_venues_cache = {}
    else:
        _artist_venues_cache = db
    return _artist_venues_cache


def lookup_artist_venues(artist_raw, max_venues=8):
    """
    Returns a list of {venue, tickets, margin} for every venue this artist
    has been bought at, sorted by tickets sold descending. Joins the
    artist→venues map (_a2v) with the venue-history DB (_av_db).

    Empty list when artist isn't in the GCT history.
    """
    a2v = _load_artist_venues_db()
    av = _load_venue_history_db()
    if not a2v or not av:
        return []
    artist = _venue_normalize(artist_raw)
    if not artist:
        return []
    # _a2v keys are stored as the original lowercase artist name. Try exact first.
    venues = a2v.get(artist)
    if not venues:
        # Fall back to substring match (some keys retain punctuation we stripped)
        for k in a2v:
            k_norm = _venue_normalize(k)
            if k_norm == artist:
                venues = a2v[k]
                break
        if not venues:
            return []

    out = []
    for venue_name in venues:
        # Build the artist|||venue key — try exact, then fuzzy match
        sections = av.get(f"{artist}|||{venue_name}")
        if not sections:
            # _av_db keys may use the un-normalized artist name; scan once
            target_v = _venue_normalize(venue_name)
            for k, data in av.items():
                try:
                    k_a, k_v = k.split("|||", 1)
                except ValueError:
                    continue
                if _venue_normalize(k_a) == artist and _venue_normalize(k_v) == target_v:
                    sections = data
                    break
        if not sections:
            continue
        # Roll up the section stats — tickets = sum of counts,
        # margin = weighted-by-count average across sections.
        total_tix = 0
        weighted_margin_num = 0
        for s in sections.values():
            cnt = s.get("count") or 0
            mar = s.get("avg_margin")
            total_tix += cnt
            if mar is not None:
                weighted_margin_num += mar * cnt
        margin = round(weighted_margin_num / total_tix, 1) if total_tix > 0 else None
        out.append({"venue": venue_name, "tickets": total_tix, "margin": margin})

    out.sort(key=lambda v: -(v["tickets"] or 0))
    return out[:max_venues]


def lookup_venue_history(artist_raw, venue_raw):
    """
    Returns the GCT venue-history record for this artist+venue, or None.
    DB keys are "artist|||venue" lowercased. Try exact first; fall back
    to artist exact + venue substring match.

    Each match is a dict of sections -> {avg_sell, avg_cost, buy_under,
    avg_margin, count}. Caller decides which section to surface.
    """
    db = _load_venue_history_db()
    if not db:
        return None
    a = _venue_normalize(artist_raw)
    v = _venue_normalize(venue_raw)
    if not a or not v:
        return None
    key = f"{a}|||{v}"
    if key in db:
        return db[key]
    # Fuzzy venue (venue strings vary: "Allstate Arena, Rosemont" vs "Allstate Arena")
    for k, data in db.items():
        try:
            k_artist, k_venue = k.split("|||", 1)
        except ValueError:
            continue
        if k_artist == a and (v in k_venue or k_venue in v):
            return data
    return None


def _match_flare_event(event):
    """
    Look up the new watcher event in Flare's all-events cache. Match on
    artist + venue + date. Returns the Flare event record (with sh_id,
    min_price etc.) or None.
    """
    artist = extract_artist(event.get("name", ""))
    if not artist:
        return None
    target_artist = _normalize(artist)
    target_venue = _normalize(event.get("location", ""))

    # Date in the watcher event is human-formatted ("Sat, Jun 14 at 8:00 PM CDT").
    # Try to extract YYYY-MM-DD from it; fall back to no date filter.
    date_str = str(event.get("date") or "")
    target_year = None
    m = re.search(r"\b(20\d{2})\b", date_str)
    if m:
        target_year = int(m.group(1))

    events = fetch_all_events()
    if not events:
        return None

    for ev in events:
        ev_artist = _event_artist(ev)
        if not ev_artist or _normalize(ev_artist) != target_artist:
            continue
        # Optional venue match — a heavy lift for fuzzy mismatches, so soft check.
        ev_venue = _normalize(
            ev.get("event_location_name") or ev.get("venue_name") or ""
        )
        if target_venue and ev_venue and target_venue not in ev_venue and ev_venue not in target_venue:
            continue
        # Optional year match if we could parse one
        if target_year:
            ev_date = _event_date(ev)
            if ev_date and ev_date.year != target_year:
                continue
        return ev
    return None


def enrich_event_with_current_sold(event):
    """
    For the new event itself (not the artist's history), look up its
    StubHub sold data and surface what's already changed hands. Adds
    'current_sold' key to event:
      {sh_id, sold_count, min_price, median_price, max_price, avg_price,
       last_sold_date}
    Uses 1 Flare get-sold-data call per matched event (1000/day budget).
    """
    if "current_sold" in event:
        return event

    flare_event = _match_flare_event(event)
    if not flare_event:
        event["current_sold"] = None
        return event

    sh_id = flare_event.get("sh_id") or flare_event.get("SH_id") or flare_event.get("stubhub_id")
    if not sh_id:
        event["current_sold"] = {"flare_event_id": flare_event.get("id"), "sh_id": None}
        return event

    sold = fetch_sold_data(sh_id)
    if not sold:
        event["current_sold"] = {"sh_id": sh_id, "sold_count": 0}
        return event

    prices = []
    last_date = None
    for s in sold:
        try:
            p = float(s.get("price") or 0)
            qty = int(s.get("quantity") or 1)
        except Exception:
            continue
        if p <= 0:
            continue
        # Each row is a sale; weight by quantity for accurate avg
        prices.extend([p] * max(1, qty))
        d = s.get("sold_date")
        if d and (last_date is None or d > last_date):
            last_date = d

    if not prices:
        event["current_sold"] = {"sh_id": sh_id, "sold_count": 0}
        return event

    prices_sorted = sorted(prices)
    n = len(prices_sorted)
    median = prices_sorted[n // 2] if n % 2 else (prices_sorted[n // 2 - 1] + prices_sorted[n // 2]) / 2

    event["current_sold"] = {
        "sh_id": sh_id,
        "sold_count": n,
        "min_price": round(min(prices_sorted), 2),
        "max_price": round(max(prices_sorted), 2),
        "median_price": round(median, 2),
        "avg_price": round(sum(prices_sorted) / n, 2),
        "last_sold_date": last_date,
    }
    return event


# ───────────────────────── Email-rendering helpers ─────────────────────────

def _venue_list_html(artist_venues):
    """Render the artist's GCT venue breakdown as a second line under the aggregate.
    Format: 'Venues: Wrigley Field (1,418 tix, +14%) · Fiserv Forum (78, -0%) · …'
    Empty string when no venues."""
    if not artist_venues:
        return ""
    bits = []
    for v in artist_venues[:5]:
        venue_name = v["venue"]
        tix = v["tickets"]
        margin = v["margin"]
        # Title-case the lowercase venue name for display
        display = ' '.join(w[:1].upper() + w[1:] if w else w for w in venue_name.split(' '))
        if margin is None:
            bits.append(f'{display} ({tix:,} tix)')
        else:
            sign = "+" if margin >= 0 else ""
            color = "#16a34a" if margin >= 25 else ("#f59e0b" if margin >= 0 else "#dc2626")
            bits.append(
                f'<span style="color:#333;">{display}</span> '
                f'<span style="color:#888;">({tix:,} tix,</span> '
                f'<span style="color:{color};font-weight:700;">{sign}{margin}%</span><span style="color:#888;">)</span>'
            )
    return (
        '<div style="margin-top:3px;font-size:11px;color:#888;line-height:1.5;">'
        '<span style="color:#aaa;">Venues: </span>'
        + ' &middot; '.join(bits)
        + '</div>'
    )


def history_html(event):
    """
    Render the GCT sales history for this event. Combines three sources:
      - Per-venue per-section breakdown (highest signal — shows section data)
      - Artist-level aggregate (fallback when no venue match)
      - Per-venue ticket breakdown (always shown when artist has any history)
    Empty pill only when artist has zero GCT history.
    """
    venue_h = event.get("gct_venue_history") or {}
    artist_h = event.get("gct_history")
    venues_list = event.get("gct_artist_venues") or []

    # Venue match — best case, has section-level data
    if venue_h:
        # Roll up section stats into one summary line. Use the most-bought
        # section (highest count) as the headline.
        sections = list(venue_h.items())
        # (name, data) where data has count/avg_margin/avg_sell/etc.
        sections.sort(key=lambda kv: -(kv[1].get("count") or 0))
        top_section, top_data = sections[0]
        total_count = sum((s[1].get("count") or 0) for s in sections)
        margin = top_data.get("avg_margin")
        avg_sell = top_data.get("avg_sell")
        buy_under = top_data.get("buy_under")

        bits = [
            f'<strong style="color:#0d1b3e;">{total_count} buys</strong>',
            f'<span style="color:#888;">at this venue</span>',
        ]
        if margin is not None:
            sign = "+" if margin >= 0 else ""
            color = "#16a34a" if margin >= 50 else ("#f59e0b" if margin >= 0 else "#dc2626")
            bits.append(
                f'<span style="color:{color};font-weight:700;">{top_section} avg {sign}{margin}%</span>'
            )
        if avg_sell:
            bits.append(f'avg sell ${avg_sell:.0f}')
        if buy_under:
            bits.append(f'<span style="color:#888;">buy under ${buy_under:.0f}</span>')

        return (
            '<div style="margin-top:6px;font-size:11px;color:#666;">'
            + 'GCT @ venue: ' + ' &middot; '.join(bits)
            + '</div>'
            + _venue_list_html(venues_list)
        )

    # Artist aggregate (fallback)
    if artist_h:
        events_count = artist_h.get("events") or 0
        margin = artist_h.get("avg_margin")
        profitable = artist_h.get("profitable_pct")
        avg_sell = artist_h.get("avg_sell_price")

        bits = [
            f'<strong style="color:#0d1b3e;">{events_count} event{"s" if events_count != 1 else ""}</strong>'
        ]
        if margin is not None:
            sign = "+" if margin >= 0 else ""
            color = "#16a34a" if margin >= 50 else ("#f59e0b" if margin >= 0 else "#dc2626")
            bits.append(
                f'<span style="color:{color};font-weight:700;">avg {sign}{margin}%</span>'
            )
        if profitable is not None:
            bits.append(f'{profitable:.0f}% profitable')
        if avg_sell:
            bits.append(f'avg sell ${avg_sell:.0f}')

        return (
            '<div style="margin-top:6px;font-size:11px;color:#666;">'
            + 'GCT history: ' + ' &middot; '.join(bits)
            + '</div>'
            + _venue_list_html(venues_list)
        )

    return (
        '<div style="margin-top:6px;font-size:11px;color:#999;">'
        'GCT history: <span style="color:#aaa;">none tracked</span>'
        '</div>'
    )


def current_sold_html(event):
    """
    Render what THIS event has actually sold for so far on StubHub.
    Different from history_html (which is the artist's past shows).
    Empty when there's no SH match or no sold data yet.
    """
    cs = event.get("current_sold")
    if not cs:
        return ""
    if not cs.get("sh_id"):
        return ""
    sold_count = cs.get("sold_count") or 0
    if sold_count == 0:
        return (
            '<div style="margin-top:4px;font-size:11px;color:#999;">'
            'StubHub: <span style="color:#aaa;">no sales yet</span>'
            '</div>'
        )

    avg = cs.get("avg_price")
    median = cs.get("median_price")
    lo = cs.get("min_price")
    hi = cs.get("max_price")
    bits = [f'<strong style="color:#0d1b3e;">{sold_count:,} sold</strong>']
    if median is not None:
        bits.append(f'median <strong>${median:.0f}</strong>')
    if lo is not None and hi is not None and lo != hi:
        bits.append(f'range ${lo:.0f}–${hi:.0f}')
    return (
        '<div style="margin-top:4px;font-size:11px;color:#666;">'
        + 'StubHub now: ' + ' &middot; '.join(bits)
        + '</div>'
    )
