"""
Ticketmaster Discovery API event parser.

Pulls upcoming events for one or more TM venue IDs. Use this to watch
any Ticketmaster-only venue (Salt Shed, Aragon, Riv, House of Blues, etc.)
without scraping.

Config:
  {
    "name": "Salt Shed",
    "parser": "ticketmaster",
    "venue_ids": ["KovZ917AI5F", "KovZ917Amf0"],  # one or many
    "size": 100,                                   # optional, default 100
  }

To find a venue ID, hit:
  https://app.ticketmaster.com/discovery/v2/venues.json?keyword=<name>&apikey=<key>

API key: env TM_API_KEY, falls back to the GCT analyzer key.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from ._common import http_get_with_retry

TM_BASE = "https://app.ticketmaster.com/discovery/v2/events.json"
TM_KEY_DEFAULT = "8enbDYyAAHgDgrU0jhoAEUerqttcgAZF"


def _api_key():
    return os.environ.get("TM_API_KEY") or TM_KEY_DEFAULT


def _format_date(dates):
    """TM returns dates as nested {start: {localDate, localTime}}."""
    start = (dates or {}).get("start") or {}
    local_date = start.get("localDate")
    local_time = start.get("localTime")
    if not local_date:
        return ""
    try:
        if local_time:
            dt = datetime.strptime(f"{local_date} {local_time}", "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%a, %b %d, %Y · %-I:%M %p").replace(" 0", " ")
        dt = datetime.strptime(local_date, "%Y-%m-%d")
        return dt.strftime("%a, %b %d, %Y").replace(" 0", " ")
    except Exception:
        return local_date


def _venue_label(event):
    venues = (event.get("_embedded") or {}).get("venues") or []
    if not venues:
        return ""
    v = venues[0]
    name = v.get("name", "")
    city = (v.get("city") or {}).get("name", "")
    if name and city and city.lower() not in name.lower():
        return f"{name}, {city}"
    return name or city


def _fetch_page(venue_ids, page, size):
    params = {
        "apikey": _api_key(),
        "venueId": ",".join(venue_ids),
        "size": size,
        "page": page,
        "sort": "date,asc",
    }
    url = f"{TM_BASE}?{urllib.parse.urlencode(params)}"
    body = http_get_with_retry(
        url,
        headers={"User-Agent": "TicketWatcher/1.0"},
        timeout=30,
        retries=1,
    )
    return json.loads(body.decode("utf-8"))


def _extract_sales(ev):
    """Pull a normalized {public_start, public_end, presales} block."""
    sales = ev.get("sales") or {}
    public = sales.get("public") or {}
    presales_in = sales.get("presales") or []
    presales_out = []
    for p in presales_in:
        presales_out.append({
            "name": (p.get("name") or "Presale").strip(),
            "start": p.get("startDateTime"),
            "end": p.get("endDateTime"),
        })
    return {
        "public_start": public.get("startDateTime"),
        "public_end": public.get("endDateTime"),
        "presales": presales_out,
    }


def _extract_price_range(ev):
    """
    TM's priceRanges is an array (often two entries: "standard" and "official platinum").
    Take the standard min/max if available; fall back to whatever is first.
    Returns {min, max, currency} or None.
    """
    ranges = ev.get("priceRanges") or []
    if not ranges:
        return None
    # Prefer the "standard" tier when TM splits it out
    standard = next((r for r in ranges if (r.get("type") or "").lower() == "standard"), None)
    chosen = standard or ranges[0]
    try:
        lo = float(chosen.get("min")) if chosen.get("min") is not None else None
        hi = float(chosen.get("max")) if chosen.get("max") is not None else None
    except (TypeError, ValueError):
        return None
    if lo is None and hi is None:
        return None
    return {
        "min": lo,
        "max": hi,
        "currency": chosen.get("currency") or "USD",
        "type": chosen.get("type") or "standard",
    }


def parse(site):
    raw_ids = site.get("venue_ids") or ([site["venue_id"]] if site.get("venue_id") else [])
    if not raw_ids:
        raise ValueError("ticketmaster parser needs venue_ids or venue_id")

    size = site.get("size", 100)
    events = []
    seen_ids = set()

    # Walk pages until empty or capped at 5 pages (500 events)
    for page in range(5):
        try:
            data = _fetch_page(raw_ids, page, size)
        except urllib.error.HTTPError as e:
            # 429 = rate limited; 401 = bad key; bail with whatever we have
            print(f"[ticketmaster] HTTP {e.code} on page {page} — stopping")
            break
        except Exception as e:
            print(f"[ticketmaster] page {page} fetch failed: {e}")
            break

        page_events = (data.get("_embedded") or {}).get("events") or []
        if not page_events:
            break

        for ev in page_events:
            eid = ev.get("id")
            if not eid or eid in seen_ids:
                continue
            seen_ids.add(eid)

            name = (ev.get("name") or "").strip()
            url = ev.get("url") or ""
            date = _format_date(ev.get("dates"))
            location = _venue_label(ev)

            events.append({
                "slug": eid,
                "name": name or f"Event {eid}",
                "location": location,
                "date": date,
                "url": url,
                "sales": _extract_sales(ev),
                "price_range": _extract_price_range(ev),
            })

        page_info = data.get("page") or {}
        total_pages = page_info.get("totalPages", 1)
        if page + 1 >= total_pages:
            break

    return events
