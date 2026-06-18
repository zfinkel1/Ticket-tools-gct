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

LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "300"))  # 5 min
HERE = os.path.dirname(os.path.abspath(__file__))
WATCHER = os.path.join(HERE, "watcher.py")

print(f"[worker] starting — running the watcher every {LOOP_SECONDS}s "
      f"(per-site throttle governs actual fetch cadence)", flush=True)
while True:
    try:
        subprocess.run([sys.executable, WATCHER, "--priority", "all"], cwd=HERE, check=False)
    except Exception as e:
        print(f"[worker] run failed: {e}", flush=True)
    time.sleep(LOOP_SECONDS)
