"""
Persistent daily prediction logging — append-only JSONL per date.
Enables real out-of-sample backtesting once enough days accumulate.

File format: predictions/YYYY-MM-DD.jsonl
One row per (game, batter) prediction. Re-running the same day is safe —
the writer dedupes on (game_pk, batter_id) keeping only the latest snapshot.
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
PRED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "predictions")
os.makedirs(PRED_DIR, exist_ok=True)


def _path(date_str: str) -> str:
    return os.path.join(PRED_DIR, f"{date_str}.jsonl")


def log_predictions(date_str: str, rows: list[dict]) -> str:
    """
    Write rows to the day's log file. Dedupes by (game_pk, batter_id) —
    last write wins (so re-running with updated data overwrites).
    Each row should already include 'game_pk' and 'batter_id'.
    Adds a 'logged_at' timestamp.

    Returns the file path written.
    """
    path = _path(date_str)
    existing = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (r.get("game_pk"), r.get("batter_id"))
                existing[key] = r

    now_iso = datetime.now(tz=EASTERN).isoformat()
    for r in rows:
        r = dict(r)  # don't mutate caller
        r["logged_at"] = now_iso
        key = (r.get("game_pk"), r.get("batter_id"))
        existing[key] = r

    with open(path, "w", encoding="utf-8") as f:
        for r in existing.values():
            f.write(json.dumps(r, default=str) + "\n")
    return path


def list_logged_dates() -> list[str]:
    """Return sorted list of dates with logged predictions."""
    if not os.path.exists(PRED_DIR):
        return []
    return sorted(
        f[:-6] for f in os.listdir(PRED_DIR)
        if f.endswith(".jsonl") and len(f) == 16  # YYYY-MM-DD.jsonl
    )


def load_predictions(date_str: str) -> list[dict]:
    """Load logged rows for a date. Empty list if no file."""
    path = _path(date_str)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


if __name__ == "__main__":
    sample = [
        {"game_pk": 12345, "batter_id": 660271, "batter_name": "Test Player",
         "p_hr_per_pa": 0.045, "p_at_least_one": 0.18},
    ]
    p = log_predictions("2026-05-01", sample)
    print(f"Wrote {p}")
    print(f"Logged dates: {list_logged_dates()}")
    print(f"Loaded back: {load_predictions('2026-05-01')[0]}")