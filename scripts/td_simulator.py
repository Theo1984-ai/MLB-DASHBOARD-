"""
TD Scorer Monte Carlo Simulator

Downloads nflverse 2026 season stats, builds per-player TD rates,
then runs Monte Carlo simulation to estimate anytime TD scorer probability.

Usage:
    from scripts.td_simulator import simulate_td_probs
    results = simulate_td_probs(team_totals_dict, n_sims=10000)
"""
import csv
import io
import json
import os
import ssl as _ssl
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_SSL    = _ssl._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")
STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_reg_2026.csv"
)
CACHE_PATH = os.path.join(os.path.dirname(__file__), "_td_stats_cache.json")
CACHE_TTL_HOURS = 6

# Positions that can score as a "TD scorer" (player crosses goal line)
SKILL_POS = {"QB", "RB", "WR", "TE", "FB"}

# Position prior: historical NFL avg TDs/game + prior weight (equivalent games)
# Blends early-season small samples toward realistic baseline
POS_PRIOR = {
    "RB": (0.55, 4),
    "WR": (0.30, 4),
    "TE": (0.22, 4),
    "QB": (0.12, 4),  # rush TDs only
    "FB": (0.18, 4),
}


def _load_stats():
    """Return cached player stats dict, refreshing if stale."""
    # Check cache
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                cached = json.load(f)
            age_h = (datetime.now(tz=timezone.utc).timestamp() - cached["_ts"]) / 3600
            if age_h < CACHE_TTL_HOURS:
                return cached["players"]
        except Exception:
            pass

    # Download fresh
    raw = urllib.request.urlopen(STATS_URL, timeout=30, context=_SSL).read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(raw))

    players = {}
    for row in reader:
        pos = row.get("position", "")
        if pos not in SKILL_POS:
            continue
        name  = row.get("player_display_name", "").strip()
        team  = (row.get("recent_team") or "").strip().upper()
        games = int(row.get("games") or 0)
        if not name or games == 0:
            continue

        rush_td = float(row.get("rushing_tds") or 0)
        recv_td = float(row.get("receiving_tds") or 0)

        # QBs score as TD scorers via RUSHING (not passing)
        if pos == "QB":
            total_td = rush_td
        else:
            total_td = rush_td + recv_td

        td_per_game = total_td / games
        # Bayesian regression to position mean
        prior_avg, prior_n = POS_PRIOR.get(pos, (0.25, 4))
        blended_rate = (total_td + prior_avg * prior_n) / (games + prior_n)

        key = name.lower()
        # Keep the entry with more games if duplicate
        if key not in players or players[key]["games"] < games:
            players[key] = {
                "name":         name,
                "team":         team,
                "position":     pos,
                "games":        games,
                "total_tds":    total_td,
                "td_per_game":  td_per_game,
                "blended_rate": blended_rate,
            }

    # Save cache
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"_ts": datetime.now(tz=timezone.utc).timestamp(), "players": players}, f)

    return players


def _team_roster(players, team):
    """Return list of player dicts for a given team abbreviation."""
    return [p for p in players.values() if p["team"] == team]


def simulate_td_probs(team_totals: dict, n_sims: int = 10000) -> dict:
    """
    team_totals: {team_abbrev: expected_points}  e.g. {"KC": 26.5, "IND": 20.5}
    Returns: {player_name_lower: {"prob": float, "td_per_game": float, ...}}
    """
    import random
    import math

    players = _load_stats()
    results = {}

    for team, team_total in team_totals.items():
        # Expected TDs for this team this game
        # NFL average: ~23 pts → ~2.3 TDs, so divisor ~10
        lam = max(team_total / 10.0, 0.5)

        roster = _team_roster(players, team)
        if not roster:
            continue

        total_td_rate = sum(p["blended_rate"] for p in roster)
        if total_td_rate == 0:
            for p in roster:
                p["_weight"] = 1.0 / len(roster)
        else:
            for p in roster:
                p["_weight"] = p["blended_rate"] / total_td_rate

        # Build weight list for fast sampling
        names   = [p["name"] for p in roster]
        weights = [p["_weight"] for p in roster]

        # Monte Carlo
        td_counts = {n: 0 for n in names}
        for _ in range(n_sims):
            # Sample number of team TDs from Poisson
            # Use Knuth algorithm for small lambda
            n_tds = 0
            L = math.exp(-lam)
            k = 0
            p_val = 1.0
            while True:
                k += 1
                p_val *= random.random()
                if p_val <= L:
                    n_tds = k - 1
                    break

            # Assign each TD to a player
            for _ in range(n_tds):
                scorer = random.choices(names, weights=weights, k=1)[0]
                td_counts[scorer] += 1

        # Convert to probability of scoring ≥1 TD
        for p in roster:
            n = p["name"]
            prob = td_counts[n] / n_sims
            results[n.lower()] = {
                "name":          n,
                "team":          team,
                "position":      p["position"],
                "games":         p["games"],
                "total_tds":     p["total_tds"],
                "td_per_game":   round(p["td_per_game"], 3),
                "blended_rate":  round(p["blended_rate"], 3),
                "share":         round(p["_weight"], 4),
                "model_prob":    round(prob, 4),
            }

    return results


def refresh_cache():
    """Force re-download stats regardless of cache age."""
    if os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)
    return _load_stats()


if __name__ == "__main__":
    # Quick test
    print("Loading stats...")
    stats = _load_stats()
    print(f"  {len(stats)} skill players loaded")

    # Test with sample team totals
    test = simulate_td_probs({"KC": 26.5, "IND": 20.5}, n_sims=5000)
    kc = sorted(
        [v for v in test.values() if v["team"] == "KC"],
        key=lambda x: -x["model_prob"]
    )
    print("\nKC top TD scorers (model):")
    for p in kc[:8]:
        print(f"  {p['name']:25s} {p['position']:3s}  {p['model_prob']*100:.1f}%  "
              f"({p['total_tds']:.0f} TDs / {p['games']} games)")
