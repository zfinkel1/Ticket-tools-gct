"""
Sales-window rendering for the alert email.

Parsers populate event["sales"] with a normalized shape:

    {
        "public_start":  ISO-8601 string or None,    # general onsale
        "public_end":    ISO-8601 string or None,
        "presales": [
            {"name": "Artist Presale", "start": "...", "end": "..."},
            ...
        ],
    }

This module renders that into a one-line HTML pill telling the reader
whether tickets are on sale now, when the next presale opens, etc.
Empty string when there's no usable sales data.
"""

from datetime import datetime, timezone


def _parse_iso(s):
    if not s:
        return None
    try:
        # TM emits "2026-05-09T10:00:00Z" — normalize to UTC-aware
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fmt_when(dt):
    """Sun May 10 · 10:00 AM CDT — local-ish, readable."""
    if dt is None:
        return ""
    # Convert to UTC and show as UTC; we don't know the user's TZ here.
    # The watcher email is read in browser; UTC is the safest display.
    try:
        return dt.astimezone(timezone.utc).strftime("%a %b %d · %I:%M %p UTC").replace(" 0", " ")
    except Exception:
        return ""


def sales_status_html(event):
    s = event.get("sales") or {}
    now = datetime.now(timezone.utc)

    public_start = _parse_iso(s.get("public_start"))
    public_end = _parse_iso(s.get("public_end"))
    presales = []
    for p in s.get("presales") or []:
        ps_start = _parse_iso(p.get("start"))
        ps_end = _parse_iso(p.get("end"))
        if not ps_start:
            continue
        presales.append({"name": p.get("name") or "Presale", "start": ps_start, "end": ps_end})

    # On sale now (public onsale active)
    if public_start and public_start <= now and (not public_end or now <= public_end):
        return _pill("On sale now", "#16a34a")

    # Past — public onsale window closed; usually means the show is sold or off
    if public_end and now > public_end:
        return _pill("Onsale closed", "#6b7280")

    # Active presale (and public hasn't started yet)
    for p in presales:
        if p["start"] <= now and (not p["end"] or now <= p["end"]):
            tail = ""
            if public_start:
                tail = f' · public {_fmt_when(public_start)}'
            return _pill(f'{p["name"]} active{tail}', "#f59e0b")

    # Future presale — show the soonest one
    upcoming_presales = sorted([p for p in presales if p["start"] > now], key=lambda x: x["start"])
    if upcoming_presales:
        next_p = upcoming_presales[0]
        return _pill(f'Presale {_fmt_when(next_p["start"])} · {next_p["name"]}', "#f59e0b")

    # Future public onsale
    if public_start and public_start > now:
        return _pill(f'On sale {_fmt_when(public_start)}', "#f59e0b")

    return ""


def _pill(text, color):
    return (
        '<div style="margin-top:6px;">'
        f'<span style="display:inline-block;background:{color};color:#fff;'
        f'font-size:10px;font-weight:800;padding:2px 8px;border-radius:4px;'
        f'letter-spacing:0.04em;text-transform:uppercase;">{text}</span>'
        '</div>'
    )
