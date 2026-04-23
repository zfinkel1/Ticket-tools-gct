"""
Generic Playwright-powered parser for bot-protected sites.

The site config for Playwright entries looks like:
  {
    "name": "Rivers Casino",
    "parser": "playwright",
    "url": "https://www.riverscasino.com/desplaines/entertainment/event-center",
    "wait_for": "text=Event",      # optional CSS selector or "text=..." to wait on
    "event_selector": "a[href*='event']",  # optional; anchors with /event/ or similar
    "name_selector": "h2, h3, .title",      # optional; first match inside event block
    "date_selector": ".date, time, [class*='date']",  # optional
  }

If selectors are omitted, the parser falls back to "find anchors whose href
looks event-like, use their text as the name." Works for most venue pages
out of the box; add selectors to tighten once you see what the site returns.
"""

import re
from urllib.parse import urljoin, urlparse

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


EVENT_HREF_PATTERNS = [
    r"/event[s]?/",
    r"/show[s]?/",
    r"/calendar/",
    r"/performance/",
    r"/e/\d+",
]


def parse(site):
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "Playwright not installed. Add `pip install playwright && playwright install chromium`."
        )

    url = site["url"]
    wait_for = site.get("wait_for")
    event_selector = site.get("event_selector")
    name_selector = site.get("name_selector")
    date_selector = site.get("date_selector")

    events = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        # Give JS-rendered content a moment to hydrate
        page.wait_for_load_state("networkidle", timeout=15_000)
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=10_000)
            except Exception:
                pass

        if event_selector:
            blocks = page.query_selector_all(event_selector)
        else:
            # Fallback: any anchor whose href looks event-like
            blocks = page.query_selector_all("a")

        for el in blocks:
            try:
                href = el.get_attribute("href") or ""
            except Exception:
                href = ""
            if not href:
                continue
            if not event_selector and not _href_looks_event_like(href):
                continue

            full_url = urljoin(url, href)

            # Name
            if name_selector:
                try:
                    name_el = el.query_selector(name_selector)
                    name = (name_el.inner_text() if name_el else el.inner_text()).strip()
                except Exception:
                    name = ""
            else:
                try:
                    name = (el.inner_text() or "").strip()
                except Exception:
                    name = ""
            name = re.sub(r"\s+", " ", name)[:200]
            if not name:
                continue

            # Date
            date = ""
            if date_selector:
                try:
                    date_el = el.query_selector(date_selector)
                    if date_el:
                        date = date_el.inner_text().strip()
                except Exception:
                    pass

            events.append({
                "slug": _slug_from_url(full_url),
                "name": name,
                "location": site.get("name", ""),
                "date": date,
                "url": full_url,
            })

        browser.close()

    # De-dupe by slug
    seen = set()
    unique = []
    for e in events:
        if e["slug"] in seen:
            continue
        seen.add(e["slug"])
        unique.append(e)
    return unique


def _href_looks_event_like(href):
    return any(re.search(p, href) for p in EVENT_HREF_PATTERNS)


def _slug_from_url(url):
    # Use the path (minus query) as a stable slug
    path = urlparse(url).path.rstrip("/")
    return path or url
