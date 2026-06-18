"""
Rivers Casino event center parser.

Rivers (Des Plaines, maybe others) is a Gatsby/React site behind Cloudflare
anti-bot. Plain HTTP and even local Playwright get connection-reset, so we
route through ScraperAPI with render=true (residential proxy + JS rendering).
If SCRAPERAPI_KEY isn't set, falls back to local Playwright — useful for
debugging when Cloudflare relaxes.

HTML structure per event:
  <h3 ... class="Text-sldlea-0-h3 ...">Event Name</h3>
  <h5 ... class="Text-sldlea-0-h5 ...">Fri, Jul 31 @ 8PM</h5>
  <a ... href="/desplaines/entertainment/event-center/event-slug">Learn More</a>

We find each /event-center/[slug] link, then look backward ~1500 chars for
the most recent h3 (name) and h5 (date).
"""

import html as html_lib
import os
import re
import urllib.parse
import urllib.request
from urllib.parse import urljoin, urlparse

from ._common import scrapfly_fetch

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def _fetch_via_scraperapi(url, api_key):
    """Fetch a JS-rendered page through ScraperAPI. Used to bypass Cloudflare.
    render=true uses ScraperAPI's headless browser (10 credits/call premium)."""
    params = urllib.parse.urlencode({
        "api_key": api_key,
        "url": url,
        "premium": "true",
        "render": "true",
    })
    req = urllib.request.Request(f"https://api.scraperapi.com?{params}")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode("utf-8", errors="ignore")


def _fetch_via_playwright(url):
    """Local-only fallback when no ScraperAPI key is set."""
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Neither SCRAPERAPI_KEY nor Playwright available")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.goto(url, timeout=45_000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        html = page.content()
        browser.close()
        return html


def parse(site):
    url = site["url"]
    # Scrapfly (asp + render_js) bypasses Cloudflare and renders the Gatsby JS,
    # replacing ScraperAPI. Playwright stays only as a local no-key fallback.
    if os.environ.get("SCRAPFLY_KEY"):
        html = scrapfly_fetch(url, render_js=True, wait_ms=3000)
    elif PLAYWRIGHT_AVAILABLE:
        html = _fetch_via_playwright(url)
    else:
        raise RuntimeError("SCRAPFLY_KEY not set and Playwright unavailable")

    base = _base_url(url)
    events = []
    path_prefix = urlparse(url).path.rstrip("/")  # e.g. "/desplaines/entertainment/event-center"

    # Find each event URL on the page. Match anchors pointing under the same
    # venue path — works for Des Plaines and would work for other Rivers locations.
    pattern = re.compile(
        rf'href="({re.escape(path_prefix)}/[a-z0-9-]+)"',
        re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        href = m.group(1)
        slug = href.rsplit("/", 1)[-1]
        # Look backward from this match for the most recent h3 + date
        window = html[max(0, m.start() - 2500):m.start()]
        name = _last_tag_text(window, "h3")
        # Date lives in an h5 (featured card) OR a p.GridItemCommon__TextDate (grid card).
        # Take whichever is closer (appears later in the window).
        date = _find_date(window)
        if not name:
            continue
        events.append({
            "slug": slug,
            "name": name,
            "location": site.get("name", ""),
            "date": date or "",
            "url": urljoin(base, href),
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


def _last_tag_text(html, tag):
    matches = re.findall(
        rf"<{tag}[^>]*>([^<]+)</{tag}>",
        html,
        re.IGNORECASE,
    )
    if matches:
        return html_lib.unescape(matches[-1]).strip()
    return ""


def _find_date(html):
    """
    Rivers has two card layouts:
      Featured — <h5 class="Text-sldlea-0-h5 ...">Fri, Jul 31 @ 8PM</h5>
      Grid     — <p class="... GridItemCommon__TextDate...">FRI, JUN 19</p>
    Take whichever appears latest in the window (closest to the event link).
    """
    candidates = []
    for m in re.finditer(r"<h5[^>]*>([^<]+)</h5>", html, re.IGNORECASE):
        candidates.append((m.start(), m.group(1)))
    for m in re.finditer(
        r'<p[^>]*class="[^"]*GridItemCommon__TextDate[^"]*"[^>]*>([^<]+)</p>',
        html,
        re.IGNORECASE,
    ):
        candidates.append((m.start(), m.group(1)))
    if not candidates:
        return ""
    candidates.sort(key=lambda c: c[0])
    return html_lib.unescape(candidates[-1][1]).strip()


def _base_url(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"
