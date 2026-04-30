#!/usr/bin/env python3
"""
Spotify enrichment for ticket events.

Looks up the artist's popularity (0–100) and follower count via the Spotify
Web API and caches the result on disk so we don't hammer the API across runs.

Used by watcher.py to add a "popularity" badge to each new event in the
alert email — helps triage at a glance which announcements actually matter.

Auth: client credentials flow (no user login needed).
"""

import base64
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent.parent / "state" / "spotify-cache.json"
TOKEN_CACHE_PATH = Path(__file__).resolve().parent.parent / "state" / "spotify-token.json"

# Hardcoded fallback (same creds used in flare_analyzer.py). Env vars take
# precedence so we can override in CI without touching code.
SP_ID_DEFAULT = "d83607186f454b0b8f96c13af5daf14d"
SP_SEC_DEFAULT = "c53b7fa0e0504d8492295c375d38a9bb"


def _creds():
    return (
        os.environ.get("SPOTIFY_CLIENT_ID") or SP_ID_DEFAULT,
        os.environ.get("SPOTIFY_CLIENT_SECRET") or SP_SEC_DEFAULT,
    )


def _load_cache():
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _get_token():
    """Client-credentials token. Cached on disk for ~1h."""
    if TOKEN_CACHE_PATH.exists():
        try:
            with open(TOKEN_CACHE_PATH, encoding="utf-8") as f:
                tok = json.load(f)
            if tok.get("expires_at", 0) > time.time() + 60:
                return tok["access_token"]
        except Exception:
            pass

    sp_id, sp_sec = _creds()
    auth = base64.b64encode(f"{sp_id}:{sp_sec}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read())

    token = body["access_token"]
    expires_at = time.time() + body.get("expires_in", 3600)
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"access_token": token, "expires_at": expires_at}, f)
    return token


# Words that aren't artist names — strip when they appear at the end of a title.
TITLE_SUFFIX_NOISE = [
    r"\s+presents\b.*$",
    r"\s+live\s+in\s+chicago\b.*$",
    r"\s+live\s+at\s+.*$",
    r"\s+at\s+(metro|the\s+rivers|tao|the\s+brothel|aragon|riv|riviera|salt\s+shed|park\s+west|house\s+of\s+blues|hob|empty\s+bottle|thalia\s+hall)\b.*$",
    r"\s+tour\s+\d{4}.*$",
    r"\s+tour\b.*$",
    r"\s+world\s+tour\b.*$",
    r"\s+\d{4}\s+tour\b.*$",
    r"\s+album\s+release.*$",
    r"\s+residency.*$",
    r"\s+night\s+\d+.*$",
    r"\s+\d+\s+night\s+run\b.*$",
    r"\s+w/.*$",
    r"\s+with\s+.*$",
    r"\s+ft\.?\s+.*$",
    r"\s+feat\.?\s+.*$",
    r"\s+\(.*\)$",
    r"\s+\[.*\]$",
]


CANCELLED_PREFIX = re.compile(r"^(cancelled|canceled|postponed|sold\s*out)\s*[:\-–]?\s*", re.IGNORECASE)


def extract_artist(title):
    """
    Best-effort artist name from an event title. We split on common
    separators and strip suffixes, then return the first chunk.
    """
    if not title:
        return None
    # Decode HTML entities (state files contain &#038; etc.)
    t = html.unescape(title).strip()
    # Strip CANCELLED: / POSTPONED: prefixes
    t = CANCELLED_PREFIX.sub("", t).strip()

    # Split on a primary separator if present
    for sep in [" - ", " – ", " — ", " | ", " // "]:
        if sep in t:
            t = t.split(sep, 1)[0].strip()
            break

    # Strip noisy suffixes (case-insensitive)
    for pat in TITLE_SUFFIX_NOISE:
        t = re.sub(pat, "", t, flags=re.IGNORECASE).strip()

    # Drop a trailing date or year
    t = re.sub(r"\s+\d{4}$", "", t).strip()
    t = re.sub(r"\s+\d{1,2}/\d{1,2}(/\d{2,4})?$", "", t).strip()

    return t or None


def _http_get_json(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_artist_data(artist_name):
    """
    Returns {'name', 'popularity', 'followers', 'url', 'genres'} or None
    if Spotify finds no match. Cached on disk by artist name.
    """
    if not artist_name:
        return None

    cache = _load_cache()
    key = artist_name.lower().strip()
    if key in cache:
        cached = cache[key]
        # 30-day TTL on cached entries (popularity drifts over time)
        if cached.get("cached_at", 0) > time.time() - 30 * 86400:
            return cached.get("data")

    try:
        token = _get_token()
    except Exception as e:
        print(f"[spotify] token fetch failed: {e}")
        return None

    try:
        url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
            {"q": artist_name, "type": "artist", "limit": 1}
        )
        body = _http_get_json(url, token)
    except urllib.error.HTTPError as e:
        print(f"[spotify] search HTTP {e.code} for {artist_name!r}")
        return None
    except Exception as e:
        print(f"[spotify] search failed for {artist_name!r}: {e}")
        return None

    items = body.get("artists", {}).get("items", [])
    if not items:
        cache[key] = {"cached_at": time.time(), "data": None}
        _save_cache(cache)
        return None

    a = items[0]
    data = {
        "name": a.get("name"),
        "popularity": a.get("popularity"),
        "followers": a.get("followers", {}).get("total"),
        "url": a.get("external_urls", {}).get("spotify"),
        "genres": a.get("genres", [])[:3],
    }
    cache[key] = {"cached_at": time.time(), "data": data}
    _save_cache(cache)
    return data


def enrich_event(event):
    """
    Mutates the event dict to add an 'enrichment' key with Spotify data.
    Returns the event for chaining. Safe to call repeatedly.
    """
    if event.get("enrichment"):
        return event
    artist = extract_artist(event.get("name", ""))
    if not artist:
        return event
    data = get_artist_data(artist)
    if data:
        event["enrichment"] = {
            "source": "spotify",
            "artist_query": artist,
            **data,
        }
    return event


def format_followers(n):
    """1234567 -> '1.2M'"""
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def popularity_label(p):
    """0–100 popularity score → human label + color"""
    if p is None:
        return None
    if p >= 75:
        return ("HOT", "#dc2626")
    if p >= 55:
        return ("Strong", "#f59e0b")
    if p >= 35:
        return ("Mid", "#16a34a")
    return ("Niche", "#6b7280")
