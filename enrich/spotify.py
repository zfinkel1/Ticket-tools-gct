#!/usr/bin/env python3
"""
Artist popularity enrichment for ticket events.

Spotify deprecated `popularity` and `followers` on the public Web API
(client-credentials apps return null even though the search succeeds).
Flare/TicketFlipping retains enterprise access and exposes those fields
on its /api/all-events response — we already cache that response on each
watcher run (state/flare-events-cache.json), so we use it as the source
of truth.

Used by watcher.py to add a "popularity" badge to each new event in the
alert email — helps triage which announcements actually matter.
"""

import html
import json
import re
import time
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent.parent / "state" / "spotify-cache.json"

# In-process index over the Flare cache: {normalized_event_name: artist_data}
# Rebuilt at most once per hour so a long-running process picks up a fresh
# Flare cache when it lands.
_flare_index = None
_flare_index_built_at = 0
_FLARE_INDEX_TTL = 3600  # seconds


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
        json.dump(cache, f, ensure_ascii=False)


def _build_flare_index():
    """
    Walk the cached Flare all-events list and produce
    {normalized_event_name: {name, popularity, followers, ...}}.

    Multiple Flare events can share an event_name (same tour, many cities) —
    keep the entry with the highest spotify_popularity since the artist-level
    popularity should be identical and we want a non-null sample.
    """
    from .flare import EVENTS_CACHE_PATH
    if not EVENTS_CACHE_PATH.exists():
        return {}
    try:
        with open(EVENTS_CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        return {}
    def _to_int(v):
        if v is None or v == "": return None
        try: return int(float(v))
        except (TypeError, ValueError): return None

    index = {}
    for e in cache.get("events", []):
        raw_name = (e.get("event_name") or e.get("name") or "").strip()
        if not raw_name:
            continue
        # Flare returns these as strings — coerce to int up front so downstream
        # math (sorts, comparisons, badge thresholds) doesn't crash on str+int.
        pop = _to_int(e.get("spotify_popularity"))
        followers = _to_int(e.get("spotify_followers"))
        if pop is None and followers is None:
            continue
        # Normalize Flare event names through the same artist extractor we
        # apply to scraped titles. Then index under every progressive prefix
        # of the cleaned name ("sting eras tour" -> "sting", "sting eras",
        # "sting eras tour") so a watcher event titled just 'Sting' still
        # matches. On collisions (e.g. rapper Drake vs Drake Bell both
        # indexing 'drake'), the higher-popularity entry wins.
        artist = (extract_artist(raw_name) or raw_name).lower().strip()
        if not artist:
            continue
        record = {
            "name": raw_name,
            "popularity": pop,
            "followers": followers,
            "city_streams": e.get("spotify_city_streams"),
            "url": e.get("artist_url") or e.get("spotify_url"),
            "genres": [],
        }
        words = artist.split()
        for i in range(1, len(words) + 1):
            k = " ".join(words[:i])
            existing = index.get(k)
            if not existing or (pop or 0) > (existing.get("popularity") or 0):
                index[k] = record
    return index


def _get_flare_index():
    global _flare_index, _flare_index_built_at
    if _flare_index is None or time.time() - _flare_index_built_at > _FLARE_INDEX_TTL:
        _flare_index = _build_flare_index()
        _flare_index_built_at = time.time()
    return _flare_index


def _build_venue_at_pattern():
    """
    Build "\\s+at\\s+(?:the\\s+)?(<venue alternation>)\\b.*$" using venue
    names from sites.py so adding a new venue automatically extends the
    artist-name stripper. Falls back to a hardcoded list if sites.py
    can't be imported.
    """
    base_tokens = {
        "metro", "the rivers", "rivers", "tao", "the brothel",
        "aragon", "riv", "riviera", "salt shed", "park west",
        "house of blues", "hob", "empty bottle", "thalia hall",
    }
    try:
        import importlib
        sites_mod = importlib.import_module("sites")
        for s in getattr(sites_mod, "SITES", []):
            n = (s.get("name") or "").lower().strip()
            for sep in [" — ", " - "]:
                if sep in n:
                    n = n.split(sep)[0].strip()
            if not n or "tixr" in n or "frontgate" in n:
                continue
            base_tokens.add(n)
            # Also add the version without trailing "chicago" / "theatre"
            for tail in (" chicago", " theatre", " music hall", " ballroom"):
                if n.endswith(tail):
                    short = n[: -len(tail)].strip()
                    if short:
                        base_tokens.add(short)
    except Exception:
        pass
    # Longer alternatives first so the regex engine prefers full matches
    alternation = "|".join(re.escape(t) for t in sorted(base_tokens, key=len, reverse=True))
    return rf"\s+at\s+(?:the\s+)?(?:{alternation})\b.*$"


# Words that aren't artist names — strip when they appear at the end of a title.
TITLE_SUFFIX_NOISE = [
    r"\s+presents\b.*$",
    r"\s+live\s+in\s+chicago\b.*$",
    r"\s+live\s+at\s+.*$",
    _build_venue_at_pattern(),
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
    # Drop a trailing tour-version number ('Sting 3.0 Tour' -> after the
    # 'Tour' strip we have 'Sting 3.0' — finish the job).
    t = re.sub(r"\s+\d+(?:\.\d+)+$", "", t).strip()

    return t or None


def get_artist_data(artist_name):
    """
    Returns {'name', 'popularity', 'followers', 'url', 'genres', 'city_streams'}
    or None if no Flare event matches the artist name.

    Match strategy:
      1. Exact normalized match on Flare event_name
      2. Flare event_name starts with `artist_name + ' '` (e.g. 'sting'
         matches 'sting 3.0 tour' but not 'stingray symphony')

    A small on-disk cache (state/spotify-cache.json) memoizes per-artist
    results for 30 days so repeat lookups don't rebuild the index each time.
    """
    if not artist_name:
        return None

    cache = _load_cache()
    key = artist_name.lower().strip()
    if key in cache:
        cached = cache[key]
        if cached.get("cached_at", 0) > time.time() - 30 * 86400:
            return cached.get("data")

    # Both sides of the lookup go through extract_artist — the Flare index
    # keys are normalized when built, the input here is normalized by
    # enrich_event before calling. So exact match is correct, and substring
    # matching would just create wrong pairings (e.g. 'Drake' -> 'Drake Bell').
    index = _get_flare_index()
    data = index.get(key)

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
            "source": "flare",  # Backed by Flare's all-events cache, not Spotify direct
            "artist_query": artist,
            **data,
        }
    return event


def format_followers(n):
    """1234567 -> '1.2M'. Tolerates str input from the Flare cache."""
    if n is None:
        return ""
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def popularity_label(p):
    """0–100 popularity score → human label + color. Tolerates str input."""
    if p is None:
        return None
    try:
        p = int(float(p))
    except (TypeError, ValueError):
        return None
    if p >= 75:
        return ("HOT", "#dc2626")
    if p >= 55:
        return ("Strong", "#f59e0b")
    if p >= 35:
        return ("Mid", "#16a34a")
    return ("Niche", "#6b7280")
