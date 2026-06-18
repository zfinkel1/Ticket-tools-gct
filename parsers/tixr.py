"""
Tixr parser via ScraperAPI (bypasses DataDome).

Config:
  {
    "name": "...", "parser": "tixr",
    "city": "chicago",     # Tixr API city filter (broad — see state filter)
    "page_size": 50,
    "state": "IL",         # optional, drops events whose venue.state doesn't match
  }

Uses Tixr's public events API:
  https://www.tixr.com/api/events?city=chicago&page=1&pageSize=50

Tixr's `city=chicago` filter is fuzzy — it returns events tagged or
associated with Chicago but the underlying venues can be in other states
(observed: "RiNo Bar" — Denver). Pass `state` in site config to drop
those. If venue.state is missing on a record we keep it (better to
over-include than to silently drop legit IL events with sparse metadata).

The API returns a JSON array of event objects. Each:
  {id, name, startDate (ms), venue: {name, city, state, country, timezone, shortName}, url, ...}
"""

import json
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ._common import scrapfly_fetch


def parse(site):
    city = site.get("city", "chicago")
    page_size = site.get("page_size", 50)
    target = f"https://www.tixr.com/api/events?city={urllib.parse.quote(city)}&page=1&pageSize={page_size}"

    # Scrapfly (asp) bypasses DataDome on Tixr's JSON API — no render needed.
    body = scrapfly_fetch(target)

    data = json.loads(body)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Tixr response: {type(data).__name__}")

    # Optional state filter — accept "IL", "Illinois", etc. Normalize to upper for compare.
    state_filter_raw = (site.get("state") or "").upper().strip()
    state_filter_aliases = set()
    if state_filter_raw:
        state_filter_aliases.add(state_filter_raw)
        # Map common abbrev/long-form pairs both ways
        STATE_ALIASES = {"IL": "ILLINOIS", "ILLINOIS": "IL"}
        if state_filter_raw in STATE_ALIASES:
            state_filter_aliases.add(STATE_ALIASES[state_filter_raw])

    events = []
    skipped_out_of_state = 0
    for ev in data:
        event_id = ev.get("id")
        name = (ev.get("name") or "").strip()
        url = ev.get("url") or ""
        venue_obj = ev.get("venue") or {}
        venue = venue_obj.get("name") or ""

        # State filter: skip if venue.state is set and doesn't match.
        # Missing state -> keep (don't drop on sparse metadata).
        if state_filter_aliases:
            ev_state = (venue_obj.get("state") or "").upper().strip()
            if ev_state and ev_state not in state_filter_aliases:
                skipped_out_of_state += 1
                continue

        ts = ev.get("startDate")  # ms since epoch (UTC)
        date = ""
        if ts:
            try:
                venue_tz = (ev.get("venue") or {}).get("timezone") or "America/Chicago"
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(ZoneInfo(venue_tz))
                date = dt.strftime("%a, %b %d at %I:%M %p %Z").replace(" 0", " ")
            except Exception:
                pass

        if not event_id or not name:
            continue

        # Tixr ships salesStartDate (ms epoch) and sometimes salesEndDate.
        # Normalize to ISO so enrich.sales can render without parser-specific knowledge.
        sales_start = ev.get("salesStartDate")
        sales_end = ev.get("salesEndDate")
        sales_block = {"public_start": None, "public_end": None, "presales": []}
        if sales_start:
            try:
                sales_block["public_start"] = datetime.fromtimestamp(
                    sales_start / 1000, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass
        if sales_end:
            try:
                sales_block["public_end"] = datetime.fromtimestamp(
                    sales_end / 1000, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass

        events.append({
            "slug": str(event_id),
            "name": name,
            "location": venue,
            "date": date,
            "url": url,
            "sales": sales_block,
        })

    if skipped_out_of_state:
        print(f"[tixr] skipped {skipped_out_of_state} event(s) outside state={state_filter_raw}")
    return events
