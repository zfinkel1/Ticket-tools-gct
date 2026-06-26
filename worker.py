#!/usr/bin/env python3
"""
Always-on Railway worker for the venue watcher.

GitHub Actions silently throttled/stopped the cron (the same failure that killed
the SeatGeek tool's cron), so we run the watcher in a tight loop on a real
always-on host instead.

Each tick runs watcher.py for ALL sites. The PER-SITE throttle does the real
cadence work — default 1h (hourly), TAO 0.25h (15 min) — so a tick only actually
fetches the sites that are due. The loop just has to tick often enough to honor
the shortest interval (15 min), so we tick every 5 min. Running the watcher as a
subprocess keeps each run isolated: a single parser crash (or a SendGrid hiccup)
ends that one run, and the loop keeps going.
"""
import os
import sys
import time
import subprocess
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    CENTRAL = ZoneInfo("America/Chicago")
except Exception:
    CENTRAL = None

LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "300"))  # 5 min
HERE = os.path.dirname(os.path.abspath(__file__))
WATCHER = os.path.join(HERE, "watcher.py")

# ── Daily presale-email digest ────────────────────────────────────────────────
# Sends the GoldCoast presale digest once a day at PRESALE_HOUR Central (2pm).
# Gated behind PRESALE_EMAIL_ENABLED=1. PRESALE_SEND_NOW=1 fires one test send on
# boot (per process — survives until the next redeploy) regardless of time/gate.
PRESALE_EMAIL = os.path.join(HERE, "presale_email.py")
PRESALE_HOUR = int(os.environ.get("PRESALE_HOUR", "14"))
STATE_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(HERE, "state")
PRESALE_LASTSENT = os.path.join(STATE_DIR, "presale_email_lastsent.txt")
PRESALE_RETRY_MIN = int(os.environ.get("PRESALE_RETRY_MIN", "20"))  # minutes between retries
_presale_last_attempt = 0.0  # epoch of the last send attempt this process


def _presale_last_sent():
    try:
        with open(PRESALE_LASTSENT) as f:
            return f.read().strip()
    except Exception:
        return ""


def _mark_presale_sent(daystr):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PRESALE_LASTSENT, "w") as f:
            f.write(daystr)
    except Exception as e:
        print(f"[presale] could not write state: {e}", flush=True)


def _run_presale_email(reason):
    print(f"[presale] sending digest ({reason})…", flush=True)
    r = subprocess.run([sys.executable, PRESALE_EMAIL], cwd=HERE,
                       env={**os.environ, "SEND": "1"}, check=False)
    ok = r.returncode == 0
    if not ok:
        print(f"[presale] send exited {r.returncode} — will retry in ~{PRESALE_RETRY_MIN} min", flush=True)
    return ok


def maybe_run_presale_email():
    global _presale_last_attempt
    now = datetime.now(CENTRAL) if CENTRAL else datetime.now()
    daystr = now.strftime("%Y-%m-%d")

    # HARD GUARANTEE: AT MOST ONE *delivered* digest per calendar day. The persistent
    # dated latch is written ONLY after a successful send, so a failed send (Flare
    # 429, SendGrid hiccup, a transient error) does NOT burn the day — a later tick
    # retries until it goes through (notably, after Flare's daily limit resets in the
    # evening). Once delivered, the latch blocks any re-send for the rest of the day.
    if _presale_last_sent() == daystr:
        return

    send_now = os.environ.get("PRESALE_SEND_NOW") == "1"
    daily_due = os.environ.get("PRESALE_EMAIL_ENABLED") == "1" and now.hour >= PRESALE_HOUR
    if not (send_now or daily_due):
        return

    # Throttle retries so a rate-limited/failed send recovers later in the day
    # without hammering the Flare API on every 5-min tick.
    if (time.time() - _presale_last_attempt) < PRESALE_RETRY_MIN * 60:
        return
    _presale_last_attempt = time.time()

    reason = "PRESALE_SEND_NOW test" if send_now else f"daily {PRESALE_HOUR}:00 Central"
    if _run_presale_email(reason):
        _mark_presale_sent(daystr)  # latch ONLY on success


print(f"[worker] starting — running the watcher every {LOOP_SECONDS}s "
      f"(per-site throttle governs actual fetch cadence)", flush=True)
while True:
    try:
        subprocess.run([sys.executable, WATCHER, "--priority", "all"], cwd=HERE, check=False)
    except Exception as e:
        print(f"[worker] run failed: {e}", flush=True)
    try:
        maybe_run_presale_email()
    except Exception as e:
        print(f"[presale] trigger failed: {e}", flush=True)
    time.sleep(LOOP_SECONDS)
