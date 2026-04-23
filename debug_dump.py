#!/usr/bin/env python3
"""
One-off: fetch a Playwright-protected page and save the rendered HTML
so we can inspect the real DOM and write a proper parser. Commits
`debug/{slug}.html` so we can pull and read it locally.

Usage in workflow: python debug_dump.py
"""

import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

TARGETS = [
    ("rivers-desplaines", "https://www.riverscasino.com/desplaines/entertainment/event-center"),
]

out_dir = Path(__file__).parent / "debug"
out_dir.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        viewport={"width": 1440, "height": 900},
    )
    for slug, url in TARGETS:
        print(f"[dump] {url}")
        page = context.new_page()
        try:
            page.goto(url, timeout=45_000, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception as e:
            print(f"  [warn] {e}")
        # Scroll to trigger lazy-loaded content
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        html = page.content()
        (out_dir / f"{slug}.html").write_text(html, encoding="utf-8")
        print(f"  saved {len(html)} chars to debug/{slug}.html")
        page.close()
    browser.close()

print("[done]")
