"""
Smart BUY recommendation — combines every signal we have into a single
top-of-row pill so the reader can triage 30 events in 10 seconds.

Inputs (all optional, gracefully degrades when missing):
  event["gct_venue_history"]  — section -> {avg_margin, count, ...}
  event["gct_history"]        — {events, avg_margin, profitable_pct, ...}
  event["enrichment"]         — Spotify {popularity, followers}
  event["sales"]              — {public_start, presales}
  event["price_range"]        — {min, max}

Output: ("BUY"/"WATCH"/"SKIP"/"PASS", reason_string)

Rules are conservative — only call BUY when we have multiple positive
signals. Default is WATCH when partial signal exists; PASS when nothing.
"""

from datetime import datetime, timezone


def _best_venue_section(venue_h):
    """Pick the section with the highest buy-count — most representative."""
    if not venue_h:
        return None, None
    items = sorted(venue_h.items(), key=lambda kv: -(kv[1].get("count") or 0))
    name, data = items[0]
    return name, data


def _is_active_sale_window(sales):
    """True if any presale or public onsale is active right now."""
    if not sales:
        return False
    now = datetime.now(timezone.utc)
    def _parse(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    ps = _parse(sales.get("public_start"))
    pe = _parse(sales.get("public_end"))
    if ps and ps <= now and (not pe or pe >= now):
        return True
    for p in sales.get("presales") or []:
        pst = _parse(p.get("start"))
        pen = _parse(p.get("end"))
        if pst and pst <= now and (not pen or pen >= now):
            return True
    return False


def compute_recommendation(event):
    """
    Returns (label, reason).
    Labels: "BUY", "STRONG WATCH", "WATCH", "PASS"
    Reason is a short string explaining the call.
    """
    venue_h = event.get("gct_venue_history") or {}
    artist_h = event.get("gct_history") or {}
    enr = event.get("enrichment") or {}
    sales = event.get("sales") or {}

    section, top_data = _best_venue_section(venue_h)
    venue_margin = (top_data or {}).get("avg_margin")
    venue_count = (top_data or {}).get("count")
    artist_margin = artist_h.get("avg_margin")
    artist_events = artist_h.get("events") or 0
    profitable_pct = artist_h.get("profitable_pct")
    popularity = enr.get("popularity")
    sale_active = _is_active_sale_window(sales)

    # ── BUY: strong venue-specific track record at this exact artist+venue ──
    if venue_margin is not None and venue_count and venue_count >= 5 and venue_margin >= 30:
        reason = f"GCT proved at {section}: {venue_count} buys, +{venue_margin}%"
        if sale_active:
            reason += " · on sale now"
        return ("BUY", reason)

    # ── BUY: strong artist record + multiple events, even without venue match ──
    if artist_margin is not None and artist_margin >= 50 and artist_events >= 5 and (profitable_pct or 0) >= 70:
        reason = f"{artist_events} past shows, avg +{artist_margin}%, {profitable_pct:.0f}% profitable"
        return ("BUY", reason)

    # ── STRONG WATCH: positive signals but lower confidence ──
    if venue_margin is not None and venue_margin >= 15:
        return ("STRONG WATCH", f"GCT @ {section}: +{venue_margin}% (n={venue_count})")
    if artist_margin is not None and artist_margin >= 25 and artist_events >= 3:
        return ("STRONG WATCH", f"+{artist_margin}% across {artist_events} shows")
    if popularity is not None and popularity >= 75 and sale_active:
        return ("STRONG WATCH", f"Spotify HOT ({popularity}) · on sale now")

    # ── WATCH: weak positive or any history at all ──
    if venue_margin is not None and venue_margin > 0:
        return ("WATCH", f"GCT @ {section}: +{venue_margin}% (light history)")
    if artist_margin is not None and artist_margin >= 0:
        return ("WATCH", f"{artist_events} past shows tracked")
    if popularity is not None and popularity >= 55:
        return ("WATCH", f"Spotify popularity {popularity}")

    # ── PASS: negative-margin artist history is a real signal to skip ──
    if artist_margin is not None and artist_margin < -20 and artist_events >= 3:
        return ("PASS", f"GCT lost on {artist_events} past shows ({artist_margin}%)")
    if venue_margin is not None and venue_margin < -10:
        return ("PASS", f"GCT lost at this venue ({venue_margin}%)")

    return (None, None)


def recommendation_html(event):
    """Render the recommendation as a pill at the top of an event row."""
    label, reason = compute_recommendation(event)
    if not label:
        return ""
    color_for = {
        "BUY":           "#16a34a",
        "STRONG WATCH":  "#f59e0b",
        "WATCH":         "#6b7280",
        "PASS":          "#dc2626",
    }
    color = color_for.get(label, "#6b7280")
    return (
        '<div style="margin-bottom:6px;">'
        f'<span style="display:inline-block;background:{color};color:#fff;'
        f'font-size:11px;font-weight:800;padding:3px 10px;border-radius:4px;'
        f'letter-spacing:0.05em;text-transform:uppercase;">{label}</span> '
        f'<span style="font-size:11px;color:#555;">{reason}</span>'
        '</div>'
    )
