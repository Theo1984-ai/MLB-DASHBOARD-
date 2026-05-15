"""
MLB Stats API client (statsapi.mlb.com) — public, no auth required.
"""

import urllib.request
import ssl
# System cert chain is broken on this Windows install — use unverified
# context for these public-data APIs. No auth/PII flows through these endpoints.
_UNVERIFIED_SSL = ssl._create_unverified_context()
import urllib.error
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
BASE = "https://statsapi.mlb.com/api/v1"


def _fetch(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mlb-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=12, context=_UNVERIFIED_SSL) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {}


# ---------- Schedule ----------

def get_schedule(target_date: str) -> list:
    """
    Returns list of game dicts for the date with weather, lineups, venue, probable pitchers.
    """
    url = (
        f"{BASE}/schedule"
        f"?sportId=1&date={target_date}"
        f"&hydrate=probablePitcher,linescore,team,weather,venue,lineups"
    )
    data = _fetch(url)
    dates = data.get("dates", [])
    if not dates:
        return []
    return dates[0].get("games", [])


def parse_first_pitch(game_date_str: str) -> tuple:
    """Returns (datetime in ET, formatted string)."""
    try:
        dt_utc = datetime.fromisoformat(game_date_str.replace("Z", "+00:00"))
        dt_et = dt_utc.astimezone(EASTERN)
        return dt_et, dt_et.strftime("%I:%M %p ET")
    except (ValueError, AttributeError):
        return None, "TBD"


# ---------- Pitcher ----------

PITCHER_KEYS = [
    "era", "wins", "losses", "strikeOuts", "whip", "inningsPitched",
    "homeRuns", "baseOnBalls", "hitsPer9Inn", "strikeoutsPer9Inn",
    "homeRunsPer9", "walksPer9Inn", "battingAvgAgainst", "ops",
    "gamesStarted", "saves",
]


def get_pitcher_season(person_id: int, season: int) -> dict:
    if not person_id:
        return _empty_pitcher()
    url = (
        f"{BASE}/people/{person_id}/stats"
        f"?stats=season&season={season}&group=pitching"
    )
    data = _fetch(url)
    try:
        splits = data["stats"][0]["splits"]
        if splits:
            s = splits[0]["stat"]
            return {
                "era":         s.get("era", "—"),
                "wins":        s.get("wins", 0),
                "losses":      s.get("losses", 0),
                "k":           s.get("strikeOuts", 0),
                "bb":          s.get("baseOnBalls", 0),
                "hr_allowed":  s.get("homeRuns", 0),
                "whip":        s.get("whip", "—"),
                "ip":          s.get("inningsPitched", "0.0"),
                "k_per9":      s.get("strikeoutsPer9Inn", "—"),
                "bb_per9":     s.get("walksPer9Inn", "—"),
                "hr_per9":     s.get("homeRunsPer9", "—"),
                "h_per9":      s.get("hitsPer9Inn", "—"),
                "avg_against": s.get("battingAvgAgainst", "—"),
                "ops_against": s.get("ops", "—"),
                "starts":      s.get("gamesStarted", 0),
            }
    except (KeyError, IndexError, TypeError):
        pass
    return _empty_pitcher()


def _empty_pitcher() -> dict:
    return {
        "era": "—", "wins": 0, "losses": 0, "k": 0, "bb": 0, "hr_allowed": 0,
        "whip": "—", "ip": "0.0", "k_per9": "—", "bb_per9": "—", "hr_per9": "—",
        "h_per9": "—", "avg_against": "—", "ops_against": "—", "starts": 0,
    }


def get_pitcher_splits(person_id: int, season: int) -> dict:
    """vs LHB / vs RHB pitching splits."""
    if not person_id:
        return {"vsL": {}, "vsR": {}}
    url = (
        f"{BASE}/people/{person_id}/stats"
        f"?stats=statSplits&season={season}&group=pitching&sitCodes=vl,vr"
    )
    data = _fetch(url)
    out = {"vsL": {}, "vsR": {}}
    try:
        for split in data["stats"][0]["splits"]:
            code = split.get("split", {}).get("code")
            stat = split.get("stat", {})
            payload = {
                "ops":     stat.get("ops", "—"),
                "avg":     stat.get("avg", "—"),
                "hr":      stat.get("homeRuns", 0),
                "k":       stat.get("strikeOuts", 0),
                "ab":      stat.get("atBats", 0),
            }
            if code == "vl":
                out["vsL"] = payload
            elif code == "vr":
                out["vsR"] = payload
    except (KeyError, IndexError, TypeError):
        pass
    return out


# ---------- Team ----------

def get_team_season(team_id: int, season: int, group: str = "hitting") -> dict:
    """group = 'hitting' or 'pitching'."""
    if not team_id:
        return {}
    url = (
        f"{BASE}/teams/{team_id}/stats"
        f"?stats=season&group={group}&season={season}"
    )
    data = _fetch(url)
    try:
        return data["stats"][0]["splits"][0]["stat"]
    except (KeyError, IndexError, TypeError):
        return {}


def get_team_recent(team_id: int, season: int, days: int = 15, group: str = "hitting") -> dict:
    """Last N days of team stats."""
    if not team_id:
        return {}
    today = datetime.now(tz=EASTERN).date()
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    url = (
        f"{BASE}/teams/{team_id}/stats"
        f"?stats=byDateRange&group={group}&startDate={start}&endDate={end}&season={season}"
    )
    data = _fetch(url)
    try:
        return data["stats"][0]["splits"][0]["stat"]
    except (KeyError, IndexError, TypeError):
        return {}


# ---------- Standings ----------

def get_standings(season: int) -> list:
    """
    Returns a list of division records with team standings.
    """
    url = f"{BASE}/standings?leagueId=103,104&season={season}"
    data = _fetch(url)
    return data.get("records", [])


# ---------- Roster (fallback when lineup not posted) ----------

def get_team_roster(team_id: int, season: int) -> list:
    """Returns active roster — used to find batters when no lineup is posted."""
    if not team_id:
        return []
    url = f"{BASE}/teams/{team_id}/roster?season={season}&rosterType=active"
    data = _fetch(url)
    return data.get("roster", [])


# ---------- League-wide date-range leaderboards ----------

def get_recent_hitting_leaderboard(season: int, days: int = 15, limit: int = 1000) -> dict:
    """
    Returns dict mapping player_id -> stat dict (HR, ops, avg, plateAppearances)
    over the last `days` days. One API call covers all of MLB.
    """
    today = datetime.now(tz=EASTERN).date()
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    url = (
        f"{BASE}/stats?stats=byDateRange&group=hitting&season={season}"
        f"&startDate={start}&endDate={end}&playerPool=all&limit={limit}"
    )
    data = _fetch(url)
    out = {}
    try:
        for split in data["stats"][0]["splits"]:
            pid = split.get("player", {}).get("id")
            if not pid:
                continue
            s = split.get("stat", {})
            out[pid] = {
                "hr":     s.get("homeRuns", 0),
                "ops":    s.get("ops", "—"),
                "avg":    s.get("avg", "—"),
                "pa":     s.get("plateAppearances", 0),
                "ab":     s.get("atBats", 0),
                "slg":    s.get("slg", "—"),
            }
    except (KeyError, IndexError, TypeError):
        pass
    return out


# ---------- Batched player handedness ----------

def get_pitcher_recent_form(person_id: int, season: int, n_starts: int = 3) -> dict:
    """
    Pulls the pitcher's last N starts via gameLog and computes recent
    averages (HR/9, K/9, velocity if available). Used to detect slumps
    and hot streaks beyond season aggregates.

    Returns dict with: n_recent_starts, recent_hr_per9, recent_k_per9,
    recent_bb_per9, recent_ip, recent_era. Empty dict if no game log.
    """
    if not person_id:
        return {}
    url = (
        f"{BASE}/people/{person_id}/stats"
        f"?stats=gameLog&group=pitching&season={season}&gameType=R"
    )
    data = _fetch(url)
    try:
        splits = data["stats"][0]["splits"]
    except (KeyError, IndexError):
        return {}
    if not splits:
        return {}

    # gameLog is chronological; we want last N starts
    starts = [s for s in splits if (s.get("stat", {}).get("gamesStarted", 0) or 0) > 0]
    starts = starts[-n_starts:]
    if not starts:
        return {}

    total_ip = 0.0
    total_hr = 0
    total_k = 0
    total_bb = 0
    total_er = 0
    for s in starts:
        st = s.get("stat", {})
        try:
            ip = float(st.get("inningsPitched", 0))
        except (ValueError, TypeError):
            continue
        if ip <= 0:
            continue
        total_ip += ip
        total_hr += st.get("homeRuns", 0) or 0
        total_k  += st.get("strikeOuts", 0) or 0
        total_bb += st.get("baseOnBalls", 0) or 0
        total_er += st.get("earnedRuns", 0) or 0

    if total_ip <= 0:
        return {}
    return {
        "n_recent_starts": len(starts),
        "recent_ip":       total_ip,
        "recent_hr_per9":  total_hr * 9.0 / total_ip,
        "recent_k_per9":   total_k * 9.0 / total_ip,
        "recent_bb_per9":  total_bb * 9.0 / total_ip,
        "recent_era":      total_er * 9.0 / total_ip,
    }


def get_team_il(team_id: int, season: int) -> set:
    """
    Returns the set of person_ids currently on any IL (10/15/60-day, NA, etc).
    Filters those out of the prediction pool so injured players don't
    show up as picks.
    """
    if not team_id:
        return set()
    il_ids: set = set()
    # MLB API exposes IL via roster types: il10, il15, il60
    for roster_type in ("40Man", "fullSeason"):
        url = f"{BASE}/teams/{team_id}/roster?rosterType={roster_type}&season={season}"
        data = _fetch(url)
        for r in data.get("roster", []):
            status = (r.get("status") or {}).get("code", "")
            # Status codes: A=active, IL10/IL15/IL60, RM=removed, SU=suspended,
            # NA=non-active. Treat any IL/non-active as "out".
            if status and status not in ("A",):
                pid = r.get("person", {}).get("id")
                if pid:
                    il_ids.add(pid)
    return il_ids


def get_team_bullpen_stats(team_id: int, season: int) -> dict:
    """
    Returns bullpen-only aggregates for a team:
      {era, ip, k_per9, hr_per9, whip, n_relievers}
    Defines a reliever as a pitcher with < 5 starts this season.
    Uses one batched people-with-stats call (after the roster fetch).
    """
    if not team_id:
        return {}
    roster = get_team_roster(team_id, season)
    pitcher_ids = [r["person"]["id"] for r in roster
                   if r.get("position", {}).get("type") == "Pitcher"
                   and r.get("person", {}).get("id")]
    if not pitcher_ids:
        return {}
    ids = ",".join(map(str, pitcher_ids))
    url = (
        f"{BASE}/people?personIds={ids}"
        f"&hydrate=stats(group=[pitching],type=[season],season={season})"
    )
    data = _fetch(url)
    total_ip = 0.0
    total_er = 0.0
    total_k = 0
    total_bb = 0
    total_hr = 0
    total_h = 0
    n_relievers = 0
    for p in data.get("people", []):
        stats_groups = p.get("stats", [])
        if not stats_groups:
            continue
        splits = stats_groups[0].get("splits", [])
        if not splits:
            continue
        s = splits[0].get("stat", {})
        gs = s.get("gamesStarted", 0) or 0
        if gs >= 5:  # treat as starter
            continue
        try:
            ip = float(s.get("inningsPitched", 0))
            era = float(s.get("era", 0))
        except (ValueError, TypeError):
            continue
        if ip <= 0:
            continue
        n_relievers += 1
        total_ip += ip
        total_er += era * ip / 9.0
        total_k  += s.get("strikeOuts", 0) or 0
        total_bb += s.get("baseOnBalls", 0) or 0
        total_hr += s.get("homeRuns", 0) or 0
        total_h  += s.get("hits", 0) or 0

    if total_ip <= 0:
        return {}

    return {
        "era":         (total_er * 9.0 / total_ip),
        "ip":          total_ip,
        "k_per9":      (total_k * 9.0 / total_ip),
        "bb_per9":     (total_bb * 9.0 / total_ip),
        "hr_per9":     (total_hr * 9.0 / total_ip),
        "whip":        ((total_h + total_bb) / total_ip),
        "n_relievers": n_relievers,
    }


def get_batter_game_log_rates(player_id: int, season: int) -> dict:
    """
    Returns per-game hit and total-base rates from the batter's game log.
    Used to calculate historical hit rates for alternate line edge analysis.

    Returns dict with:
      games, hits_1plus_rate, hits_2plus_rate,
      tb_1plus_rate, tb_2plus_rate, tb_3plus_rate
    Returns {} if no data available.
    """
    if not player_id:
        return {}
    url = (
        f"{BASE}/people/{player_id}/stats"
        f"?stats=gameLog&group=hitting&season={season}&gameType=R"
    )
    data = _fetch(url)
    try:
        splits = data["stats"][0]["splits"]
    except (KeyError, IndexError):
        return {}
    if not splits:
        return {}

    games = 0
    hits_1, hits_2 = 0, 0
    tb_1, tb_2, tb_3 = 0, 0, 0

    for s in splits:
        stat = s.get("stat", {})
        pa = stat.get("plateAppearances", 0) or 0
        if pa == 0:
            continue
        games += 1
        h  = stat.get("hits", 0) or 0
        tb = stat.get("totalBases", 0) or 0
        if h  >= 1: hits_1 += 1
        if h  >= 2: hits_2 += 1
        if tb >= 1: tb_1 += 1
        if tb >= 2: tb_2 += 1
        if tb >= 3: tb_3 += 1

    if games == 0:
        return {}
    return {
        "games":           games,
        "hits_1plus_rate": hits_1 / games,
        "hits_2plus_rate": hits_2 / games,
        "tb_1plus_rate":   tb_1  / games,
        "tb_2plus_rate":   tb_2  / games,
        "tb_3plus_rate":   tb_3  / games,
    }


def get_handedness(person_ids: list) -> dict:
    """
    Returns dict mapping person_id -> {'bats': 'L|R|S', 'throws': 'L|R'}.
    Batches in chunks of 100 IDs per request to stay under URL length limits.
    """
    out = {}
    if not person_ids:
        return out
    ids = [str(i) for i in person_ids if i]
    for i in range(0, len(ids), 100):
        chunk = ",".join(ids[i:i+100])
        url = f"{BASE}/people?personIds={chunk}"
        data = _fetch(url)
        for p in data.get("people", []):
            pid = p.get("id")
            if pid is None:
                continue
            out[pid] = {
                "bats":   p.get("batSide", {}).get("code", "R"),
                "throws": p.get("pitchHand", {}).get("code", "R"),
            }
    return out


if __name__ == "__main__":
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    season = int(today[:4])
    games = get_schedule(today)
    print(f"Schedule {today}: {len(games)} games")
    if games:
        g = games[0]
        print(f"  {g['teams']['away']['team']['name']} @ {g['teams']['home']['team']['name']}")
        print(f"  weather: {g.get('weather')}")
        ap_id = (g['teams']['away'].get('probablePitcher') or {}).get('id')
        if ap_id:
            ps = get_pitcher_season(ap_id, season)
            print(f"  pitcher season: ERA={ps['era']}, HR/9={ps['hr_per9']}, K/9={ps['k_per9']}, OPS-against={ps['ops_against']}")
            sp = get_pitcher_splits(ap_id, season)
            print(f"  pitcher splits: vsL OPS={sp['vsL'].get('ops')}, vsR OPS={sp['vsR'].get('ops')}")
        ht_id = g['teams']['home']['team']['id']
        ts = get_team_season(ht_id, season, "hitting")
        tr = get_team_recent(ht_id, season, 15, "hitting")
        print(f"  home team season HR={ts.get('homeRuns')}, OPS={ts.get('ops')}")
        print(f"  home team last-15 HR={tr.get('homeRuns')}, OPS={tr.get('ops')}")
    s = get_standings(season)
    print(f"  standings: {len(s)} divisions")
