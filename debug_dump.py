#!/usr/bin/env python3
"""
One-off: fetch pages with Playwright and save rendered HTML for inspection.
Commits to debug/{slug}.html
"""

from pathlib import Path
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import stealth_sync
    STEALTH = True
except ImportError:
    STEALTH = False

out_dir = Path(__file__).parent / "debug"
out_dir.mkdir(exist_ok=True)


def dump_simple(context, slug, url):
    """Just load and save."""
    page = context.new_page()
    try:
        page.goto(url, timeout=45_000, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception as e:
        print(f"  [warn] {e}")
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    html = page.content()
    (out_dir / f"{slug}.html").write_text(html, encoding="utf-8")
    print(f"  saved {len(html)} chars to debug/{slug}.html")
    page.close()


def dump_tixr_via_scraperapi():
    """Hit Tixr's city search API via ScraperAPI (bypasses DataDome)."""
    import os, urllib.request, urllib.parse
    api_key = os.environ.get("SCRAPERAPI_KEY")
    if not api_key:
        print("  [tixr] SCRAPERAPI_KEY not set, skipping")
        return
    target = "https://www.tixr.com/api/events?city=chicago&page=1&pageSize=50"
    params = urllib.parse.urlencode({
        "api_key": api_key,
        "url": target,
        "premium": "true",
    })
    url = f"https://api.scraperapi.com?{params}"
    print(f"  [tixr] fetching {target} via ScraperAPI...")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read().decode("utf-8", errors="ignore")
        (out_dir / "tixr-chicago.json").write_text(body, encoding="utf-8")
        print(f"  [tixr] saved {len(body)} chars to debug/tixr-chicago.json")
    except Exception as e:
        print(f"  [tixr] ScraperAPI failed: {e}")


def dump_tixr_chicago(context):
    """(unused — replaced by dump_tixr_via_scraperapi)"""
    page = context.new_page()
    if STEALTH:
        try:
            stealth_sync(page)
            print("  [tixr] stealth enabled")
        except Exception as e:
            print(f"  [tixr] stealth failed: {e}")
    page.goto("https://www.tixr.com/", timeout=45_000, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=20_000)

    # 1) Click magnifying glass
    for sel in [
        'button[aria-label*="search" i]',
        'a[aria-label*="search" i]',
        '[data-testid*="search" i]',
        'button:has(svg[aria-label*="search" i])',
        'svg[aria-label*="search" i]',
        'button:has-text("Search")',
    ]:
        try:
            page.click(sel, timeout=2000)
            print(f"  [tixr] clicked {sel}")
            break
        except Exception:
            continue
    page.wait_for_timeout(1000)

    # 2) Type chicago in the search input
    for sel in ['input[placeholder*="search" i]', 'input[type="search"]', 'input[type="text"]']:
        try:
            page.fill(sel, "chicago", timeout=2000)
            print(f"  [tixr] typed into {sel}")
            break
        except Exception:
            continue
    page.wait_for_timeout(1500)

    # 3) Open the "by X" dropdown and pick "City"
    # Try a few likely button/select patterns
    clicked_dropdown = False
    for sel in ['button:has-text("by")', 'button:has-text("Event")', 'button:has-text("Venue")', 'button:has-text("Artist")']:
        try:
            page.click(sel, timeout=2000)
            clicked_dropdown = True
            print(f"  [tixr] opened dropdown via {sel}")
            break
        except Exception:
            continue

    if clicked_dropdown:
        page.wait_for_timeout(500)
        try:
            page.click('text="City"', timeout=3000)
            print("  [tixr] selected City")
        except Exception as e:
            print(f"  [tixr] City click failed: {e}")

    page.wait_for_timeout(3000)  # give results time to render
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    html = page.content()
    (out_dir / "tixr-chicago.html").write_text(html, encoding="utf-8")
    print(f"  [tixr] saved {len(html)} chars to debug/tixr-chicago.html")
    page.close()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        viewport={"width": 1440, "height": 900},
    )

    print("[dump] Rivers Casino Des Plaines")
    dump_simple(context, "rivers-desplaines", "https://www.riverscasino.com/desplaines/entertainment/event-center")

    browser.close()

print("[dump] Tixr via ScraperAPI")
dump_tixr_via_scraperapi()

print("[done]")
