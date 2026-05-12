#!/usr/bin/env python3
"""
One-off extractor: pull `_av_db` out of concert_historical.py and dump it
as JSON for the watcher to consume.

Re-run this whenever the GCT venue-history database changes inside
concert_historical.py. The watcher reads state/gct-venue-history-db.json
on each run; no analyzer process required at runtime.

Usage:
    python tools/extract_venue_history.py
"""
import ast
import json
import sys
from pathlib import Path

ANALYZER_PATH = Path(
    "C:/Users/zfinkel/Documents/Batch Folder/GCT ANALYZER/concert_historical.py"
)
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
VENUE_OUT = STATE_DIR / "gct-venue-history-db.json"
A2V_OUT = STATE_DIR / "gct-artist-venues-db.json"


def extract_assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    return None


def _dump(db, path, label):
    if db is None:
        print(f"[error] {label} not found in concert_historical.py", file=sys.stderr)
        sys.exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)
    print(f"[ok] wrote {len(db):,} entries to {path.name}")


def main():
    if not ANALYZER_PATH.exists():
        print(f"[error] concert_historical.py not found at {ANALYZER_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(ANALYZER_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    _dump(extract_assignment(tree, "_av_db"), VENUE_OUT, "_av_db")
    _dump(extract_assignment(tree, "_a2v"), A2V_OUT, "_a2v")


if __name__ == "__main__":
    main()
