#!/usr/bin/env python3
"""
StubHub direct scraping via ScraperAPI.

Used as a FALLBACK when Flare's all-events cache doesn't have a StubHub
mapping for the new event yet — typical for events announced in the
last 24-48h. ScraperAPI premium=true is required because StubHub is
DataDome-protected (10 credits per call on the Hobby plan).

Strategy: hit StubHub's search page with "{artist} {venue}", parse
JSON-LD blocks (which SH ships for SEO/Google) to extract Event records
with offers.lowPrice. Match by venue + year, return the cheapest ask.

Cache TTL is 6 hours — listing prices change but not by the minute.

Public API:
    enrich_event_with_listing(event)  # adds 'current_listing' to event
    current_listing_html(event)       # renders for the alert email
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .spotify import extract_artist
from .flare import _normalize  # reuse the same fuzzy-matcher

SH_BASE = "https://www.stubhub.com"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
LISTING_CACHE_PATH = STATE_DIR / "stubhub-listing-cache.json"
LISTING_TTL_SECONDS = 6 * 3600  # 6h — listings shift but slowly


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


def _sp_request(target_url):
    """Wrap target URL with ScraperAPI premium params. Returns the SP url."""
    api_key = os.environ.get("SCRAPERAPI_KEY")
    if not api_key:
        raise RuntimeError("SCRAPERAPI_KEY not set — required for StubHub scraping")
    params = urllib.parse.urlencode({
        "api_key": api_key,
        "url": target_url,
        # premium=true uses 10 credits but bypasses DataDome
        "premium": "true",
    })
    return f"https://api.scraperapi.com?{params}"


def _extract_event_offers(html):
    """
    Parse JSON-LD blocks out of the StubHub search HTML.
    Returns a list of {name, url, min_price, location, start_date}.
    """
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    out = []
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("@type")
            t_str = t if isinstance(t, str) else (t[0] if isinstance(t, list) and t else "")
            if "Event" not in t_str:
                continue
            offers = item.get("offers")
            low_price = None
            if isinstance(offers, dict):
                low_price = offers.get("lowPrice") or offers.get("price")
            elif isinstance(offers, list) and offers:
                first = offers[0] if isinstance(offers[0], dict) else {}
                low_price = first.get("lowPrice") or first.get("price")
            try:
                low_price = float(low_price) if low_price not in (None, "") else None
            except Exception:
                low_price = None

            location = item.get("location") or {}
            loc_name = location.get("name") if isinstance(location, dict) else ""

            out.append({
                "name": item.get("name") or "",
                "url": item.get("url") or "",
                "min_price": low_price,
                "location": loc_name or "",
                "start_date": item.get("startDate") or "",
            })
    return out


def _best_match(candidates, target_venue, target_year):
    """Pick the candidate that best matches venue + year, prefer one with a price."""
    if not candidates:
        return None
    target_venue_n = _normalize(target_venue) if target_venue else ""

    def score(c):
        s = 0
        if c.get("min_price"):
            s += 10
        cand_venue = _normalize(c.get("location") or "")
        if target_venue_n and cand_venue and (target_venue_n in cand_venue or cand_venue in target_venue_n):
            s += 5
        if target_year and target_year in (c.get("start_date") or ""):
            s += 3
        return s

    ranked = sorted(candidates, key=score, reverse=True)
    return ranked[0]


def fetch_stubhub_listing(artist, venue, year=None):
    """
    Search StubHub for "{artist} {venue}" and return the best-match
    listing with cheapest ask. Caches results for 6h.

    Returns dict:
        {min_price, url, location, start_date, source: "stubhub_search", cached: bool}
    or None if nothing useful found.
    """
    if not artist:
        return None

    query = f"{artist} {venue}".strip() if venue else artist
    cache_key = _normalize(query)

    cache = _read_json(LISTING_CACHE_PATH) or {}
    entry = cache.get(cache_key)
    if entry and time.time() - entry.get("fetched_at", 0) < LISTING_TTL_SECONDS:
        result = entry.get("result")
        if result is None:
            return None
        return {**result, "cached": True}

    target = f"{SH_BASE}/find/s/?q={urllib.parse.quote(query)}"
    try:
        req = urllib.request.Request(_sp_request(target), headers={"User-Agent": "TicketWatcher/1.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        print(f"[stubhub] HTTP {e.code} for query '{query}'")
        cache[cache_key] = {"fetched_at": time.time(), "result": None}
        _write_json(LISTING_CACHE_PATH, cache)
        return None
    except Exception as e:
        print(f"[stubhub] fetch failed for '{query}': {e}")
        return None

    candidates = _extract_event_offers(html)
    best = _best_match(candidates, venue, year)
    result = None
    if best and best.get("min_price"):
        result = {
            "min_price": best["min_price"],
            "url": best.get("url") or "",
            "location": best.get("location") or "",
            "start_date": best.get("start_date") or "",
            "source": "stubhub_search",
        }

    cache[cache_key] = {"fetched_at": time.time(), "result": result}
    _write_json(LISTING_CACHE_PATH, cache)
    return result


def enrich_event_with_listing(event):
    """
    For events where Flare doesn't yet have sold data (no sh_id match
    or sold_count=0), scrape StubHub for the current cheapest ask.
    Adds 'current_listing' key. Skips entirely if Flare sold data
    already exists for this event (saves ScraperAPI credits).
    """
    if "current_listing" in event:
        return event

    # If Flare already gave us a sold-count > 0, skip scraping — Flare data is
    # better (real transactions) and free. Only scrape when no signal exists.
    cs = event.get("current_sold")
    if cs and cs.get("sold_count"):
        event["current_listing"] = None
        return event

    artist = extract_artist(event.get("name", ""))
    if not artist:
        event["current_listing"] = None
        return event

    venue = event.get("location", "")
    date_str = str(event.get("date") or "")
    year_match = re.search(r"\b(20\d{2})\b", date_str)
    year = year_match.group(1) if year_match else None

    try:
        result = fetch_stubhub_listing(artist, venue, year)
    except RuntimeError as e:
        # SCRAPERAPI_KEY not set — fail soft
        print(f"[stubhub] {e}")
        event["current_listing"] = None
        return event
    except Exception as e:
        print(f"[stubhub] enrich failed for {event.get('name','?')}: {e}")
        event["current_listing"] = None
        return event

    event["current_listing"] = result
    return event


def current_listing_html(event):
    """Render scraped listing data for the alert email row."""
    cl = event.get("current_listing")
    if not cl or not cl.get("min_price"):
        return ""
    price = cl["min_price"]
    cached_tag = ' <span style="color:#bbb;">(cached)</span>' if cl.get("cached") else ""
    url = cl.get("url") or ""
    link_open = f'<a href="{url}" style="color:#0d1b3e;text-decoration:none;border-bottom:1px dotted #0d1b3e;">' if url else ""
    link_close = "</a>" if url else ""
    return (
        '<div style="margin-top:4px;font-size:11px;color:#666;">'
        f'StubHub asking: {link_open}<strong style="color:#0d1b3e;">from ${price:.0f}</strong>{link_close}{cached_tag}'
        '</div>'
    )
