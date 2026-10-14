"""
TD Matchup Scanner — NFL & CFB

Returns player-level TD matchup scores combining:
  - Odds API: anytime TD scorer true probability + team implied totals
  - Sleeper API: red zone opportunities, snap count, recent form (NFL only)

Score formula (with Sleeper data):
  40% true_prob + 25% team_implied_total + 20% rz_opp_per_game + 15% rz_share

Score formula (odds-only fallback):
  65% true_prob + 35% team_implied_total
"""
import difflib
import json
import os
import ssl
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASTERN = ZoneInfo("America/New_York")

BOOKS = {"draftkings", "fanduel", "bovada", "betrivers", "pinnacle"}
MIN_BOOKS = 2
MAX_PRICE_INCLUDE = 600   # skip massive favorites (implied > 86%)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# Sleeper team abbreviation → Odds API full team name
_TEAM_MAP = {
    "ARI": "Arizona Cardinals",    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",     "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",   "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",       "DEN": "Denver Broncos",
    "DET": "Detroit Lions",        "GB":  "Green Bay Packers",
    "HOU": "Houston Texans",       "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars", "KC":  "Kansas City Chiefs",
    "LV":  "Las Vegas Raiders",    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",     "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",    "NE":  "New England Patriots",
    "NO":  "New Orleans Saints",   "NYG": "New York Giants",
    "NYJ": "New York Jets",        "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",  "SEA": "Seattle Seahawks",
    "SF":  "San Francisco 49ers",  "TB":  "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",     "WAS": "Washington Commanders",
}
_TEAM_MAP_REV = {v: k for k, v in _TEAM_MAP.items()}

# Stadiums that are fully enclosed domes (no weather impact)
_DOMES = {"ATL", "DET", "HOU", "IND", "LV", "MIN", "NO", "TEN"}

# City to fetch weather for each outdoor stadium
_STADIUM_CITY = {
    "ARI": "Glendale+AZ", "BAL": "Baltimore+MD", "BUF": "Orchard+Park+NY",
    "CAR": "Charlotte+NC", "CHI": "Chicago+IL",  "CIN": "Cincinnati+OH",
    "CLE": "Cleveland+OH", "DAL": "Arlington+TX", "DEN": "Denver+CO",
    "GB":  "Green+Bay+WI", "JAX": "Jacksonville+FL", "KC": "Kansas+City+MO",
    "LAC": "Inglewood+CA", "LAR": "Inglewood+CA", "MIA": "Miami+Gardens+FL",
    "NE":  "Foxborough+MA", "NYG": "East+Rutherford+NJ",
    "NYJ": "East+Rutherford+NJ", "PHI": "Philadelphia+PA",
    "PIT": "Pittsburgh+PA",  "SEA": "Seattle+WA",
    "SF":  "Santa+Clara+CA", "TB":  "Tampa+FL", "WAS": "Landover+MD",
}


# ─── HTTP helpers ────────────────────────────────────────────────────────────

def _get(url, timeout=15):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    remaining = resp.headers.get("x-requests-remaining")
    return json.loads(resp.read()), remaining


import urllib.request


def _imp(american):
    """American odds → raw implied probability."""
    if american is None:
        return None
    if american >= 0:
        return 100 / (american + 100)
    return -american / (-american + 100)


# ─── ODDS API ────────────────────────────────────────────────────────────────

def fetch_events(api_key, sport="americanfootball_nfl"):
    try:
        data, _ = _get(
            f"https://api.the-odds-api.com/v4/sports/{sport}/events"
            f"?apiKey={api_key}"
        )
        return data
    except Exception:
        return []


def fetch_team_totals(api_key, sport="americanfootball_nfl"):
    """Returns {full_team_name: implied_total} from the team_totals market."""
    try:
        data, _ = _get(
            f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
            f"?apiKey={api_key}&regions=us&markets=team_totals&oddsFormat=american"
        )
    except Exception:
        return {}

    totals = {}
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
    return totals


def fetch_td_props(api_key, event_id, sport="americanfootball_nfl"):
    """Returns {player_name: {book_key: american_price}} for anytime TD scorer."""
    try:
        data, _ = _get(
            f"https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds"
            f"?apiKey={api_key}&regions=us&markets=player_td_scorer&oddsFormat=american"
        )
    except Exception:
        return {}

    players = defaultdict(dict)
    for bm in data.get("bookmakers", []):
        bk = bm["key"]
        if bk not in BOOKS:
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "player_td_scorer":
                continue
            for out in mkt.get("outcomes", []):
                name  = out.get("name", "")
                price = out.get("price")
                if name and price is not None:
                    players[name][bk] = price
    return dict(players)


# ─── SLEEPER API ─────────────────────────────────────────────────────────────

_SLEEPER_PLAYERS = None   # module-level cache


def _sleeper_get(url, timeout=12):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    return json.loads(resp.read())


def sleeper_state():
    try:
        return _sleeper_get("https://api.sleeper.app/v1/state/nfl")
    except Exception:
        return {}


def sleeper_week_stats(week, year):
    try:
        return _sleeper_get(
            f"https://api.sleeper.app/v1/stats/nfl/regular/{year}/{week}"
        )
    except Exception:
        return {}


def sleeper_players():
    global _SLEEPER_PLAYERS
    if _SLEEPER_PLAYERS is not None:
        return _SLEEPER_PLAYERS

    cache_path = os.path.join(ROOT, "scripts", "_sleeper_players_cache.json")
    if os.path.exists(cache_path):
        try:
            age = (datetime.now().timestamp() - os.path.getmtime(cache_path)) / 86400
            if age < 7:
                with open(cache_path, encoding="utf-8") as f:
                    _SLEEPER_PLAYERS = json.load(f)
                return _SLEEPER_PLAYERS
        except Exception:
            pass

    try:
        players = _sleeper_get("https://api.sleeper.app/v1/players/nfl")
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(players, f)
        _SLEEPER_PLAYERS = players
        return players
    except Exception:
        return {}


def build_name_index(players):
    """Returns {normalized_name: (player_id, position, team_abbrev)}."""
    idx = {}
    for pid, p in players.items():
        pos  = p.get("position", "")
        team = p.get("team", "")
        fn   = (p.get("full_name") or "").lower()
        if fn:
            idx[fn] = (pid, pos, team)
        # "J. Smith" format
        first = p.get("first_name", "")
        last  = p.get("last_name",  "")
        if first and last:
            abbr = f"{first[0].lower()}. {last.lower()}"
            idx.setdefault(abbr, (pid, pos, team))
    return idx


def lookup_player(name, idx):
    n = name.lower()
    if n in idx:
        return idx[n]
    hits = difflib.get_close_matches(n, idx.keys(), n=1, cutoff=0.82)
    if hits:
        return idx[hits[0]]
    return None, None, None


# ─── WEATHER ─────────────────────────────────────────────────────────────────

def fetch_weather(team_abbrev):
    """Returns (description, wind_mph) or (None, None)."""
    if team_abbrev in _DOMES:
        return "🏟 Dome", 0
    city = _STADIUM_CITY.get(team_abbrev)
    if not city:
        return None, None
    try:
        url = f"https://wttr.in/{city}?format=j1"
        data = _sleeper_get(url, timeout=8)
        cur  = data["current_condition"][0]
        desc_code = int(cur.get("weatherCode", 0))
        wind_mph  = int(cur.get("windspeedMiles", 0))
        temp_f    = int(cur.get("temp_F", 70))
        # Build label
        if desc_code in (200, 201, 202, 210, 211, 212, 221, 230, 231, 232):
            icon = "⛈"
        elif desc_code in (300, 301, 302, 310, 311, 312, 313, 314, 321,
                           500, 501, 502, 503, 504, 511, 520, 521, 522, 531):
            icon = "🌧"
        elif desc_code in (600, 601, 602, 611, 612, 613, 615, 616, 620, 621, 622):
            icon = "❄"
        elif desc_code in (701, 711, 721, 731, 741, 751, 761, 762, 771, 781):
            icon = "🌫"
        elif desc_code == 800:
            icon = "☀"
        else:
            icon = "⛅"
        wx = f"{icon} {temp_f}°F"
        if wind_mph >= 20:
            wx += f" 💨{wind_mph}mph"
        return wx, wind_mph
    except Exception:
        return None, None


# ─── SCORING ─────────────────────────────────────────────────────────────────

def composite_score(true_prob, team_total, rz_opp=None, rz_share=None):
    """Returns 0-100 composite score."""
    tp = min(true_prob / 100, 1.0)
    tt = min((team_total or 21) / 35, 1.0)

    if rz_opp is not None:
        rz = min((rz_opp or 0) / 6.0, 1.0)
        rs = min((rz_share or 0) / 70.0, 1.0)
        raw = 0.40 * tp + 0.25 * tt + 0.20 * rz + 0.15 * rs
    else:
        raw = 0.65 * tp + 0.35 * tt

    return round(raw * 100)


# ─── MAIN SCAN ───────────────────────────────────────────────────────────────

def scan(api_key, sport="nfl", fetch_weather_data=True):
    """
    Returns list of player dicts sorted by TD score descending.
    sport: "nfl" or "ncaaf"
    """
    sport_key = f"americanfootball_{sport}"

    events = fetch_events(api_key, sport_key)
    if not events:
        return []

    team_totals = fetch_team_totals(api_key, sport_key)

    # ── Sleeper (NFL only) ───────────────────────────────────────────────────
    pid_stats = {}   # pid → averaged stat dict
    name_idx  = {}   # normalized name → (pid, pos, team_abbrev)
    if sport == "nfl":
        state  = sleeper_state()
        week   = state.get("week", 1)
        season = int(state.get("season", datetime.now(tz=EASTERN).year))
        if state.get("season_type") == "pre":
            week = 1

        for w in range(max(1, week - 2), week + 1):
            wstats = sleeper_week_stats(w, season)
            for pid, s in wstats.items():
                if pid not in pid_stats:
                    pid_stats[pid] = []
                pid_stats[pid].append(s)

        players  = sleeper_players()
        name_idx = build_name_index(players)

    rows = []

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

        td_props = fetch_td_props(api_key, eid, sport_key)
        if not td_props:
            continue

        # Weather for home team's stadium
        wx_label = wx_wind = None
        if fetch_weather_data:
            home_abbr = _TEAM_MAP_REV.get(home, "")
            if home_abbr:
                wx_label, wx_wind = fetch_weather(home_abbr)

        home_total = team_totals.get(home, 0)
        away_total = team_totals.get(away, 0)

        for player_name, book_prices in td_props.items():
            if len(book_prices) < MIN_BOOKS:
                continue

            prices = [v for v in book_prices.values() if v is not None]
            # Filter out huge longshots (max price cap is more conservative)
            prices = [p for p in prices if p <= MAX_PRICE_INCLUDE]
            if not prices:
                continue

            imps = [_imp(p) for p in prices]
            true_prob = (sum(imps) / len(imps)) * 100

            best_price = max(prices)
            best_book  = max(book_prices, key=lambda k: book_prices.get(k, -9999))

            # ── Sleeper lookup ───────────────────────────────────────────────
            pos = rz_opp = rz_share = snap_avg = None
            form = [0, 0, 0]
            player_team_total = None

            if name_idx:
                pid, pos, team_abbr = lookup_player(player_name, name_idx)
                if pid and pid in pid_stats:
                    weeks = pid_stats[pid]

                    def _avg(key):
                        vals = [w.get(key) or 0 for w in weeks]
                        return sum(vals) / len(vals) if vals else None

                    rz_tgt  = _avg("rec_tgt_rz")   # receiving RZ targets
                    rz_rush = _avg("rush_rz_att")   # rushing RZ attempts
                    rz_total = (rz_tgt or 0) + (rz_rush or 0)
                    if rz_total > 0:
                        rz_opp = rz_total

                    snap_avg = _avg("off_snp")

                    # Form: TD scored each of last 3 weeks (in order)
                    form_raw = []
                    for w in weeks[-3:]:
                        tds = ((w.get("rec_td") or 0) +
                               (w.get("rush_td") or 0) +
                               (w.get("pass_td") or 0))
                        form_raw.append(1 if tds > 0 else 0)
                    while len(form_raw) < 3:
                        form_raw.insert(0, 0)
                    form = form_raw

                    # Team implied total via Sleeper team mapping
                    full_team = _TEAM_MAP.get(team_abbr, "")
                    if full_team:
                        player_team_total = team_totals.get(full_team)

            # Best available team total
            if player_team_total is None:
                player_team_total = max(home_total, away_total, 21)

            score = composite_score(true_prob, player_team_total, rz_opp, rz_share)

            try:
                gt   = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
                hour = int(gt.strftime("%I"))
                ampm = gt.strftime("%p")
                game_time = f"{gt.strftime('%a')} {hour}:{gt.strftime('%M')} {ampm} ET"
            except Exception:
                game_time = "TBD"

            rows.append({
                "player":        player_name,
                "league":        sport.upper() if sport != "ncaaf" else "CFB",
                "pos":           pos or "",
                "game":          f"{away} @ {home}",
                "home":          home,
                "away":          away,
                "game_time":     game_time,
                "commence_time": ct,
                "score":         score,
                "true_prob":     round(true_prob, 1),
                "best_price":    best_price,
                "best_book":     best_book,
                "n_books":       len(book_prices),
                "team_total":    player_team_total or 0,
                "home_total":    home_total,
                "away_total":    away_total,
                "rz_opp":        round(rz_opp, 1) if rz_opp else None,
                "snap_avg":      round(snap_avg) if snap_avg else None,
                "form":          form,
                "weather":       wx_label or ("🏟 Dome" if sport == "nfl" else ""),
                "wind_mph":      wx_wind or 0,
            })

    return sorted(rows, key=lambda x: x["score"], reverse=True)
