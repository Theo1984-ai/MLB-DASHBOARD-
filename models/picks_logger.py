"""
Persistent log of user-selected round-robin picks.
Separate from models/logger.py (which logs raw model predictions).

File format: picks/YYYY-MM-DD.json
Stores the user's chosen 4-leg slate, round-robin structure, stake info,
and (after games complete) actual outcomes so we can compute hit rate /
P&L over time.
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
PICKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "picks")
os.makedirs(PICKS_DIR, exist_ok=True)


def _path(date_str: str) -> str:
    return os.path.join(PICKS_DIR, f"{date_str}.json")


def save_slate(date_str: str, slate: dict) -> str:
    """
    Save a slate dict containing keys:
      picks: list of {market, batter_or_team, point, side, model_p,
                      best_odds, decimal_odds, implied, edge_pp, game_pk,
                      matchup, kelly_stake, source_event_id}
      sizes: list of round-robin combo sizes (e.g. [2, 3, 4])
      stake_per_parlay: float
      total_stake: float
      total_ev: float
      saved_at: iso timestamp (added here)
      bankroll, kelly_mult, max_fraction (sidebar settings at save time)
    """
    slate = dict(slate)
    slate["saved_at"] = datetime.now(tz=EASTERN).isoformat()
    slate["date"] = date_str

    # If a slate file exists for this date, keep prior versions in history
    path = _path(date_str)
    history = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                history = existing.get("history", [])
                history.append({
                    k: existing[k] for k in existing if k != "history"
                })
        except (json.JSONDecodeError, OSError):
            pass

    slate["history"] = history
    with open(path, "w", encoding="utf-8") as f:
        json.dump(slate, f, indent=2, default=str)
    return path


def load_slate(date_str: str) -> dict | None:
    """Returns the most-recent slate for a date, or None."""
    path = _path(date_str)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def list_slate_dates() -> list[str]:
    if not os.path.exists(PICKS_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(PICKS_DIR)
        if f.endswith(".json") and len(f) == 15  # YYYY-MM-DD.json
    )


def load_all_slates() -> list[dict]:
    """All saved slates in chronological order, with their date attached."""
    out = []
    for d in list_slate_dates():
        s = load_slate(d)
        if s is not None:
            s.setdefault("date", d)
            out.append(s)
    return out


if __name__ == "__main__":
    sample = {
        "picks": [
            {"market": "hr", "batter_or_team": "Aaron Judge", "model_p": 0.18,
             "best_odds": 320, "decimal_odds": 4.20, "implied": 0.238,
             "edge_pp": -5.8, "matchup": "BOS @ NYY", "game_pk": 12345,
             "side": "Yes", "kelly_stake": 0.0},
        ],
        "sizes": [2, 3, 4],
        "stake_per_parlay": 5.0,
        "total_stake": 55.0,
        "total_ev": 2.50,
        "bankroll": 1000.0,
        "kelly_mult": 0.5,
        "max_fraction": 0.05,
    }
    p = save_slate("2026-05-03", sample)
    print(f"saved → {p}")
    print(f"loaded back: {load_slate('2026-05-03') is not None}")
    print(f"all dates: {list_slate_dates()}")
