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
from datetime import datetime


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
        ts = ev.get("startDate")  # ms since epoch
        date = ""
        if ts:
            try:
                dt = datetime.fromtimestamp(ts / 1000)
                date = dt.strftime("%a, %b %d at %I:%M %p")
            except Exception:
                pass

        if not event_id or not name:
            continue

        events.append({
            "slug": str(event_id),  # Tixr IDs are stable
            "name": name,
            "location": venue,
            "date": date,
            "url": url,
        })
    return events
