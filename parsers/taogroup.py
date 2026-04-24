"""
Tao Group (taogroup.com) event parser.

Uses Tao's public WordPress REST API filtered to a specific venue term ID.

Config:
  {
    "name": "Tao Nightclub Chicago",
    "parser": "taogroup",
    "venue_id": 131,   # WordPress term ID for the venue
    "per_page": 100,   # optional, default 100
  }

To find a venue_id, hit:
  https://taogroup.com/wp-json/wp/v2/event_venue?per_page=100
"""

import html as html_lib
import json
import urllib.request


def parse(site):
    venue_id = site["venue_id"]
    per_page = site.get("per_page", 100)
    url = f"https://taogroup.com/wp-json/wp/v2/events?event_venue={venue_id}&per_page={per_page}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", errors="ignore"))

    events = []
    for ev in data:
        eid = ev.get("id")
        if not eid:
            continue
        title = html_lib.unescape((ev.get("title") or {}).get("rendered") or "").strip()
        link = ev.get("link") or ""
        # acf.event_title.display_title is often cleaner than the URL-style title
        acf = ev.get("acf") or {}
        event_title = ((acf.get("event_title") or {}).get("display_title") or "").strip()
        if event_title:
            display = html_lib.unescape(event_title)
        else:
            display = title

        # Try to pull a date — title often has "M/D/YYYY" prefix
        date = ""
        # Most Tao titles start with "M/D/YYYY – "
        parts = title.split("–", 1) if "–" in title else title.split("-", 1)
        if parts and "/" in parts[0]:
            date = parts[0].strip()

        events.append({
            "slug": str(eid),
            "name": display or title or f"Event {eid}",
            "location": site.get("name", ""),
            "date": date,
            "url": link,
        })

    # De-dupe
    seen = set()
    unique = []
    for e in events:
        if e["slug"] in seen:
            continue
        seen.add(e["slug"])
        unique.append(e)
    return unique
