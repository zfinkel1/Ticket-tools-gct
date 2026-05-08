"""
Shared parser helpers.

http_get_with_retry — wraps urllib.request with one retry on transient
failures (timeouts, 5xx, 429). Without retry, a single hiccup makes the
parser return [] for that run, which makes every event look "new" on the
NEXT run after recovery, spamming the alert email.
"""

import time
import urllib.error
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
