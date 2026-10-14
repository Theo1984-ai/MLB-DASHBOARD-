"""
ESPN NFL Defensive Rankings (unofficial API, no key required)

Fetches each team's record via ESPN Core API to compute:
  - pts_allowed_pg  — points allowed per game → overall defensive rank
  - rush_td_rank    — rushing TDs allowed rank (proxy: rush yards allowed)
  - pass_td_rank    — passing TDs allowed rank (proxy: pass yards allowed)

Rank 1 = most TDs/points allowed = weakest defense = best matchup.

Results are cached in memory (module-level) for the process lifetime.
Call fetch_nfl_def_rankings() then get_def_rank() to look up a player's matchup.
"""
import json
import os
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# ESPN team ID ↔ Sleeper/standard abbreviation
_ESPN_IDS = {
    1: "ATL",  2: "BUF",  3: "CHI",  4: "CIN",  5: "CLE",
    6: "DAL",  7: "DEN",  8: "DET",  9: "GB",   10: "TEN",
   11: "IND", 12: "KC",  13: "LV",  14: "LAR",  15: "MIA",
   16: "MIN", 17: "NE",  18: "NO",  19: "NYG",  20: "NYJ",
   21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC",  25: "SF",
   26: "SEA", 27: "TB",  28: "WAS", 29: "CAR",  30: "JAX",
   33: "BAL", 34: "HOU",
}
_ABBREV_TO_ESPN = {v: k for k, v in _ESPN_IDS.items()}

_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_espn_def_cache.json")
_CACHE = None   # in-process cache; file cache survives process restarts


def _get(url, timeout=5):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    return json.loads(resp.read())


def _stat(stats_list, *names):
    """Return value of first matching stat name from a [{name, value}] list."""
    for s in stats_list:
        if s.get("name") in names:
            v = s.get("value")
            return float(v) if v is not None else None
    return None


def _fetch_team_record(espn_id, year):
    """
    Returns {pts_allowed_pg, rush_yds_allowed_pg, pass_yds_allowed_pg, games}
    via ESPN's per-team record endpoint.
    """
    url = (
        f"https://sports.core.api.espn.com/v2/sports/football/nfl"
        f"/seasons/{year}/types/2/teams/{espn_id}/record"
    )
    try:
        data = _get(url)
    except Exception:
        return {}

    for item in data.get("items", []):
        desc = (item.get("description") or item.get("type", {}).get("text", "")).lower()
        if "overall" not in desc and "total" not in desc:
            continue
        sl = item.get("stats", [])
        games = _stat(sl, "gamesPlayed", "games") or 1
        pts_against = _stat(sl, "pointsAgainst", "opponentPoints") or 0
        return {
            "games":          int(games),
            "pts_allowed":    pts_against,
            "pts_allowed_pg": pts_against / games,
        }
    return {}


def _fetch_team_stats(espn_id, year):
    """
    Pull rush yards allowed and pass yards allowed from team statistics.
    Returns {rush_yds_allowed, pass_yds_allowed, rush_td_allowed, pass_td_allowed} or {}
    """
    url = (
        f"https://sports.core.api.espn.com/v2/sports/football/nfl"
        f"/seasons/{year}/types/2/teams/{espn_id}/statistics"
    )
    try:
        data = _get(url)
    except Exception:
        return {}

    flat = {}
    for cat in data.get("splits", {}).get("categories", []):
        for s in cat.get("stats", []):
            name = s.get("name", "")
            val  = s.get("value")
            if val is not None:
                flat[name] = float(val)

    games = flat.get("games", 1) or 1

    # Try position-specific stat names ESPN uses (names vary by season)
    rush_td = (
        flat.get("rushingTouchdownsAllowed") or
        flat.get("opponentRushingTouchdowns") or
        flat.get("defensiveRushingTouchdowns")
    )
    pass_td = (
        flat.get("passingTouchdownsAllowed") or
        flat.get("opponentPassingTouchdowns") or
        flat.get("netPassingTouchdownsAllowed") or
        flat.get("defensivePassingTouchdowns")
    )
    rush_yds = (
        flat.get("rushingYardsAllowed") or
        flat.get("opponentRushingYards") or
        flat.get("defensiveRushingYards")
    )
    pass_yds = (
        flat.get("passingYardsAllowed") or
        flat.get("opponentPassingYards") or
        flat.get("netPassingYardsAllowed")
    )
    pts_ag = (
        flat.get("pointsAllowed") or
        flat.get("totalPointsAllowed") or
        flat.get("opponentTotalPoints")
    )

    result = {}
    if rush_td is not None:  result["rush_td_allowed"]      = rush_td
    if pass_td is not None:  result["pass_td_allowed"]      = pass_td
    if rush_yds is not None: result["rush_yds_allowed_pg"]  = rush_yds / games
    if pass_yds is not None: result["pass_yds_allowed_pg"]  = pass_yds / games
    if pts_ag is not None:   result["pts_allowed_stats"]    = pts_ag
    return result


def _rank_desc(raw, key):
    """Rank teams 1-N by descending value of key (1 = highest = weakest defense)."""
    sorted_teams = sorted(raw.keys(), key=lambda t: raw[t].get(key, 0), reverse=True)
    return {t: i + 1 for i, t in enumerate(sorted_teams)}


def _load_file_cache():
    """Load rankings from the daily file cache. Returns dict or None if stale/missing."""
    try:
        if not os.path.exists(_CACHE_PATH):
            return None
        age_hours = (datetime.now().timestamp() - os.path.getmtime(_CACHE_PATH)) / 3600
        if age_hours > 24:
            return None
        with open(_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_file_cache(data):
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def fetch_nfl_def_rankings(year=None):
    """
    Fetches all 32 teams (32 + 32 API calls, ~10-20s first load).
    File-cached for 24 hours; in-process cached for the lifetime of the worker.
    Returns {team_abbrev: {pts_allowed_pg, rush_td_rank, pass_td_rank, overall_rank, ...}}
    Rank 1 = weakest defense (most allowed) = best matchup for the offensive player.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    cached = _load_file_cache()
    if cached:
        _CACHE = cached
        return _CACHE

    if year is None:
        year = datetime.now(tz=EASTERN).year

    def _fetch_one(espn_id_abbrev):
        espn_id, abbrev = espn_id_abbrev
        rec   = _fetch_team_record(espn_id, year)
        stats = _fetch_team_stats(espn_id, year)
        return abbrev, {**rec, **stats} if (rec or stats) else None

    raw = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_one, item): item for item in _ESPN_IDS.items()}
        for fut in as_completed(futures):
            try:
                abbrev, data = fut.result()
                if data:
                    raw[abbrev] = data
            except Exception:
                pass

    if not raw:
        return {}

    # Build rankings — fall back chain:
    # rush_td → rush_yds_allowed_pg → pts_allowed_pg
    # pass_td → pass_yds_allowed_pg → pts_allowed_pg
    overall_ranks = _rank_desc(raw, "pts_allowed_pg")

    rush_key = "rush_td_allowed" if any("rush_td_allowed" in raw[t] for t in raw) \
               else "rush_yds_allowed_pg" if any("rush_yds_allowed_pg" in raw[t] for t in raw) \
               else "pts_allowed_pg"
    pass_key = "pass_td_allowed" if any("pass_td_allowed" in raw[t] for t in raw) \
               else "pass_yds_allowed_pg" if any("pass_yds_allowed_pg" in raw[t] for t in raw) \
               else "pts_allowed_pg"

    rush_ranks = _rank_desc(raw, rush_key)
    pass_ranks = _rank_desc(raw, pass_key)

    for abbrev in raw:
        raw[abbrev]["overall_rank"] = overall_ranks.get(abbrev, 16)
        raw[abbrev]["rush_td_rank"] = rush_ranks.get(abbrev, 16)
        raw[abbrev]["pass_td_rank"] = pass_ranks.get(abbrev, 16)
        raw[abbrev]["rush_rank_src"] = rush_key
        raw[abbrev]["pass_rank_src"] = pass_key

    _save_file_cache(raw)
    _CACHE = raw
    return raw


def clear_cache():
    global _CACHE
    _CACHE = None
    try:
        if os.path.exists(_CACHE_PATH):
            os.remove(_CACHE_PATH)
    except Exception:
        pass


def get_def_rank(rankings, opponent_abbrev, pos):
    """
    Returns (rank_int, label_str) for a player position vs a specific defense.
    rank 1 = weakest defense (best matchup), rank 32 = toughest.
    Returns (None, None) if data is unavailable.
    """
    if not rankings or not opponent_abbrev or opponent_abbrev not in rankings:
        return None, None

    d = rankings[opponent_abbrev]
    if pos in ("WR", "TE"):
        rank = d.get("pass_td_rank") or d.get("overall_rank")
        src  = "pass TDs allowed" if "pass_td_allowed" in d else \
               "pass yds allowed" if "pass_yds_allowed_pg" in d else "pts allowed"
    elif pos == "RB":
        rank = d.get("rush_td_rank") or d.get("overall_rank")
        src  = "rush TDs allowed" if "rush_td_allowed" in d else \
               "rush yds allowed" if "rush_yds_allowed_pg" in d else "pts allowed"
    elif pos == "QB":
        # QBs score both rushing and passing TDs — blend toward pass
        pass_r = d.get("pass_td_rank") or d.get("overall_rank") or 16
        rush_r = d.get("rush_td_rank") or d.get("overall_rank") or 16
        rank = round(pass_r * 0.6 + rush_r * 0.4)
        src  = "pass+rush TDs allowed"
    else:
        rank = d.get("overall_rank")
        src  = "pts allowed"

    return rank, src
