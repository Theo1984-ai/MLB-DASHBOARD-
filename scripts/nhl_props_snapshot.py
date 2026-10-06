"""
NHL Player Props snapshot — runs via GitHub Actions cron.

Saves a snapshot of all active Goals + SOG props to:
  nhl_props_history/YYYY-MM-DD.json

Multiple snapshots per day are appended to the same file so line movement
across the day is preserved (opening noon lines vs. pre-game 6 PM lines).
"""
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.nhl_props_scanner import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
HISTORY_DIR = os.path.join(ROOT, "nhl_props_history")

# Fields to persist per prop row (skip raw context stats to keep files lean)
SAVE_FIELDS = [
    "player", "market", "market_key", "line",
    "game", "away_team", "home_team", "first_pitch",
    "nv_over_pct", "nv_under_pct",
    "best_over_price", "best_over_book",
    "best_under_price", "best_under_book",
    "over_edge", "under_edge",
    "n_books",
    "over_FD", "over_BR", "under_FD", "under_BR",
]


def _resolve_api_key():
    key = os.environ.get("THE_ODDS_API_KEY")
    if not key:
        try:
            import tomllib
            with open(os.path.join(ROOT, ".streamlit", "secrets.toml"), "rb") as f:
                key = tomllib.load(f).get("THE_ODDS_API_KEY")
        except Exception:
            pass
    return key


def main(force=False, min_gap_min=60):
    api_key = _resolve_api_key()
    if not api_key:
        return {"status": "no_api_key"}

    now_et = datetime.now(tz=EASTERN)
    os.makedirs(HISTORY_DIR, exist_ok=True)

    date_key = now_et.strftime("%Y-%m-%d")
    out_path = os.path.join(HISTORY_DIR, f"{date_key}.json")

    # Load existing snapshots for today
    existing = {"date": date_key, "snapshots": []}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    snapshots = existing.get("snapshots", []) or []

    # Freshness guard — don't snapshot more than once per hour
    if not force and snapshots:
        try:
            last_ts = datetime.fromisoformat(snapshots[-1]["captured_at"])
            age_min = (now_et - last_ts).total_seconds() / 60
            if age_min < min_gap_min:
                return {
                    "status":     "skipped_fresh",
                    "path":       out_path,
                    "n_snapshots": len(snapshots),
                    "age_min":    round(age_min, 1),
                }
        except Exception:
            pass

    # Fetch props
    rows, dbg = scan(api_key)

    if not rows:
        return {
            "status":  "no_props",
            "path":    out_path,
            "debug":   dbg,
        }

    # Strip to save fields only
    slim_rows = []
    for r in rows:
        slim_rows.append({k: r.get(k) for k in SAVE_FIELDS})

    snapshots.append({
        "captured_at": now_et.isoformat(),
        "n_props":     len(slim_rows),
        "props":       slim_rows,
    })

    payload = {
        "date":        date_key,
        "n_snapshots": len(snapshots),
        "snapshots":   snapshots,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    return {
        "status":      "ok",
        "path":        out_path,
        "n_snapshots": len(snapshots),
        "n_props":     len(slim_rows),
        "date":        date_key,
    }


if __name__ == "__main__":
    force = "--force" in sys.argv
    result = main(force=force)
    print(result)
