"""
Shared parser helpers.

http_get_with_retry — wraps urllib.request with one retry on transient
failures (timeouts, 5xx, 429). Without retry, a single hiccup makes the
parser return [] for that run, which makes every event look "new" on the
NEXT run after recovery, spamming the alert email.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


def http_get_with_retry(url, headers=None, timeout=30, retries=1, backoff=2.0):
    """
    GET url with up to (retries+1) attempts. Returns the response body
    bytes. Retries on TimeoutError, URLError, and 5xx/429 HTTPError.
    Re-raises other HTTPErrors immediately (4xx config issues won't be
    fixed by retrying).
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_err = e
            # Retry only on rate-limit / server-side errors
            if e.code == 429 or 500 <= e.code <= 599:
                if attempt < retries:
                    time.sleep(backoff * (attempt + 1))
                    continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("http_get_with_retry: unreachable")


def scrapfly_fetch(url, render_js=False, wait_ms=0, timeout=180):
    """Fetch a URL through Scrapfly's anti-bot proxy (asp=true) and return the
    response body text (HTML or JSON). Replaces ScraperAPI for the Cloudflare /
    DataDome venues so the whole stack runs on one provider (Scrapfly), matching
    the SeatGeek/StubHub tool.

    render_js=True runs Scrapfly's headless browser — needed for JS-rendered
    pages (Rivers, a Gatsby/React site). Leave it off for plain JSON APIs (Tixr).
    Raises on a non-200 upstream so the watcher's retry/health logic still works.
    """
    key = os.environ.get("SCRAPFLY_KEY")
    if not key:
        raise RuntimeError("SCRAPFLY_KEY not set")
    params = {"key": key, "url": url, "asp": "true", "country": "us"}
    if render_js:
        params["render_js"] = "true"
        if wait_ms:
            params["rendering_wait"] = str(wait_ms)
    api = "https://api.scrapfly.io/scrape?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(api, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8", "replace"))
    result = payload.get("result", {})
    status = result.get("status_code")
    if status != 200:
        raise RuntimeError(f"Scrapfly upstream {status} for {url}")
    return result.get("content") or ""
