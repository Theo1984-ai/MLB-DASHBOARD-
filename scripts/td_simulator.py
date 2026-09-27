"""
TD Scorer Monte Carlo Simulator

Downloads nflverse 2026 season stats, builds per-player TD rates,
then runs Monte Carlo simulation to estimate anytime TD scorer probability.

Composite weighting:
  RB  — blended TD rate × carry volume × goal-line efficiency (rush TDs/carry)
  WR/TE — blended TD rate × WOPR (opportunity) × end-zone efficiency (recv TDs/target)
  QB/FB — blended TD rate only

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

SKILL_POS = {"QB", "RB", "WR", "TE", "FB"}

# Position prior: historical NFL avg TDs/game + prior weight (equivalent games)
POS_PRIOR = {
    "RB": (0.55, 4),
    "WR": (0.30, 4),
    "TE": (0.22, 4),
    "QB": (0.12, 4),
    "FB": (0.18, 4),
}

# Smoothing priors for TD-rate derived metrics (prevents small-sample noise)
# rush_td_rate: add N ghost carries at position average
_RUSH_TD_PRIOR = {"RB": (0.05, 10), "QB": (0.04, 6), "FB": (0.06, 6)}
# recv_td_rate: add N ghost targets at position average
_RECV_TD_PRIOR = {"WR": (0.03, 12), "TE": (0.05, 10), "RB": (0.03, 8)}


def _smooth_rate(numerator, denominator, prior_rate, prior_n):
    return (numerator + prior_rate * prior_n) / (denominator + prior_n)


def _composite_score(pos, blended_rate, carries_per_game, rush_td_rate,
                     wopr, recv_td_rate):
    """Return a composite weight score for TD scorer share within a team."""
    base = blended_rate
    if pos == "RB":
        # High-carry starters and goal-line specialists score more
        carry_boost = max(min(carries_per_game / 10.0, 1.5), 0.05)
        gl_boost    = 1.0 + min(rush_td_rate * 5.0, 1.2)
        return base * carry_boost * gl_boost
    elif pos in ("WR", "TE"):
        # High-opportunity receivers (WOPR) + end-zone target share
        opp_boost = 1.0 + min((wopr or 0.0) * 2.0, 1.2)
        ez_boost  = 1.0 + min(recv_td_rate * 6.0, 1.0)
        return base * opp_boost * ez_boost
    else:
        return base


def _load_stats():
    """Return cached player stats dict, refreshing if stale."""
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                cached = json.load(f)
            age_h = (datetime.now(tz=timezone.utc).timestamp() - cached["_ts"]) / 3600
            if age_h < CACHE_TTL_HOURS:
                return cached["players"]
        except Exception:
            pass

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

        rush_td  = float(row.get("rushing_tds")   or 0)
        recv_td  = float(row.get("receiving_tds")  or 0)
        carries  = float(row.get("carries")        or 0)
        targets  = float(row.get("targets")        or 0)

        # nflverse may store as empty string or float string
        def _f(col, default=0.0):
            v = row.get(col, "") or ""
            try:
                return float(v)
            except Exception:
                return default

        target_share = _f("target_share")
        wopr         = _f("wopr")

        total_td = rush_td if pos == "QB" else rush_td + recv_td
        td_per_game = total_td / games

        # Skip pure non-contributors (no offensive involvement at all)
        if total_td == 0 and carries == 0 and targets == 0:
            continue

        prior_avg, prior_n = POS_PRIOR.get(pos, (0.25, 4))
        blended_rate = (total_td + prior_avg * prior_n) / (games + prior_n)

        carries_per_game = carries / games

        # Smoothed rate: TDs per carry (goal-line proxy for RBs)
        if pos in _RUSH_TD_PRIOR:
            rp_rate, rp_n = _RUSH_TD_PRIOR[pos]
            rush_td_rate = _smooth_rate(rush_td, carries, rp_rate, rp_n)
        else:
            rush_td_rate = rush_td / max(carries, 1)

        # Smoothed rate: TDs per target (end-zone proxy for WR/TE)
        if pos in _RECV_TD_PRIOR:
            ep_rate, ep_n = _RECV_TD_PRIOR[pos]
            recv_td_rate = _smooth_rate(recv_td, targets, ep_rate, ep_n)
        else:
            recv_td_rate = recv_td / max(targets, 1)

        composite = _composite_score(
            pos, blended_rate, carries_per_game,
            rush_td_rate, wopr, recv_td_rate,
        )

        key = name.lower()
        if key not in players or players[key]["games"] < games:
            players[key] = {
                "name":            name,
                "team":            team,
                "position":        pos,
                "games":           games,
                "total_tds":       total_td,
                "td_per_game":     round(td_per_game, 3),
                "blended_rate":    round(blended_rate, 4),
                # red zone / opportunity metrics
                "carries":         int(carries),
                "carries_per_game": round(carries_per_game, 2),
                "rush_td_rate":    round(rush_td_rate, 4),
                "targets":         int(targets),
                "target_share":    round(target_share, 4),
                "wopr":            round(wopr, 4),
                "recv_td_rate":    round(recv_td_rate, 4),
                "composite":       round(composite, 6),
            }

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"_ts": datetime.now(tz=timezone.utc).timestamp(), "players": players}, f)

    return players


def _team_roster(players, team):
    return [p for p in players.values() if p["team"] == team]


def simulate_td_probs(team_totals: dict, n_sims: int = 10000) -> dict:
    """
    team_totals: {team_abbrev: expected_points}  e.g. {"KC": 26.5, "IND": 20.5}
    Returns: {player_name_lower: {...}}
    """
    import random
    import math

    players  = _load_stats()
    results  = {}

    for team, team_total in team_totals.items():
        lam    = max(team_total / 10.0, 0.5)
        roster = _team_roster(players, team)
        if not roster:
            continue

        total_composite = sum(p["composite"] for p in roster)
        if total_composite == 0:
            for p in roster:
                p["_weight"] = 1.0 / len(roster)
        else:
            for p in roster:
                p["_weight"] = p["composite"] / total_composite

        names   = [p["name"] for p in roster]
        weights = [p["_weight"] for p in roster]

        td_counts = {n: 0 for n in names}
        for _ in range(n_sims):
            n_tds = 0
            L     = math.exp(-lam)
            k     = 0
            p_val = 1.0
            while True:
                k    += 1
                p_val *= random.random()
                if p_val <= L:
                    n_tds = k - 1
                    break
            for _ in range(n_tds):
                scorer = random.choices(names, weights=weights, k=1)[0]
                td_counts[scorer] += 1

        for p in roster:
            n    = p["name"]
            prob = td_counts[n] / n_sims
            results[n.lower()] = {
                "name":             n,
                "team":             team,
                "position":         p["position"],
                "games":            p["games"],
                "total_tds":        p["total_tds"],
                "td_per_game":      p["td_per_game"],
                "blended_rate":     p["blended_rate"],
                "carries_per_game": p["carries_per_game"],
                "rush_td_rate":     p["rush_td_rate"],
                "target_share":     p["target_share"],
                "wopr":             p["wopr"],
                "recv_td_rate":     p["recv_td_rate"],
                "share":            round(p["_weight"], 4),
                "model_prob":       round(prob, 4),
            }

    return results


def refresh_cache():
    """Force re-download stats regardless of cache age."""
    if os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)
    return _load_stats()


if __name__ == "__main__":
    print("Loading stats...")
    stats = _load_stats()
    print(f"  {len(stats)} skill players loaded")
    test = simulate_td_probs({"KC": 26.5, "IND": 20.5}, n_sims=5000)
    kc = sorted(
        [v for v in test.values() if v["team"] == "KC"],
        key=lambda x: -x["model_prob"]
    )
    print("\nKC top TD scorers (model):")
    for p in kc[:8]:
        print(
            f"  {p['name']:25s} {p['position']:3s}  {p['model_prob']*100:.1f}%  "
            f"TDs/g={p['td_per_game']:.3f}  "
            f"carries/g={p['carries_per_game']:.1f}  "
            f"rush_td%={p['rush_td_rate']*100:.1f}  "
            f"wopr={p['wopr']:.3f}  "
            f"recv_td%={p['recv_td_rate']*100:.1f}"
        )
