#!/usr/bin/env python3
"""
Scoring for new-event email alerts.

Same backtest-derived logic as presale_analyzer.py's derived signals — but
running on the limited info we have at watcher time (event name, venue, date).
The watcher doesn't have presale-type flags / TM face price / etc, so this
produces a partial score using only the universally-available signals:

  - history (avg margin, profitable_pct, events count from gct-history-db.json)
  - day-of-week (league-aware for sports)
  - venue type / blacklist
  - pattern match (rivalry, Taylor Swift, Hamilton, etc.)
  - sports month penalty
  - auto-detected loser (artists with consistent negative margin)

Output is a score 1-99 plus a buy/watch/skip verdict and a component breakdown
so the email can show "why" the score is what it is.
"""
import json
import re
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
HISTORY_DB_PATH = STATE_DIR / "gct-history-db.json"


# ── League detection ─────────────────────────────────────────────────────────
_LEAGUE_HINTS = {
    "NHL": ["blackhawks", "rangers", "bruins", "penguins", "ducks", "stars",
            "predators", "lightning", "panthers", "avalanche", "golden knights",
            "canadiens", "maple leafs", "oilers", "flames", "senators", "sabres",
            "capitals", "islanders", "devils", "flyers", "blue jackets", "red wings"],
    "NBA": ["lakers", "warriors", "celtics", "knicks", "bulls", "heat",
            "nuggets", "76ers", "sixers", "bucks", "suns", "thunder", "mavericks",
            "clippers", "pelicans", "grizzlies", "raptors", "hawks", "hornets",
            "pistons", "magic", "wizards", "timberwolves", "trail blazers",
            "spurs", "jazz", "rockets", "cavaliers", "pacers"],
    "MLB": ["yankees", "red sox", "dodgers", "cubs", "white sox", "mets",
            "phillies", "braves", "astros", "guardians", "rays", "blue jays",
            "orioles", "twins", "athletics", "padres", "rockies", "diamondbacks",
            "marlins", "nationals", "pirates", "brewers", "reds", "cardinals",
            "royals", "tigers", "angels", "mariners"],
    "NFL": ["patriots", "cowboys", "eagles", "chiefs", "ravens", "bills",
            "packers", "steelers", "49ers", "jets", "saints", "rams",
            "broncos", "browns", "vikings", "lions", "bengals", "texans",
            "jaguars", "titans", "colts", "raiders", "chargers", "falcons",
            "buccaneers", "commanders", "dolphins", "seahawks"],
    "MLS": ["fc cincinnati", "fc dallas", "lafc", "atlanta united",
            "inter miami", "fire fc", "dynamo fc", "sounders", "timbers"],
    "NCAA": ["wildcats", "spartans", "fighting irish", "wolverines", "buckeyes",
             "tar heels", "longhorns", "sooners", "crimson tide",
             "gators", "razorbacks", "hokies", "huskies"],
}


def detect_league(event_name):
    n = (event_name or "").lower()
    for lg in ("NHL", "NBA", "MLB", "NFL", "MLS", "NCAA"):
        for hint in _LEAGUE_HINTS[lg]:
            if hint in n:
                return lg
    return None


_DOW_WEIGHTS = {
    "concert": [3, 4, 2, 3, 5, 6, 5],
    "MLB":     [-3, -5, -4, -1, 5, 8, 4],
    "NHL":     [-5, -5, -5, -2, 2, 8, 1],
    "NBA":     [-1, -4, 0, 0, 3, 7, 7],
    "NFL":     [6, -6, -3, 5, 0, 2, 8],
    "NCAA":    [-6, -4, 3, 1, 6, 10, 7],
    "MLS":     [-2, -8, 2, -3, 7, 1, 7],
    "sports":  [-3, -5, -3, 0, 4, 7, 5],
}


def _parse_date(date_str):
    """Parse the messy date strings that different venue parsers produce.

    Handled formats:
      - ISO: 2026-08-23, 2026-08-23T20:00:00
      - US: 8/23/2026
      - Human: 'Sun, Aug 23, 2026 - 9:00 PM' (the RHP/Empty Bottle style)
      - 'Aug 23, 2026', 'August 23, 2026'
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    # Try strict formats on a sensible length prefix of the string.
    for fmt, length in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10),
                        ("%m/%d/%Y", 10), ("%m-%d-%Y", 10)):
        try:
            return datetime.strptime(s[:length], fmt)
        except (ValueError, TypeError):
            continue
    # Human formats — extract month/day/year via regex so weekday prefix /
    # time suffix don't break the parse.
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        mon_name, day, year = m.group(1), int(m.group(2)), int(m.group(3))
        for fmt in ("%b", "%B"):
            try:
                mon = datetime.strptime(mon_name[:3] if fmt == "%b" else mon_name, fmt).month
                return datetime(year, mon, day)
            except (ValueError, TypeError):
                continue
    # Last-ditch: first 10 chars as YYYY-MM-DD
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except Exception:
        return None


def day_of_week_score(date_str, event_name=""):
    dt = _parse_date(date_str)
    if not dt:
        return 0
    dow = dt.weekday()
    source = "sports" if re.search(r"\b(vs\.?|@)\b", event_name or "", re.I) else "concert"
    if source == "sports":
        weights = _DOW_WEIGHTS.get(detect_league(event_name)) or _DOW_WEIGHTS["sports"]
    else:
        weights = _DOW_WEIGHTS["concert"]
    return weights[dow]


def venue_type_score(venue_name):
    n = (venue_name or "").lower()
    if any(w in n for w in ("theatre", "theater", "hall", "auditorium", "palace")):
        return 8
    if any(w in n for w in ("club", "lounge", "house of blues")):
        return 6
    if any(w in n for w in ("stadium", "field", "park", "coliseum")):
        return 3
    if any(w in n for w in ("amphitheater", "amphitheatre", "pavilion")):
        return -3
    return 0


# Rivalry pairs — must contain BOTH terms (in either order). Handles full team
# names like "New York Yankees vs. Boston Red Sox" that simple substring misses.
_RIVALRY_PAIRS = [
    ("yankees", "red sox"),
    ("cubs", "dodgers"),
    ("yankees", "mets"),
    ("ohio state", "michigan"),
    ("alabama", "auburn"),
    ("duke", "unc"),
    ("duke", "north carolina"),
    ("ucla", "usc"),
    ("michigan", "michigan state"),
    ("lakers", "celtics"),
    ("rangers", "islanders"),
]

_PATTERN_BONUSES = [
    ("Taylor Swift",    ("taylor swift",),                                +15),
    ("Eras Tour",       ("eras tour",),                                   +12),
    ("Ohio State",      ("ohio state buckeyes", "buckeyes vs."),          +8),
    ("Michigan",        ("michigan wolverines",),                         +6),
    ("Hamilton",        ("hamilton",),                                    +10),
    ("Playoff/Finals",  ("playoff", "world series", "stanley cup",
                         "nba finals"),                                   +8),
    ("Championship",    ("championship",),                                +5),
    ("Past-peak",       ("aerosmith", "panic! at the disco",
                         "chance the rapper", "lewis black",
                         "1776 - the musical", "nct dream"),              -10),
]


def pattern_bonus_score(event_name):
    n = (event_name or "").lower()
    total = 0
    matches = []
    # Rivalries: both teams must appear in the event name
    for team_a, team_b in _RIVALRY_PAIRS:
        if team_a in n and team_b in n:
            total += 15
            matches.append(f"Rivalry ({team_a}-{team_b})")
            break  # Only one rivalry bonus per event
    for label, kws, pts in _PATTERN_BONUSES:
        for kw in kws:
            if kw in n:
                total += pts
                matches.append(label)
                break
    return max(-15, min(20, total)), matches


def month_penalty_sports(date_str, event_name=""):
    if not re.search(r"\b(vs\.?|@)\b", event_name or "", re.I):
        return 0
    dt = _parse_date(date_str)
    if not dt:
        return 0
    m = dt.month
    if m == 4: return -8
    if m == 5: return -5
    if m == 8: return -4
    if m == 11: return -3
    if m in (10, 3, 1): return +3
    return 0


_VENUE_BLACKLIST = {
    "fiserv forum":     ({4, 5}, -10),
    "citi field":       (set(), -8),
    "upmc events center": (set(), -15),
    "galen center":     (set(), -10),
    "the watsco center at um": (set(), -12),
    "clearview arena":  (set(), -12),
    "desert diamond arena": (set(), -8),
}


def venue_blacklist_score(venue_name, date_str):
    v = (venue_name or "").lower().strip()
    if v not in _VENUE_BLACKLIST:
        return 0
    allowed_dows, penalty = _VENUE_BLACKLIST[v]
    if not allowed_dows:
        return penalty
    dt = _parse_date(date_str)
    if dt and dt.weekday() in allowed_dows:
        return 0
    return penalty


# ── History lookup from gct-history-db.json ──────────────────────────────────
_history_cache = None


def _load_history():
    global _history_cache
    if _history_cache is not None:
        return _history_cache
    if not HISTORY_DB_PATH.exists():
        _history_cache = {}
        return _history_cache
    try:
        with open(HISTORY_DB_PATH, encoding="utf-8") as f:
            _history_cache = json.load(f)
    except Exception:
        _history_cache = {}
    return _history_cache


def _normalize(s):
    """Match the analyzer's _history_normalize."""
    if not s:
        return ""
    s = s.lower()
    for sep in [" - ", ": ", " – "]:
        if sep in s:
            s = s.split(sep)[0].strip()
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"[^\w\s]", "", s).strip()
    return s


def lookup_history(event_name):
    db = _load_history()
    if not db:
        return None
    key = _normalize(event_name)
    if not key:
        return None
    if key in db:
        return db[key]
    # Word-level prefix match (avoid fuzzy "ive" / "iu" matching to "Live" etc.)
    # DB keys keep punctuation (e.g., "bruce springsteen & the e street band")
    # but our normalized lookup key strips it. So normalize the DB key too
    # before comparing words.
    key_words = key.split()
    best = None
    best_len = 0
    for db_key, data in db.items():
        if not db_key or db_key.startswith('"') or "," in db_key:
            continue
        norm_db_key = _normalize(db_key)
        db_words = norm_db_key.split()
        if not db_words or len(db_words) > len(key_words):
            continue
        if key_words[: len(db_words)] != db_words:
            continue
        if len(db_key) > best_len:
            best, best_len = data, len(db_key)
    return best


def history_score(hist):
    """Returns (points, breakdown_label) for the history component."""
    if not hist:
        return 0, "no history"
    margin = hist.get("avg_margin") or 0
    prof = hist.get("profitable_pct") or 0
    events = hist.get("events") or 0
    pts = 0
    if margin >= 50:   pts += 25
    elif margin >= 25: pts += 15
    elif margin >= 10: pts += 8
    elif margin >= 0:  pts += 3
    elif margin >= -10: pts += -5
    else:              pts += -15
    if prof >= 90:   pts += 10
    elif prof >= 75: pts += 6
    elif prof >= 60: pts += 3
    elif prof < 40:  pts -= 5
    label = f"{events}ev avg {margin:+.0f}% margin, {prof:.0f}% profitable"
    return pts, label


def auto_loser_score(event_name):
    """Same heuristic as presale_analyzer: avg margin < -10% over 5+ events."""
    hist = lookup_history(event_name)
    if not hist:
        return 0
    events = hist.get("events") or 0
    margin = hist.get("avg_margin") or 0
    if events >= 5 and margin < -10:
        return -10
    return 0


# ── Tier-level predictions (face / resale / margin per section tier) ────────
# Uses gct-venue-history-db.json (extracted from concert_historical's _av_db).
# Section-level (e.g., section 119 vs 122) isn't currently exported, only tier
# (floor / 100-level / 200-level / 300-level). Sufficient for "which tier to buy".
VENUE_HISTORY_DB_PATH = STATE_DIR / "gct-venue-history-db.json"
_venue_history_cache = None


def _load_venue_history():
    global _venue_history_cache
    if _venue_history_cache is not None:
        return _venue_history_cache
    if not VENUE_HISTORY_DB_PATH.exists():
        _venue_history_cache = {}
        return _venue_history_cache
    try:
        with open(VENUE_HISTORY_DB_PATH, encoding="utf-8") as f:
            _venue_history_cache = json.load(f)
    except Exception:
        _venue_history_cache = {}
    return _venue_history_cache


def predict_tiers(event_name, venue_name):
    """Returns list of {tier, predicted_face, predicted_resale, predicted_margin,
    count} for tiers we have history at this artist+venue. Sorted by margin desc.

    Used by the watcher to attach 'best tier to buy' info to each new-event alert.
    """
    db = _load_venue_history()
    if not db:
        return []
    artist = _normalize(event_name)
    venue = _normalize(venue_name)
    av_key = artist + "|||" + venue
    tier_data = None
    if av_key in db:
        tier_data = db[av_key]
    else:
        # Word-level prefix match on the combined key
        target_words = av_key.split("|||")
        for k in db:
            if "|||" not in k: continue
            db_artist, db_venue = k.split("|||", 1)
            if _normalize(db_venue) != venue: continue
            db_words = _normalize(db_artist).split()
            artist_words = target_words[0].split()
            if not db_words or len(db_words) > len(artist_words): continue
            if artist_words[: len(db_words)] != db_words: continue
            tier_data = db[k]
            break
    if not tier_data:
        return []
    out = []
    for tier, stats in tier_data.items():
        if stats.get("count", 0) < 3: continue
        out.append({
            "tier":             tier,
            "predicted_face":   stats.get("avg_cost", 0),
            "predicted_resale": stats.get("avg_sell", 0),
            "predicted_margin": stats.get("avg_margin", 0),
            "count":            stats.get("count", 0),
        })
    out.sort(key=lambda x: -x["predicted_margin"])
    return out


# ── Main scoring function ─────────────────────────────────────────────────────
def score_event(event_name, venue_name, event_date):
    """Returns dict with 'score' (1-99), 'rec' (buy/watch/skip), 'components'."""
    components = {}
    components["history"], hist_label = history_score(lookup_history(event_name))
    components["dow"] = day_of_week_score(event_date, event_name)
    pat, pat_labels = pattern_bonus_score(event_name)
    components["pattern"] = pat
    components["pattern_labels"] = pat_labels
    components["venue_type"] = venue_type_score(venue_name)
    components["venue_blacklist"] = venue_blacklist_score(venue_name, event_date)
    components["month"] = month_penalty_sports(event_date, event_name)
    components["auto_loser"] = auto_loser_score(event_name)
    components["history_label"] = hist_label

    # Base score 30 (neutral) so we have headroom in both directions.
    raw = 30 + sum(v for k, v in components.items()
                   if isinstance(v, (int, float)) and k != "history_label")
    score = max(1, min(99, raw))
    if score >= 65:
        rec = "buy"
    elif score >= 45:
        rec = "watch"
    else:
        rec = "skip"
    # Build a single-sentence "why" so emails always have a reason, even
    # when no signals fired (the common skip case).
    components["why"] = _build_why_sentence(components, rec)
    return {"score": score, "rec": rec, "components": components}


def _build_why_sentence(comp, rec):
    """One-line human explanation: what fired (positives) + what's missing
    (for skips)."""
    positives = []
    negatives = []
    hist_label = comp.get("history_label", "")
    hist_pts = comp.get("history", 0)
    if hist_pts >= 20:
        positives.append(f"strong history ({hist_label})")
    elif hist_pts >= 8:
        positives.append(f"decent history ({hist_label})")
    elif hist_pts <= -10:
        negatives.append(f"weak history ({hist_label})")
    elif hist_pts < 0:
        negatives.append(f"mixed history ({hist_label})")
    elif hist_label == "no history":
        negatives.append("no GCT history for this artist")

    if comp.get("pattern_labels"):
        positives.append("pattern: " + ", ".join(comp["pattern_labels"]))
    if comp.get("dow", 0) >= 6:
        positives.append(f"great day of week (+{comp['dow']})")
    elif comp.get("dow", 0) <= -5:
        negatives.append(f"bad day of week ({comp['dow']})")
    if comp.get("venue_type", 0) >= 6:
        positives.append("theater/club venue (high margin)")
    elif comp.get("venue_type", 0) <= -3:
        negatives.append("amphitheater venue (low margin)")
    if comp.get("venue_blacklist", 0):
        negatives.append("known loss-prone venue for this day")
    if comp.get("month", 0) <= -5:
        negatives.append("bad month for sports")
    if comp.get("auto_loser", 0):
        negatives.append("auto-flagged loser (5+ neg-margin events)")

    if rec == "buy":
        body = "Buy because: " + "; ".join(positives) if positives else "Buy (high score)"
    elif rec == "watch":
        body = "Watch — " + (("strong: " + "; ".join(positives)) if positives else "") + ((" weak: " + "; ".join(negatives)) if negatives else "")
        body = body.strip(" —") or "Watch — mixed signals"
    else:
        body = "Skip because: " + ("; ".join(negatives) if negatives else "no positive signals to support a buy")
    return body
