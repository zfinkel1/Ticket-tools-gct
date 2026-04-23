"""
RHP (Rockhouse Partners) venue CMS parser.

Powers many indie music venue sites (Metro Chicago, many House of Blues
locations, etc.). Event URLs follow /event/[slug]/[venue-slug]/[city-state]/
and event cards have h4.entry-title.summary with an inner <a>.
"""

import re
import urllib.request
from urllib.parse import urlparse

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def parse(site):
    req = urllib.request.Request(site["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="ignore")

    base = _base_url(site["url"])

    # RHP renders each event as a block with a class containing "widget-events-contents"
    # preceded by a date div. Easier strategy: split on h4.entry-title and parse each chunk.
    events = []
    # Split on h4.entry-title blocks (each event has one)
    chunks = re.split(r'<h4[^>]*class="entry-title summary[^"]*"', html)[1:]

    for chunk in chunks:
        chunk = chunk[:6000]

        # Event URL from the first <a href=...> inside the h4
        href_m = re.search(r'<a[^>]+href="([^"]+/event/[^"]+)"', chunk)
        if not href_m:
            continue
        full_url = href_m.group(1)
        slug_m = re.search(r'/event/([a-z0-9-]+)', full_url)
        if not slug_m:
            continue
        slug = slug_m.group(1)

        # Name is the text inside that first <a>
        name_m = re.search(r'<a[^>]+href="[^"]+/event/[^"]+"[^>]*>\s*([^<]+?)\s*</a>', chunk)
        name = name_m.group(1).strip() if name_m else slug.replace("-", " ").title()

        # Venue name — rhpVenueContent block with venueLink
        venue_m = re.search(r'class="venueLink"[^>]*title="([^"]+)"', chunk)
        venue = venue_m.group(1).strip() if venue_m else ""

        events.append({
            "slug": slug,
            "name": name,
            "location": venue,
            "date": _extract_date_before(html, slug),
            "url": full_url,
        })

    # De-dupe by slug
    seen = set()
    unique = []
    for e in events:
        if e["slug"] in seen:
            continue
        seen.add(e["slug"])
        unique.append(e)
    return unique


def _base_url(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _extract_date_before(html, slug):
    """RHP puts the date div before the h4. Find the last eventMonth block before this event's slug."""
    idx = html.find(f"/event/{slug}")
    if idx == -1:
        return ""
    snippet = html[max(0, idx - 2000):idx]
    # Last occurrence of the date block in that snippet
    date_matches = re.findall(
        r'class="[^"]*Date eventMonth[^"]*"[^>]*>\s*([^<]+?)\s*</div>',
        snippet,
    )
    if date_matches:
        return date_matches[-1].strip()
    return ""
