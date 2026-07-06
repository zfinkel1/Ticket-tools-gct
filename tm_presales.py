#!/usr/bin/env python3
"""
Ticketmaster Discovery presale supplement for the GoldCoast Presale Digest.

Why: Flare's /api/all-events presale fields are sparse — on 2026-07-06 the whole
28k-event feed carried only 52 upcoming presales (0 Chicagoland in a 2-week
window). TM is where most real presales live, so the digest merges this in.

What it returns: events shaped exactly like presale_analyzer.fetch_presale_api()
rows, pre-filtered to events with a presale (or public onsale) window opening on
the target day. presale_start times are already CENTRAL (converted from TM's
UTC), so the caller must NOT run central_from_local() on these.

Dedup: TM lists one entry per performance (a 20-show theatre run = 20 rows).
Rows are grouped by (normalized event name, normalized venue) into one event
with the earliest upcoming date and a shows_count. Dedup against Flare's rows
happens in presale_email.py (TM id first, then name+city).

API budget: <= ~35 calls/run (3 segment sweeps + Chicago DMA, 5 pages max each,
0.3s apart) against a 5,000/day quota.
"""

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
CHICAGO_DMA = "249"
# How far past the target day an event's PUBLIC onsale may be. Presales open a
# few days before public onsale, so this window catches every presale opening
# on the target day without sweeping TM's entire future calendar.
ONSALE_WINDOW_DAYS = 21
PAGE_SIZE = 200
MAX_PAGES = 5  # TM deep-paging cap: size * page must stay under 1000


def _tm_key():
    import os
    key = os.environ.get("TM_API_KEY")
    if key:
        return key
    try:
        import presale_analyzer as PA
        return PA.TM_KEY
    except Exception:
        return ""


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _central(s):
    """TM datetimes are UTC ('2026-07-07T15:00:00Z') -> Central datetime."""
    try:
        return (datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
                .replace(tzinfo=timezone.utc).astimezone(CENTRAL))
    except Exception:
        return None


def _get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "GoldCoast/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            code = getattr(e, "code", None)
            if i < tries - 1 and code in (429, 500, 502, 503):
                time.sleep(1.5 * (i + 1))
                continue
            raise
    return None


def _fetch_pages(params, label):
    """Page through one Discovery query. Returns (raw event dicts, truncated)."""
    out = []
    truncated = False
    for page in range(MAX_PAGES):
        qs = urllib.parse.urlencode({**params, "size": PAGE_SIZE, "page": page})
        data = _get(f"https://app.ticketmaster.com/discovery/v2/events.json?{qs}")
        evs = (data.get("_embedded") or {}).get("events", []) if data else []
        out.extend(evs)
        pg = (data or {}).get("page") or {}
        total_pages = pg.get("totalPages", 0)
        if page >= total_pages - 1:
            break
        if page == MAX_PAGES - 1 and total_pages > MAX_PAGES:
            truncated = True
            print(f"[tm-presales] {label}: truncated at {MAX_PAGES * PAGE_SIZE} of ~{pg.get('totalElements')} rows")
        time.sleep(0.3)
    return out, truncated


def _windows_for(ev, target_day):
    """(central_datetime, window_name) for every presale + the public onsale."""
    sales = ev.get("sales") or {}
    out = []
    for p in sales.get("presales") or []:
        dt = _central(p.get("startDateTime"))
        if dt:
            out.append((dt, p.get("name") or "Presale"))
    pub = _central((sales.get("public") or {}).get("startDateTime"))
    if pub:
        out.append((pub, "Public Onsale"))
    return out


def fetch_tm_presales(target_day):
    """All TM events with a presale/onsale window opening on target_day (a date),
    deduped per-performance, shaped like fetch_presale_api() rows."""
    key = _tm_key()
    if not key:
        print("[tm-presales] no TM API key — skipping")
        return []

    base = {"apikey": key, "sort": "onSaleStartDate,asc"}

    raw = []
    # Chicago DMA: one open-ended query (all upcoming onsales — a few hundred
    # rows, no cap risk, and catches presales for far-out onsales too).
    rows, _ = _fetch_pages({**base, "dmaId": CHICAGO_DMA,
                            "onsaleOnAfterStartDate": target_day.strftime("%Y-%m-%d")}, "chicago-dma")
    raw += rows
    # National: one exact-date query per day in the window (onsaleOnStartDate is
    # the only bounded onsale filter Discovery has — "before" params are silently
    # ignored). If a hot onsale day tops the 1000-row paging cap, re-pull it
    # split by segment.
    for offset in range(ONSALE_WINDOW_DAYS + 1):
        day = (target_day + timedelta(days=offset)).strftime("%Y-%m-%d")
        rows, truncated = _fetch_pages({**base, "countryCode": "US", "onsaleOnStartDate": day}, f"US {day}")
        if truncated:
            rows = []
            for seg in ("Music", "Sports", "Arts & Theatre", "Film", "Miscellaneous"):
                r2, _ = _fetch_pages({**base, "countryCode": "US", "classificationName": seg,
                                      "onsaleOnStartDate": day}, f"{seg} {day}")
                rows += r2
        raw += rows

    try:
        import presale_analyzer as PA
    except Exception:
        PA = None

    now_c = datetime.now(CENTRAL)
    groups = {}
    seen_ids = set()
    for ev in raw:
        if ev.get("test"):
            continue
        tm_id = ev.get("id") or ""
        if tm_id in seen_ids:  # chicago-dma rows repeat in the segment sweeps
            continue
        seen_ids.add(tm_id)

        venues = (ev.get("_embedded") or {}).get("venues") or [{}]
        v = venues[0]
        country = ((v.get("country") or {}).get("countryCode") or "US")
        if country not in ("US", ""):
            continue
        windows = _windows_for(ev, target_day)
        if not any(dt.date() == target_day for dt, _ in windows):
            continue

        name = (ev.get("name") or "").strip()
        venue = (v.get("name") or "").strip()
        city = ((v.get("city") or {}).get("name") or "").strip()
        state = ((v.get("state") or {}).get("stateCode") or "").strip()

        start = (ev.get("dates") or {}).get("start") or {}
        local_date = start.get("localDate") or ""
        local_time = start.get("localTime") or "19:00:00"
        event_date = f"{local_date}T{local_time}" if local_date else ""

        cls = (ev.get("classifications") or [{}])[0]
        segment = ((cls.get("segment") or {}).get("name") or "").strip()

        g = groups.setdefault((_norm(name), _norm(venue)), {
            "name": name, "venue": venue, "city": city, "state": state,
            "url": ev.get("url") or "", "tm_event_id": tm_id,
            "segment": segment, "event_dates": [], "windows": set(),
            "presale_names": [], "shows_count": 0,
        })
        g["shows_count"] += 1
        if event_date:
            g["event_dates"].append(event_date)
        for dt, wname in windows:
            g["windows"].add(dt.strftime("%Y-%m-%d %H:%M:%S"))
            if wname not in g["presale_names"]:
                g["presale_names"].append(wname)

    events = []
    for g in groups.values():
        # earliest performance that hasn't happened yet (fall back to earliest)
        dates = sorted(g["event_dates"])
        future = [d for d in dates if d[:10] >= now_c.strftime("%Y-%m-%d")]
        event_date = (future or dates or [""])[0]
        days_out = None
        if event_date:
            try:
                days_out = max(0, (datetime.strptime(event_date[:10], "%Y-%m-%d")
                                   - datetime.now()).days)
            except Exception:
                pass

        presales = g["presale_names"]
        pl = [p.lower() for p in presales]
        capacity = None
        category = (g["segment"] or "other").lower()
        if PA is not None:
            try:
                capacity = PA.get_cap(g["venue"], g["city"])
                category = PA.categorize_event(g["name"], g["segment"])
            except Exception:
                pass

        events.append({
            "name": g["name"], "shows_count": g["shows_count"],
            "url": g["url"], "venue": g["venue"],
            "city": g["city"], "state": g["state"],
            "location": f"{g['city']}, {g['state']}",
            "event_date": event_date, "days_out": days_out,
            "segment": g["segment"], "category": category,
            "presales": presales,
            "presale_start": "|".join(sorted(g["windows"])),
            "presale_code": "",
            "capacity": capacity,
            "presale_count": len([p for p in presales if p != "Public Onsale"]),
            "has_spotify": any("spotify" in p for p in pl),
            "has_platinum": any("platinum" in p for p in pl),
            "has_ln": any("live nation" in p for p in pl),
            "has_artist": any("artist" in p for p in pl),
            "has_vip": any("vip" in p for p in pl),
            "has_citi": any("citi" in p for p in pl),
            "has_amex": any("amex" in p or "american express" in p for p in pl),
            "has_chase": any("chase" in p for p in pl),
            "min_price": None, "max_price": None,
            "sp_followers": None, "sp_pop": None,
            "city_streams": None, "youtube_views": None,
            "flare_event_id": None,
            "tm_event_id": g["tm_event_id"],
            "tour_dates": g["shows_count"], "dates_in_city": 1,
            "score": 0, "rec": "pending",
            "src": "tm",
        })
    print(f"[tm-presales] {len(raw)} TM rows -> {len(events)} deduped events with a window on {target_day}")
    return events
