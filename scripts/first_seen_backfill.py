"""One-off: rebuild best_plays_log/first_seen/<date>.json from the saved hourly snapshots.
Run from the repo root:  python -m scripts.first_seen_backfill
Existing records keep their first sighting and any result already graded."""
import glob
import json
import os
import re
from datetime import datetime

from scripts import first_seen as fs

ROOT = "best_plays_log"


def main():
    for day in sorted(os.listdir(f"{ROOT}/snapshots")):
        path = f"{ROOT}/{fs.LOG_SUB}/{day}.json"
        log = json.load(open(path)) if os.path.exists(path) else {}
        files = []
        for f in glob.glob(f"{ROOT}/snapshots/{day}/*_props.json"):
            m = re.fullmatch(r"([A-Z]+)_(\d{4})_props\.json", os.path.basename(f))
            if m:
                files.append((m[2], m[1], f))
        new = 0
        for hhmm, sport, f in sorted(files):
            snap = json.load(open(f))
            t = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=fs.EASTERN)
            new += fs.update_log(log, fs.split_plays(sport, snap, t), t.isoformat(), day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(log, open(path, "w"), separators=(",", ":"))
        print(day, len(files), "snapshots,", new, "new,", len(log), "total")


if __name__ == "__main__":
    main()
