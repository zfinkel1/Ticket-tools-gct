"""
Carbonhouse RSS parser.

Many AEG / AXS venues run on Carbonhouse's CMS (Radius Chicago is one)
and publish a clean events RSS feed at /events/rss. The feed updates
within minutes of a new event going live and is structured XML — no
JS rendering, no anti-bot interstitials. Cheaper and more reliable
than the venue's Ticketmaster mirror.

Item titles include the event date in the form
"<Name> on <Mon DD, YYYY>" — split that off so the date lands in the
right column instead of bloating the event name.

Config:
  {
    "name": "Radius Chicago",
    "parser": "carbonhouse",
    "url": "https://www.radius-chicago.com/events/rss",
  }
"""

import html as html_lib
import re

from ._common import http_get_with_retry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL | re.IGNORECASE)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
LINK_RE = re.compile(r"<link>(.*?)</link>", re.DOTALL | re.IGNORECASE)
GUID_RE = re.compile(r"<guid[^>]*>(.*?)</guid>", re.DOTALL | re.IGNORECASE)
# Event-detail URLs end in .../events/detail/<numeric-id>
SLUG_RE = re.compile(r"/events/detail/(\d+)")
# "... on May 29, 2026" — the trailing date suffix on Carbonhouse titles
DATE_SUFFIX_RE = re.compile(r"\s+on\s+([A-Z][a-z]{2,9}\s+\d{1,2},\s+\d{4})\s*$")


def _clean(text):
    """Decode entities + strip a wrapping CDATA section if Carbonhouse emits one."""
    text = html_lib.unescape((text or "").strip())
    m = re.match(r"^<!\[CDATA\[(.*?)\]\]>$", text, flags=re.DOTALL)
    return (m.group(1) if m else text).strip()


def parse(site):
    body = http_get_with_retry(
        site["url"],
        headers={"User-Agent": USER_AGENT},
        timeout=30,
        retries=1,
    )
    xml = body.decode("utf-8", errors="ignore")

    events = []
    for raw in ITEM_RE.findall(xml):
        link_m = LINK_RE.search(raw)
        guid_m = GUID_RE.search(raw)
        url = _clean(link_m.group(1) if link_m else "")
        guid = _clean(guid_m.group(1) if guid_m else "")
        slug_m = SLUG_RE.search(guid) or SLUG_RE.search(url)
        if not slug_m:
            continue
        slug = slug_m.group(1)

        title_m = TITLE_RE.search(raw)
        title = _clean(title_m.group(1) if title_m else "")
        date = ""
        date_match = DATE_SUFFIX_RE.search(title)
        if date_match:
            date = date_match.group(1)
            name = title[: date_match.start()].strip()
        else:
            name = title

        events.append(
            {
                "slug": slug,
                "name": name,
                "location": "",
                "date": date,
                "url": url,
            }
        )

    # De-dupe (RSS feeds occasionally repeat the same event)
    seen = set()
    unique = []
    for e in events:
        if e["slug"] in seen:
            continue
        seen.add(e["slug"])
        unique.append(e)
    return unique
