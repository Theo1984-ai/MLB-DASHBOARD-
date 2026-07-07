"""
Daily True Probability snapshot — saves whatever the page would show at
the moment of execution, locked in for later settling.

Run from GitHub Actions cron once per day (e.g. 20:00 UTC = 4 PM ET) so
the snapshot lines up with when most game props are live.

Output: true_prob_history/YYYY-MM-DD.json (today's date in ET).

Usage:
    THE_ODDS_API_KEY=xxx python scripts/true_prob_snapshot.py
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.true_prob_scanner import scan  # noqa: E402


EASTERN = ZoneInfo("America/New_York")


def main():
    from scripts.snapshot_merger import (
        race_safe_git_pull, load_existing, merge_picks, summary as _sm,
    )

    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
        print("ERROR: THE_ODDS_API_KEY not set in environment", file=sys.stderr)
        sys.exit(1)

    today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    out_dir = os.path.join(ROOT, "true_prob_history")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{today_et}.json")

    print(f"Running True Probability scan for {today_et}...")
    race_safe_git_pull(ROOT)
    new_picks = scan(api_key, include_alts=True)
    print(f"  Scanner returned {len(new_picks)} qualifying picks (75%+ TP + EV>=3)")

    # APPEND+PERSISTENCE (7/7): merge into existing file so morning-only
    # signals aren't lost when afternoon cron finds a different set.
    existing = load_existing(out_path)
    def _key(p):
        return (str(p.get("game","") or p.get("event","")),
                str(p.get("market","")),
                str(p.get("player","") or p.get("selection","")),
                str(p.get("side","")),
                p.get("point"))
    merged, added, rehit = merge_picks(
        existing, new_picks, _key,
        rolling_fields=("best_price","best_book","true_prob_pct",
                        "ev_per_100","n_books","all_prices"),
        history_fields={"tp": "true_prob_pct", "ev": "ev_per_100",
                        "price": "best_price"},
    )
    print(f"  Merged: {added} new + {rehit} re-appearances "
          f"(had {len(existing)}, total {len(merged)})")

    snapshot = {
        "date":         today_et,
        "snapshot_at":  datetime.now(tz=EASTERN).isoformat(),
        "n_picks":      len(merged),
        "filter":       {
            "min_true_prob":     0.75,
            "min_books":         4,
            "min_price":         -400,
            "max_price":         None,
            "min_ev_per_100":    3.0,
        },
        "summary":      _sm(merged),
        "picks":        merged,
    }

    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)
    print(f"  Saved -> {out_path}")


if __name__ == "__main__":
    main()
