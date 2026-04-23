"""
RHP (Rockhouse Partners) venue CMS parser.

Works on both list and grid layouts. Pattern: every event has an anchor like
  <a class="url" href=".../event/[slug]/..." title="Event Name" rel="bookmark">
Date is usually in a preceding .eventMonth div.
"""

import html as html_lib
import re
import urllib.request

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def parse(site):
    req = urllib.request.Request(site["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="ignore")

    # Find every event anchor: <a ... href=".../event/SLUG/..." title="NAME" ...>
    # Accept both full URLs and relative.
    pattern = re.compile(
        r'<a[^>]+href="(?P<url>[^"]*?/event/(?P<slug>[a-z0-9-]+)[^"]*?)"[^>]*?title="(?P<name>[^"]+)"',
        re.IGNORECASE,
    )

    events = []
    for m in pattern.finditer(html):
        slug = m.group("slug")
        url = m.group("url")
        name = html_lib.unescape(m.group("name")).strip()
        date = _date_before(html, m.start())
        events.append({
            "slug": slug,
            "name": name,
            "location": "",  # venue is the site itself
            "date": date,
            "url": url,
        })

    # De-dupe by slug (list + grid layouts can repeat)
    seen = set()
    unique = []
    for e in events:
        if e["slug"] in seen:
            continue
        seen.add(e["slug"])
        unique.append(e)
    return unique


def _date_before(html, pos):
    """Look back ~1500 chars for the last eventMonth div's text content."""
    snippet = html[max(0, pos - 1500):pos]
    matches = re.findall(
        r'class="[^"]*eventMonth[^"]*"[^>]*>\s*([^<]+?)\s*</div>',
        snippet,
    )
    if matches:
        return matches[-1].strip()
    return ""
