"""
Bullpen fatigue calculator using MLB Stats API boxscore data.

Fetches the last N days of completed game boxscores and computes
per-team bullpen workload metrics:
  - appearances:    total relief appearances in window
  - total_pitches:  cumulative pitches thrown by relievers
  - avg_days_rest:  mean days since last appearance across relievers
  - fatigue_score:  0–100 composite (higher = more tired)
  - rating:         'fresh' | 'normal' | 'tired' | 'exhausted'
  - run_adj:        multiplier for expected runs (tired BP → more runs)

Fatigue matters for the Under strategy because:
  - If a starter exits early AND the bullpen is tired,
    the team may give up extra runs late.
  - Exhausted bullpen (score ≥ 70) adds ~0.3–0.5 runs per team.
"""

import json
import time
import urllib.request
import ssl as _ssl_compat
_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

WINDOW_DAYS = 3          # look back this many days
HIGH_FATIGUE_THRESHOLD   = 65
NORMAL_FATIGUE_THRESHOLD = 40


def _fetch(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "mlb-dashboard/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _get_completed_game_pks(date_str: str) -> list[int]:
    """Return gamePk list for all Final games on a given date."""
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}"
    data = _fetch(url)
    if not data:
        return []
    pks = []
    for day in data.get("dates", []):
        for g in day.get("games", []):
            if g.get("status", {}).get("abstractGameState", "") == "Final":
                pks.append(g["gamePk"])
    return pks


def _parse_boxscore_bullpen(game_pk: int, days_ago: int) -> dict[str, dict]:
    """
    Return {team_abbr: {appearances, pitches, pitcher_ids}} from one boxscore.
    `days_ago` is used to weight recency.
    """
    url = f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
    data = _fetch(url)
    if not data:
        return {}

    result = {}
    for side in ("away", "home"):
        team_data = data.get("teams", {}).get(side, {})
        abbr = team_data.get("team", {}).get("abbreviation", "")
        if not abbr:
            continue
        pitchers    = team_data.get("pitchers", [])   # list of player IDs
        players     = team_data.get("players", {})
        bp_pitchers = pitchers[1:]                    # skip starter (index 0)

        appearances = 0
        total_pc    = 0
        ids         = []
        for pid in bp_pitchers:
            pkey   = f"ID{pid}"
            pdata  = players.get(pkey, {})
            stats  = pdata.get("stats", {}).get("pitching", {})
            pc     = int(stats.get("pitchesThrown", 0) or 0)
            if pc > 0:
                appearances += 1
                total_pc    += pc
                ids.append(pid)

        if appearances > 0:
            result[abbr] = {
                "appearances": appearances,
                "pitches":     total_pc,
                "pitcher_ids": ids,
                "days_ago":    days_ago,
            }
    return result


def compute_bullpen_fatigue(
    today: Optional[str] = None,
    window: int = WINDOW_DAYS,
) -> dict[str, dict]:
    """
    Main entry point.  Returns {team_abbr: fatigue_dict} for all 30 teams.

    fatigue_dict keys:
      appearances    total BP appearances over window
      total_pitches  total pitches by relievers over window
      fatigue_score  0-100
      rating         'fresh' | 'normal' | 'tired' | 'exhausted'
      run_adj        float multiplier for expected runs
    """
    if today is None:
        today = date.today().isoformat()

    # Collect per-game bullpen data for last `window` days
    team_data: dict[str, list[dict]] = defaultdict(list)

    for days_ago in range(1, window + 1):
        d = (date.fromisoformat(today) - timedelta(days=days_ago)).isoformat()
        pks = _get_completed_game_pks(d)
        for pk in pks:
            parsed = _parse_boxscore_bullpen(pk, days_ago)
            for abbr, info in parsed.items():
                team_data[abbr].append(info)
            time.sleep(0.05)   # gentle rate-limiting

    # Aggregate
    results = {}
    for abbr, games in team_data.items():
        total_appearances = sum(g["appearances"] for g in games)
        total_pitches     = sum(g["pitches"]     for g in games)
        # Recency weight: games yesterday count more than 3 days ago
        weighted_app = sum(
            g["appearances"] * (window + 1 - g["days_ago"])
            for g in games
        )

        # Fatigue score: normalize to 0-100
        # League average BP: ~3 appearances/game × 3 games = 9 appearances,
        # ~15 pitches each = 135 pitches over 3 days.
        # Heavy usage = 12+ appearances or 200+ pitches.
        app_score   = min(total_appearances / 14.0 * 100, 100)
        pitch_score = min(total_pitches     / 220.0 * 100, 100)
        recency_sc  = min(weighted_app      / 30.0  * 100, 100)
        score       = int(app_score * 0.35 + pitch_score * 0.40 + recency_sc * 0.25)
        score       = max(0, min(score, 100))

        if score >= 70:
            rating  = "exhausted"
            run_adj = 1.06
        elif score >= HIGH_FATIGUE_THRESHOLD:
            rating  = "tired"
            run_adj = 1.03
        elif score >= NORMAL_FATIGUE_THRESHOLD:
            rating  = "normal"
            run_adj = 1.00
        else:
            rating  = "fresh"
            run_adj = 0.99

        results[abbr] = {
            "appearances":   total_appearances,
            "total_pitches": total_pitches,
            "fatigue_score": score,
            "rating":        rating,
            "run_adj":       run_adj,
            "games_sampled": len(games),
        }

    # Fill in any missing teams as normal
    all_abbrs = [
        "AZ","ATL","BAL","BOS","CHC","CWS","CIN","CLE","COL","DET",
        "HOU","KC","LAA","LAD","MIA","MIL","MIN","NYM","NYY","ATH",
        "PHI","PIT","SD","SF","SEA","STL","TB","TEX","TOR","WSH",
    ]
    for abbr in all_abbrs:
        if abbr not in results:
            results[abbr] = {
                "appearances":   0,
                "total_pitches": 0,
                "fatigue_score": 30,
                "rating":        "normal",
                "run_adj":       1.00,
                "games_sampled": 0,
            }

    return results


def get_game_fatigue_flag(
    away: str, home: str, fatigue: dict[str, dict]
) -> dict:
    """
    Return a summary flag for a single game.
    Keys: away_rating, home_rating, combined_run_adj, flag, note
    """
    a = fatigue.get(away, {"rating": "normal", "run_adj": 1.00, "fatigue_score": 30})
    h = fatigue.get(home, {"rating": "normal", "run_adj": 1.00, "fatigue_score": 30})

    combined_adj = ((a["run_adj"] - 1) + (h["run_adj"] - 1)) / 2 + 1
    max_score    = max(a["fatigue_score"], h["fatigue_score"])

    if max_score >= 70:
        flag = "🔴 exhausted"
        note = f"{away if a['fatigue_score']>h['fatigue_score'] else home} bullpen exhausted — under risk"
    elif max_score >= HIGH_FATIGUE_THRESHOLD:
        flag = "🟡 tired"
        note = f"One or both bullpens tired — slight under risk"
    else:
        flag = "🟢 fresh"
        note = "Both bullpens rested"

    return {
        "away_rating":    a["rating"],
        "home_rating":    h["rating"],
        "away_score":     a["fatigue_score"],
        "home_score":     h["fatigue_score"],
        "combined_adj":   round(combined_adj, 3),
        "flag":           flag,
        "note":           note,
    }


if __name__ == "__main__":
    from datetime import date
    print("Computing bullpen fatigue (last 3 days)...")
    fatigue = compute_bullpen_fatigue()
    print(f"\n{'Team':<6} {'Score':>6} {'Rating':<12} {'Apps':>5} {'Pitches':>8} {'RunAdj':>7}")
    print("-" * 50)
    for abbr, d in sorted(fatigue.items(), key=lambda x: -x[1]["fatigue_score"]):
        print(
            f"{abbr:<6} {d['fatigue_score']:>6} {d['rating']:<12} "
            f"{d['appearances']:>5} {d['total_pitches']:>8} {d['run_adj']:>7.3f}"
        )
