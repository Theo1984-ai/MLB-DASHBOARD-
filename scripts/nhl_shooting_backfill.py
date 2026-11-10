"""
One-off backfill of NHL shooting data (shots and goals per player per game) from the season start.

Run in GitHub Actions (ESPN is reachable there): python -m scripts.nhl_shooting_backfill [YYYY-MM-DD]
Saves best_plays_log/nhl_shooting/<date>.json for every day from the start date (default 2026-10-07, the season
opener) to today, skipping games already saved.
"""
import json
import os
import sys
from datetime import datetime, timedelta

from scripts import nhl_shooting as shoot

ROOT = "best_plays_log"


def _load(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save(path, data, msg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def main():
    from zoneinfo import ZoneInfo
    start = datetime.strptime(sys.argv[1] if len(sys.argv) > 1 else "2026-10-07", "%Y-%m-%d")
    end = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    day, total = start, 0
    while day.date() <= end.date():
        d = day.strftime("%Y-%m-%d")
        try:
            n = shoot.harvest_date(d, _load, _save, log_dir=ROOT)
        except Exception as e:  # noqa: BLE001
            print(f"{d}: error {e}")
            n = 0
        total += n
        print(f"{d}: {n} games saved")
        day += timedelta(days=1)
    print("total games saved:", total)


if __name__ == "__main__":
    main()
