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
DEBUG_DUMP_PATH = STATE_DIR / "stubhub-debug.json"
LISTING_TTL_SECONDS = 6 * 3600  # 6h — listings shift but slowly

# Per-process flag — we only dump the first ScraperAPI response of a run
# so we can see what's coming back without bloating git on every event.
_debug_dumped = False


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


def _fetch_via_scraperapi(target_url, timeout=90):
    """Fetch a URL through ScraperAPI premium. Returns HTML string or None.

    Always dumps the FIRST fetch attempt of each run (success or failure)
    to state/stubhub-debug.json so we can see what's happening on CI:
    HTTPError code, exception type, or full HTML if it succeeded. Without
    this, the run is opaque since 104/104 entries are null and the cache
    only records 'null' not 'why null'.
    """
    global _debug_dumped
    dump_payload = {"url": target_url, "fetched_at": time.time()}
    html = None
    try:
        req = urllib.request.Request(_sp_request(target_url), headers={"User-Agent": "TicketWatcher/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="ignore")
            dump_payload["status"] = r.status
    except urllib.error.HTTPError as e:
        print(f"[stubhub] HTTP {e.code} for {target_url[:80]}")
        # Read the error body — ScraperAPI often returns useful info in the
        # body even on a 4xx (e.g. "Invalid API key", "Quota exceeded").
        try:
            err_body = e.read().decode("utf-8", errors="ignore")[:2000]
        except Exception:
            err_body = ""
        dump_payload["status"] = e.code
        dump_payload["error_body"] = err_body
    except Exception as e:
        print(f"[stubhub] fetch failed: {e}")
        dump_payload["status"] = "exception"
        dump_payload["error_type"] = type(e).__name__
        dump_payload["error_msg"] = str(e)[:500]

    if html is not None:
        dump_payload["html_length"] = len(html)
        dump_payload["html_sample_head"] = html[:2000]
        dump_payload["html_sample_tail"] = html[-1000:] if len(html) > 1000 else ""
        dump_payload["indicators"] = {
            "ld_json_blocks": len(re.findall(r'application/ld\+json', html, flags=re.IGNORECASE)),
            "event_urls": re.findall(r'https://www\.stubhub\.com/[^"\s<>]+/event/\d+/?', html)[:5],
            "captcha_hits": len(re.findall(r'captcha|datadome|cloudflare|access denied|blocked', html, flags=re.IGNORECASE)),
            "has_tickets_word": "tickets" in html.lower(),
            "has_dollar_signs": html.count("$"),
            "title_tag": (re.search(r'<title[^>]*>([^<]+)</title>', html) or [None, ""])[1][:120] if re.search(r'<title[^>]*>([^<]+)</title>', html) else "",
        }

    # Dump the first attempt regardless of success/failure
    if not _debug_dumped:
        _debug_dumped = True
        try:
            _write_json(DEBUG_DUMP_PATH, dump_payload)
            print(f"[stubhub] DEBUG: dumped first attempt to {DEBUG_DUMP_PATH.name} status={dump_payload.get('status')}")
        except Exception as e:
            print(f"[stubhub] debug-dump failed: {e}")

    return html


def _find_first_event_url(html):
    """
    StubHub search HTML contains links like:
      https://www.stubhub.com/<event-slug>-tickets/event/<id>/
    Find the first one (most relevant by SH's own ranking). Returns full URL or None.
    """
    m = re.search(
        r'https://www\.stubhub\.com/[a-z0-9\-]+-tickets/event/\d+/?',
        html, flags=re.IGNORECASE,
    )
    return m.group(0) if m else None


def _regex_price_from_html(html):
    """
    Last-resort: pull a min price out of plain HTML where structured data
    isn't available. StubHub renders prices like "from $45", "$45+", or
    "Tickets from $45.00". Returns float or None.
    """
    for pattern in (
        r'from\s*\$\s*(\d+(?:\.\d+)?)',
        r'tickets\s*from\s*\$\s*(\d+(?:\.\d+)?)',
        r'\$\s*(\d+(?:\.\d+)?)\s*\+',
        r'"lowPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        r'"minPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?',
    ):
        m = re.search(pattern, html, flags=re.IGNORECASE)
        if m:
            try:
                price = float(m.group(1))
                if 5 <= price <= 50000:  # sanity-bound
                    return price
            except ValueError:
                continue
    return None


def fetch_stubhub_listing(artist, venue, year=None):
    """
    Multi-strategy StubHub asking-price lookup:
      1. Hit /find/s/?q=<artist venue>, parse JSON-LD Event records (best)
      2. If no JSON-LD events, find first event URL in search HTML and
         fetch THAT page — event detail pages still ship JSON-LD when
         search results no longer do
      3. Fall back to regex price scrape on whichever page has data

    Costs 1 SP credit on success path 1, 2 on path 2. Caches result for 6h
    regardless of which path succeeded (or all failed).
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

    # ── Strategy 1: search page → JSON-LD events ──
    search_url = f"{SH_BASE}/find/s/?q={urllib.parse.quote(query)}"
    search_html = _fetch_via_scraperapi(search_url)

    if not search_html:
        cache[cache_key] = {"fetched_at": time.time(), "result": None}
        _write_json(LISTING_CACHE_PATH, cache)
        return None

    candidates = _extract_event_offers(search_html)
    ld_blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>',
        search_html, flags=re.IGNORECASE,
    )
    print(
        f"[stubhub] q={query!r} search={len(search_html)}b "
        f"ld_blocks={len(ld_blocks)} candidates={len(candidates)}"
    )

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

    # ── Strategy 2: follow first event link, parse JSON-LD on event page ──
    if not result:
        event_url = _find_first_event_url(search_html)
        if event_url:
            print(f"[stubhub] following event link: {event_url[:80]}")
            event_html = _fetch_via_scraperapi(event_url)
            if event_html:
                event_candidates = _extract_event_offers(event_html)
                if event_candidates:
                    ec = event_candidates[0]
                    if ec.get("min_price"):
                        result = {
                            "min_price": ec["min_price"],
                            "url": ec.get("url") or event_url,
                            "location": ec.get("location") or "",
                            "start_date": ec.get("start_date") or "",
                            "source": "stubhub_event_page",
                        }
                # Strategy 3: regex price from event page
                if not result:
                    price = _regex_price_from_html(event_html)
                    if price:
                        result = {
                            "min_price": price,
                            "url": event_url,
                            "location": "",
                            "start_date": "",
                            "source": "stubhub_event_page_regex",
                        }
                        print(f"[stubhub] regex price hit: ${price}")

    # ── Strategy 3b: regex price from search HTML as absolute last resort ──
    if not result:
        price = _regex_price_from_html(search_html)
        if price:
            result = {
                "min_price": price,
                "url": "",
                "location": "",
                "start_date": "",
                "source": "stubhub_search_regex",
            }
            print(f"[stubhub] search-page regex price hit: ${price}")

    cache[cache_key] = {"fetched_at": time.time(), "result": result}
    _write_json(LISTING_CACHE_PATH, cache)
    return result


def enrich_event_with_listing(event):
    """
    Scrape StubHub for the current cheapest ask. Always runs (~10 SP
    credits per new event); shown alongside Flare sold-data so the email
    has both "what's selling" and "what's currently asked."
    """
    if "current_listing" in event:
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
