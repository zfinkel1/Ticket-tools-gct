"""
Tixr parser via ScraperAPI (bypasses DataDome).

Config:
  {"name": "...", "parser": "tixr", "city": "chicago", "page_size": 50}

Uses Tixr's public events API:
  https://www.tixr.com/api/events?city=chicago&page=1&pageSize=50

The API returns a JSON array of event objects. Each:
  {id, name, startDate (ms), venue: {name, shortName}, url, ...}
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def parse(site):
    api_key = os.environ.get("SCRAPERAPI_KEY")
    if not api_key:
        raise RuntimeError("SCRAPERAPI_KEY not set — required for Tixr parser")

    city = site.get("city", "chicago")
    page_size = site.get("page_size", 50)
    target = f"https://www.tixr.com/api/events?city={urllib.parse.quote(city)}&page=1&pageSize={page_size}"

    params = urllib.parse.urlencode({
        "api_key": api_key,
        "url": target,
        "premium": "true",
    })
    sp_url = f"https://api.scraperapi.com?{params}"

    req = urllib.request.Request(sp_url)
    with urllib.request.urlopen(req, timeout=90) as r:
        body = r.read().decode("utf-8", errors="ignore")

    data = json.loads(body)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Tixr response: {type(data).__name__}")

    events = []
    for ev in data:
        event_id = ev.get("id")
        name = (ev.get("name") or "").strip()
        url = ev.get("url") or ""
        venue = (ev.get("venue") or {}).get("name") or ""
        ts = ev.get("startDate")  # ms since epoch (UTC)
        date = ""
        if ts:
            try:
                venue_tz = (ev.get("venue") or {}).get("timezone") or "America/Chicago"
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(ZoneInfo(venue_tz))
                date = dt.strftime("%a, %b %d at %I:%M %p %Z").replace(" 0", " ")
            except Exception:
                pass

        if not event_id or not name:
            continue

        # Tixr ships salesStartDate (ms epoch) and sometimes salesEndDate.
        # Normalize to ISO so enrich.sales can render without parser-specific knowledge.
        sales_start = ev.get("salesStartDate")
        sales_end = ev.get("salesEndDate")
        sales_block = {"public_start": None, "public_end": None, "presales": []}
        if sales_start:
            try:
                sales_block["public_start"] = datetime.fromtimestamp(
                    sales_start / 1000, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass
        if sales_end:
            try:
                sales_block["public_end"] = datetime.fromtimestamp(
                    sales_end / 1000, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass

        events.append({
            "slug": str(event_id),
            "name": name,
            "location": venue,
            "date": date,
            "url": url,
            "sales": sales_block,
        })
    return events
