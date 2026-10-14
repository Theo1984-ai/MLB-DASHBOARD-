"""
NHL Anytime Goal Scorer Scanner

Score formula (full — with NHL stats):
  40% true_prob + 25% team_implied_total + 20% def_rank + 15% goals_per_game

Score formula (def rank only, no player stats):
  50% true_prob + 30% team_implied_total + 20% def_rank

Score formula (odds-only fallback):
  65% true_prob + 35% team_implied_total

DEF rank: goals allowed per game per team from NHL standings
  rank 1 = most goals allowed = weakest goaltending = best matchup
  rank 32 = fewest goals allowed = toughest matchup

Books used: DraftKings, FanDuel, Bovada, BetRivers, Pinnacle, ESPN Bet, BetOnline
  (NO BetMGM / Caesars per project policy)
"""
import difflib
import json
import os
import ssl
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASTERN = ZoneInfo("America/New_York")

BOOKS = {"draftkings", "fanduel", "bovada", "betrivers", "pinnacle", "espnbet", "betonlineag"}
MIN_BOOKS = 1
MAX_PRICE_INCLUDE = 500   # skip huge longshots (>83% implied)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# NHL.com 3-letter abbrev → Odds API full team name
_TEAM_MAP = {
    "ANA": "Anaheim Ducks",
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames",
    "CAR": "Carolina Hurricanes",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montréal Canadiens",
    "NSH": "Nashville Predators",
    "NJD": "New Jersey Devils",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SJS": "San Jose Sharks",
    "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Hockey Club",
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights",
    "WSH": "Washington Capitals",
    "WPG": "Winnipeg Jets",
}
_TEAM_MAP_REV = {v: k for k, v in _TEAM_MAP.items()}

_ROSTER_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_nhl_roster_index.json")
_STATS_CACHE_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_nhl_skater_stats.json")

_ROSTER_INDEX  = None   # module-level: {normalized_name: {id, pos, team}}
_SKATER_STATS  = None   # module-level: {player_id: {goals_pg, shots_pg, gp}}


# ─── HTTP helpers ────────────────────────────────────────────────────────────

def _get(url, timeout=10):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    remaining = resp.headers.get("x-requests-remaining")
    return json.loads(resp.read()), remaining


_NHL_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def _nhl_get(url, timeout=8):
    req = urllib.request.Request(url, headers=_NHL_HEADERS)
    resp = urllib.request.urlopen(req, timeout=timeout, context=_CTX)
    return json.loads(resp.read())


def _imp(american):
    if american is None:
        return None
    if american >= 0:
        return 100 / (american + 100)
    return -american / (-american + 100)


# ─── NHL season helper ───────────────────────────────────────────────────────

def _current_season():
    now = datetime.now(tz=EASTERN)
    y = now.year
    return f"{y}{y + 1}" if now.month >= 9 else f"{y - 1}{y}"


# ─── NHL STANDINGS → defensive rank ─────────────────────────────────────────

def fetch_nhl_def_ranks():
    """
    Returns {team_abbrev: {goals_against_pg: float, rank: int}}
    rank 1 = most goals allowed = weakest goaltending = best matchup.
    """
    try:
        data = _nhl_get("https://api-web.nhle.com/v1/standings/now")
    except Exception:
        return {}

    teams = []
    for entry in data.get("standings", []):
        abbrev = entry.get("teamAbbrev", {}).get("default", "")
        ga     = entry.get("goalAgainst", 0)
        gp     = entry.get("gamesPlayed", 1) or 1
        if abbrev:
            teams.append((abbrev, ga / gp))

    if not teams:
        return {}

    # rank 1 = most goals allowed
    teams.sort(key=lambda x: x[1], reverse=True)
    return {
        abbrev: {"goals_against_pg": round(ga_pg, 2), "rank": i + 1}
        for i, (abbrev, ga_pg) in enumerate(teams)
    }


# ─── NHL ROSTERS → name index ────────────────────────────────────────────────

def _build_roster_index():
    """Fetches rosters for all 32 teams (parallelized). Returns {normalized_name: {id, pos, team}}."""
    index = {}

    def _fetch_one(abbrev):
        try:
            data = _nhl_get(f"https://api-web.nhle.com/v1/roster/{abbrev}/current")
        except Exception:
            return {}
        result = {}
        for group, pos_code in [("forwards", "F"), ("defensemen", "D"), ("goalies", "G")]:
            for p in data.get(group, []):
                pid   = p.get("id")
                first = p.get("firstName", {}).get("default", "")
                last  = p.get("lastName",  {}).get("default", "")
                if not pid or not last:
                    continue
                full = f"{first} {last}".strip().lower()
                abbr = f"{first[0].lower()}. {last.lower()}" if first else last.lower()
                entry = {"id": pid, "pos": pos_code, "team": abbrev}
                result[full] = entry
                result.setdefault(abbr, entry)
        return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_one, abbrev): abbrev for abbrev in _TEAM_MAP}
        for fut in as_completed(futures):
            try:
                index.update(fut.result())
            except Exception:
                pass

    return index


def load_roster_index():
    global _ROSTER_INDEX
    if _ROSTER_INDEX is not None:
        return _ROSTER_INDEX

    if os.path.exists(_ROSTER_CACHE_PATH):
        try:
            age_days = (datetime.now().timestamp() - os.path.getmtime(_ROSTER_CACHE_PATH)) / 86400
            if age_days < 7:
                with open(_ROSTER_CACHE_PATH, encoding="utf-8") as f:
                    _ROSTER_INDEX = json.load(f)
                return _ROSTER_INDEX
        except Exception:
            pass

    _ROSTER_INDEX = _build_roster_index()
    try:
        with open(_ROSTER_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_ROSTER_INDEX, f)
    except Exception:
        pass
    return _ROSTER_INDEX


def lookup_player(name, index):
    """Returns {id, pos, team} or None."""
    n = name.lower()
    if n in index:
        return index[n]
    hits = difflib.get_close_matches(n, index.keys(), n=1, cutoff=0.82)
    if hits:
        return index[hits[0]]
    return None


# ─── NHL PLAYER STATS (current season) ───────────────────────────────────────

def _fetch_player_landing(player_id):
    """Returns {goals_pg, shots_pg, gp} or None."""
    try:
        data   = _nhl_get(f"https://api-web.nhle.com/v1/player/{player_id}/landing")
        season = _current_season()
        fs     = data.get("featuredStats", {})
        # featuredStats.season matches current season
        if str(fs.get("season", "")) != season:
            # try first available regularSeason block
            rs = fs.get("regularSeason", {}).get("subSeason", {})
        else:
            rs = fs.get("regularSeason", {}).get("subSeason", {})
        gp      = rs.get("gamesPlayed", 0) or 0
        goals   = rs.get("goals", 0) or 0
        shots   = rs.get("shots", 0) or 0
        if gp == 0:
            return None
        return {
            "gp":        gp,
            "goals_pg":  round(goals / gp, 3),
            "shots_pg":  round(shots / gp, 1),
        }
    except Exception:
        return None


def load_skater_stats(player_ids):
    """
    Fetches current-season stats for a set of player IDs (parallelized).
    Returns {player_id: {goals_pg, shots_pg, gp}}.
    Uses a session-level module cache.
    """
    global _SKATER_STATS
    if _SKATER_STATS is None:
        _SKATER_STATS = {}

    missing = [pid for pid in player_ids if pid not in _SKATER_STATS]
    if not missing:
        return _SKATER_STATS

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_player_landing, pid): pid for pid in missing}
        for fut in as_completed(futures):
            pid = futures[fut]
            try:
                result = fut.result()
                if result:
                    _SKATER_STATS[pid] = result
            except Exception:
                pass

    return _SKATER_STATS


# ─── ODDS API ────────────────────────────────────────────────────────────────

def fetch_events(api_key):
    try:
        data, _ = _get(
            f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/events?apiKey={api_key}"
        )
        return data
    except Exception:
        return []


def fetch_team_totals(api_key):
    """Returns {full_team_name: implied_total}. Falls back to game total / 2."""
    totals = {}
    try:
        data, _ = _get(
            f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds"
            f"?apiKey={api_key}&regions=us&markets=team_totals&oddsFormat=american"
        )
        for game in data:
            for bm in game.get("bookmakers", []):
                if bm["key"] not in BOOKS:
                    continue
                for mkt in bm.get("markets", []):
                    if mkt["key"] != "team_totals":
                        continue
                    for out in mkt.get("outcomes", []):
                        team = out.get("description", "")
                        if out.get("name") == "Over" and out.get("point") and team:
                            totals.setdefault(team, out["point"])
    except Exception:
        pass

    if not totals:
        # Fall back to game totals split evenly
        try:
            data, _ = _get(
                f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds"
                f"?apiKey={api_key}&regions=us&markets=totals&oddsFormat=american"
            )
            for game in data:
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                for bm in game.get("bookmakers", []):
                    if bm["key"] not in BOOKS:
                        continue
                    for mkt in bm.get("markets", []):
                        if mkt["key"] != "totals":
                            continue
                        for out in mkt.get("outcomes", []):
                            if out.get("name") == "Over" and out.get("point"):
                                half = out["point"] / 2
                                totals.setdefault(home, half)
                                totals.setdefault(away, half)
        except Exception:
            pass

    return totals


def fetch_goal_props(api_key, event_id):
    """
    Returns {player_name: {book_key: american_price}} for anytime goal scorer (YES/NO).
    Uses player_goal_scorer_anytime market. Player name is in outcome['description'].
    """
    try:
        data, _ = _get(
            f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/events/{event_id}/odds"
            f"?apiKey={api_key}&regions=us,us2&markets=player_goal_scorer_anytime&oddsFormat=american"
        )
    except Exception:
        return {}

    players = defaultdict(dict)
    for bm in data.get("bookmakers", []):
        bk = bm["key"]
        if bk not in BOOKS:
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "player_goal_scorer_anytime":
                continue
            for out in mkt.get("outcomes", []):
                if out.get("name") != "Yes":
                    continue
                name  = out.get("description", "")
                price = out.get("price")
                if name and price is not None:
                    players[name][bk] = price
    return dict(players)


# ─── SCORING ─────────────────────────────────────────────────────────────────

def composite_score(true_prob, team_total, def_rank=None, goals_pg=None):
    """
    Returns 0-100 composite score.
    def_rank : 1 (weakest) … 32 (toughest)
    goals_pg : player's goals per game this season
    """
    tp = min(true_prob / 100, 1.0)
    tt = min((team_total or 2.5) / 5.0, 1.0)   # 5 goals = very high-scoring team

    # def_rank normalized: rank 1 → 1.0 (best matchup), rank 32 → 0.0 (toughest)
    dr = ((32 - def_rank) / 31) if def_rank is not None else None

    # goals_pg normalized: 0.5 goals/game = very hot scorer
    gf = min((goals_pg or 0) / 0.5, 1.0) if goals_pg is not None else None

    if gf is not None and dr is not None:
        raw = 0.40 * tp + 0.25 * tt + 0.20 * dr + 0.15 * gf
    elif dr is not None:
        raw = 0.50 * tp + 0.30 * tt + 0.20 * dr
    else:
        raw = 0.65 * tp + 0.35 * tt

    return round(raw * 100)


# ─── MAIN SCAN ───────────────────────────────────────────────────────────────

def scan(api_key, fetch_nhl_data=True):
    """
    Returns list of player dicts sorted by goal score descending.
    """
    events = fetch_events(api_key)
    if not events:
        return []

    team_totals = fetch_team_totals(api_key)

    # ── NHL defensive ranks ───────────────────────────────────────────────────
    def_ranks = fetch_nhl_def_ranks() if fetch_nhl_data else {}

    # ── NHL roster index (name → player id) ──────────────────────────────────
    roster_index = load_roster_index() if fetch_nhl_data else {}

    rows = []
    all_props = {}   # event_id → {player: {book: price}}
    event_meta = {}  # event_id → {home, away, home_total, away_total, ct}

    for ev in events:
        eid  = ev["id"]
        home = ev["home_team"]
        away = ev["away_team"]
        ct   = ev["commence_time"]

        # Skip already-started games
        try:
            gt  = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            now = datetime.now(tz=gt.tzinfo)
            if gt <= now:
                continue
        except Exception:
            pass

        props = fetch_goal_props(api_key, eid)
        if not props:
            continue

        all_props[eid]  = props
        event_meta[eid] = {
            "home": home, "away": away, "ct": ct,
            "home_total": team_totals.get(home, 0),
            "away_total": team_totals.get(away, 0),
        }

    if not all_props:
        return []

    # ── Collect unique player IDs for stat fetching ───────────────────────────
    player_lookup = {}  # player_name → {id, pos, team}
    if roster_index:
        all_names = {name for props in all_props.values() for name in props}
        for name in all_names:
            info = lookup_player(name, roster_index)
            if info:
                player_lookup[name] = info

    player_ids = {info["id"] for info in player_lookup.values()}
    skater_stats = load_skater_stats(player_ids) if player_ids else {}

    # ── Build rows ────────────────────────────────────────────────────────────
    for eid, props in all_props.items():
        meta = event_meta[eid]
        home = meta["home"]
        away = meta["away"]
        ct   = meta["ct"]
        home_total = meta["home_total"]
        away_total = meta["away_total"]

        home_abbr = _TEAM_MAP_REV.get(home, "")
        away_abbr = _TEAM_MAP_REV.get(away, "")

        for player_name, book_prices in props.items():
            if len(book_prices) < MIN_BOOKS:
                continue

            prices = [v for v in book_prices.values() if v is not None]
            prices = [p for p in prices if p <= MAX_PRICE_INCLUDE]
            if not prices:
                continue

            imps      = [_imp(p) for p in prices]
            true_prob = (sum(imps) / len(imps)) * 100
            best_price = max(prices)
            best_book  = max(book_prices, key=lambda k: book_prices.get(k, -9999))

            # ── Player info ───────────────────────────────────────────────────
            pos         = None
            team_abbr   = None
            goals_pg    = None
            shots_pg    = None
            gp          = None
            opponent_abbrev = None
            player_team_total = None

            info = player_lookup.get(player_name)
            if info:
                pos        = info.get("pos", "")
                team_abbr  = info.get("team", "")
                stats      = skater_stats.get(info["id"])
                if stats:
                    goals_pg = stats.get("goals_pg")
                    shots_pg = stats.get("shots_pg")
                    gp       = stats.get("gp")

                full_team = _TEAM_MAP.get(team_abbr, "")
                player_team_total = team_totals.get(full_team)

                if team_abbr == home_abbr:
                    opponent_abbrev = away_abbr
                elif team_abbr == away_abbr:
                    opponent_abbrev = home_abbr

            if player_team_total is None:
                player_team_total = max(home_total, away_total, 2.5)

            # ── DEF rank for opponent ─────────────────────────────────────────
            def_rank = None
            if opponent_abbrev and def_ranks:
                def_rank = def_ranks.get(opponent_abbrev, {}).get("rank")

            score = composite_score(true_prob, player_team_total, def_rank, goals_pg)

            try:
                gt   = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
                hour = int(gt.strftime("%I"))
                game_time = f"{gt.strftime('%a')} {hour}:{gt.strftime('%M')} {gt.strftime('%p')} ET"
            except Exception:
                game_time = "TBD"

            rows.append({
                "player":          player_name,
                "pos":             pos or "",
                "game":            f"{away} @ {home}",
                "home":            home,
                "away":            away,
                "game_time":       game_time,
                "commence_time":   ct,
                "score":           score,
                "true_prob":       round(true_prob, 1),
                "best_price":      best_price,
                "best_book":       best_book,
                "n_books":         len(book_prices),
                "team_total":      round(player_team_total, 2),
                "home_total":      home_total,
                "away_total":      away_total,
                "def_rank":        def_rank,
                "opponent_abbrev": opponent_abbrev,
                "goals_pg":        goals_pg,
                "shots_pg":        shots_pg,
                "gp":              gp,
            })

    return sorted(rows, key=lambda x: x["score"], reverse=True)
