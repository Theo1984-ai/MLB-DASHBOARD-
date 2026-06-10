"""
Persistent log of user-selected round-robin picks.
Separate from models/logger.py (which logs raw model predictions).

File format: picks/YYYY-MM-DD.json
Stores the user's chosen 4-leg slate, round-robin structure, stake info,
and (after games complete) actual outcomes so we can compute hit rate /
P&L over time.

PERSISTENCE: writes to local disk AND pushes to GitHub when GITHUB_TOKEN
is available (Streamlit Cloud env). Without GitHub push, saves are lost
when the Streamlit Cloud container restarts (ephemeral filesystem).
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
PICKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "picks")
os.makedirs(PICKS_DIR, exist_ok=True)

OWNER = "Theo1984-ai"
REPO = "MLB-DASHBOARD-"
GH_PATH_PREFIX = "picks"


def _path(date_str: str) -> str:
    return os.path.join(PICKS_DIR, f"{date_str}.json")


def _gh_token():
    """Resolve GitHub token from Streamlit secrets or env."""
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        import streamlit as st
        if "GITHUB_TOKEN" in st.secrets:
            return st.secrets["GITHUB_TOKEN"]
    except Exception:
        pass
    return None


def _push_to_github(date_str: str, payload: dict):
    """Persist slate to GitHub via Contents API. Silent fail if no token."""
    tok = _gh_token()
    if not tok:
        return None
    try:
        from data import github_storage as gh
        return gh.save_json(tok, OWNER, REPO,
                            f"{GH_PATH_PREFIX}/{date_str}.json",
                            payload,
                            commit_msg=f"Round Robin slate {date_str}")
    except Exception:
        return None


def _pull_from_github(date_str: str):
    tok = _gh_token()
    if not tok:
        return None
    try:
        from data import github_storage as gh
        return gh.load_json(tok, OWNER, REPO,
                            f"{GH_PATH_PREFIX}/{date_str}.json")
    except Exception:
        return None


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

    # Also push to GitHub so the slate survives Streamlit Cloud restarts
    _push_to_github(date_str, slate)
    return path


def load_slate(date_str: str) -> dict | None:
    """Returns the most-recent slate for a date, or None.
    Tries local first, then falls back to GitHub (handles Streamlit Cloud
    redeploys where local files have been wiped)."""
    path = _path(date_str)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    # Fall back to GitHub
    gh_data = _pull_from_github(date_str)
    if gh_data is not None:
        # Mirror to local for fast subsequent reads (until next restart)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(gh_data, f, indent=2, default=str)
        except Exception:
            pass
        return gh_data
    return None


def list_slate_dates() -> list[str]:
    """Returns sorted slate dates from local + GitHub (union)."""
    out = set()
    if os.path.exists(PICKS_DIR):
        for f in os.listdir(PICKS_DIR):
            if f.endswith(".json") and len(f) == 15:
                out.add(f[:-5])
    # Add GitHub-stored dates
    tok = _gh_token()
    if tok:
        try:
            from data import github_storage as gh
            for entry in gh.list_dir(tok, OWNER, REPO, GH_PATH_PREFIX):
                name = entry.get("name", "")
                if name.endswith(".json") and len(name) == 15:
                    out.add(name[:-5])
        except Exception:
            pass
    return sorted(out)


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
