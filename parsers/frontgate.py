"""FrontGate Tickets parser — Webflow CMS with event-item-wrap blocks."""

import re
import urllib.request

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
BASE = "https://www.frontgatetickets.com"


def parse(site):
    """site is a dict like {name, url, parser}; returns list of event dicts."""
    req = urllib.request.Request(site["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="ignore")

    events = []
    for block in re.split(r'class="event-item-wrap', html)[1:]:
        block = block[:8000]
        slug_m = re.search(r'href="/events/([a-z0-9-]+)"', block)
        if not slug_m:
            continue
        slug = slug_m.group(1)
        name_m = re.search(
            r'<h3[^>]*class="heading-style-h6 is-main"[^>]*>([^<]+)</h3>', block
        )
        name = name_m.group(1).strip() if name_m else slug.replace("-", " ").title()
        loc_m = re.search(r'fs-cmsfilter-field="location"[^>]*>([^<]+)<', block)
        location = loc_m.group(1).strip() if loc_m else ""
        date_m = re.search(r'fs-cmssort-field="festival-date"[^>]*>([^<]+)<', block)
        date = date_m.group(1).strip() if date_m else ""
        events.append({
            "slug": slug,
            "name": name,
            "location": location,
            "date": date,
            "url": f"{BASE}/events/{slug}",
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
