"""
BallDontLie MLB API client  (mlb.balldontlie.io).

Authentication: Authorization: <api_key> header.
All IDs are BDL-internal (not MLBAM). Name-based player lookup is cached
in-process to avoid repeated /players search calls.
"""

import urllib.request
import ssl
# System cert chain is broken on this Windows install — use unverified
# context for these public-data APIs. No auth/PII flows through these endpoints.
_UNVERIFIED_SSL = ssl._create_unverified_context()
import urllib.error
import urllib.parse
import json
import re
import time
import unicodedata

BASE = "https://api.balldontlie.io/mlb/v1"

_api_key: str | None = None


def set_api_key(key: str):
    global _api_key
    _api_key = key.strip()


def is_configured() -> bool:
    return bool(_api_key)


# ---------- HTTP helpers ----------

def _headers() -> dict:
    if not _api_key:
        raise ValueError("BallDontLie API key not set. Call set_api_key() first.")
    return {"Authorization": _api_key, "User-Agent": "mlb-dashboard/1.0"}


def _build_url(endpoint: str, params: dict | None) -> str:
    url = f"{BASE}/{endpoint.lstrip('/')}"
    if params:
        # Expand list values into repeated keys: {'ids[]': [1,2]} → ids[]=1&ids[]=2
        pairs = []
        for k, vs in params.items():
            for v in (vs if isinstance(vs, list) else [vs]):
                pairs.append((k, v))
        url += "?" + urllib.parse.urlencode(pairs)
    return url


def _fetch(endpoint: str, params: dict | None = None, _retries: int = 3) -> dict:
    url = _build_url(endpoint, params)
    for attempt in range(_retries):
        try:
            req = urllib.request.Request(url, headers=_headers())
            with urllib.request.urlopen(req, timeout=20, context=_UNVERIFIED_SSL) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()[:300]
            except Exception:
                body = ""
            if e.code == 429:
                # Rate-limited: back off and retry
                wait = 2 ** attempt  # 1s, 2s, 4s
                time.sleep(wait)
                continue
            raise ValueError(f"BallDontLie HTTP {e.code}: {body}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return {}
    return {}  # exhausted retries


def _fetch_all(endpoint: str, params: dict | None = None, max_pages: int = 10) -> list:
    """Auto-paginate (cursor-based). Stops after max_pages to guard against runaway."""
    params = dict(params or {})
    params.setdefault("per_page", 100)
    results = []
    cursor = None
    for _ in range(max_pages):
        if cursor is not None:
            params["cursor"] = cursor
        data = _fetch(endpoint, params)
        items = data.get("data", [])
        results.extend(items)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor or not items:
            break
    return results


# ---------- Name normalisation ----------

_punct = re.compile(r"[.\-']")
_suffix = re.compile(r"\s+(jr|sr|ii|iii|iv)\.?$", re.IGNORECASE)


def _norm(name: str) -> str:
    n = name.strip()
    if "," in n:
        last, _, first = n.partition(",")
        n = f"{first.strip()} {last.strip()}"
    n = "".join(c for c in unicodedata.normalize("NFKD", n) if not unicodedata.combining(c))
    n = _punct.sub("", n)
    n = _suffix.sub("", n)
    return re.sub(r"\s+", " ", n).strip().lower()


# ---------- Player-ID / name caches ----------

_id_cache: dict[str, int | None] = {}
_name_cache: dict[int, str | None] = {}


def get_player_name(bdl_id: int) -> str | None:
    """Returns the full name for a BDL player ID. Caches per process."""
    if bdl_id in _name_cache:
        return _name_cache[bdl_id]
    try:
        data = _fetch(f"players/{bdl_id}")
        player = data.get("data", {})
        name = player.get("full_name") or player.get("display_name")
    except (ValueError, TypeError):
        name = None
    _name_cache[bdl_id] = name
    return name


def get_player_names_batch(bdl_ids: list[int]) -> dict[int, str]:
    """
    Fetches names for a list of player IDs efficiently by batching them into
    multi-player /stats calls (which embed player.full_name in each row).
    Tries current season first, then prior season for rookies/new players.
    Falls back to individual /players/{id} for any remaining cache misses.
    Returns dict mapping id → full_name (IDs with no result are omitted).
    """
    from datetime import date as _date
    current_season = _date.today().year

    result: dict[int, str] = {}
    missing = []
    for pid in dict.fromkeys(bdl_ids):
        if pid in _name_cache and _name_cache[pid]:
            result[pid] = _name_cache[pid]
        else:
            missing.append(pid)

    def _fetch_names_via_stats(ids: list[int], season: int) -> list[int]:
        """Fetch names via stats, return list of still-missing IDs."""
        still_missing = []
        BATCH = 30
        for i in range(0, len(ids), BATCH):
            chunk = ids[i : i + BATCH]
            params: dict = {"player_ids[]": chunk, "seasons[]": [season], "per_page": BATCH}
            try:
                rows = _fetch_all("stats", params, max_pages=1)
            except ValueError:
                still_missing.extend(chunk)
                continue
            found: set[int] = set()
            for row in rows:
                p = row.get("player") or {}
                pid = p.get("id")
                if pid and pid not in found:
                    found.add(pid)
                    name = p.get("full_name") or (
                        f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or None
                    )
                    if name:
                        result[pid] = name
                        _name_cache[pid] = name
            still_missing.extend(pid for pid in chunk if pid not in found)
        return still_missing

    # Pass 1: current season stats
    missing = _fetch_names_via_stats(missing, current_season)
    # Pass 2: prior season stats (catches players new to current year)
    if missing:
        missing = _fetch_names_via_stats(missing, current_season - 1)
    # Pass 3: individual /players/{id} for anything still missing
    for pid in missing:
        name = get_player_name(pid)
        if name:
            result[pid] = name
    return result


def get_player_id(player_name: str) -> int | None:
    """
    Returns the BallDontLie player ID for a given name.
    Searches BDL /players with the name, picks exact-normalised match first,
    then first result. Caches per process.
    """
    key = _norm(player_name)
    if key in _id_cache:
        return _id_cache[key]

    try:
        data = _fetch("players", {"search": player_name, "per_page": 10})
    except ValueError:
        _id_cache[key] = None
        return None

    players = data.get("data", [])
    if not players:
        _id_cache[key] = None
        return None

    for p in players:
        if _norm(p.get("full_name", "")) == key:
            _id_cache[key] = p["id"]
            return p["id"]

    # Best partial match: last name equals
    target_last = key.rsplit(" ", 1)[-1]
    for p in players:
        if _norm(p.get("full_name", "")).rsplit(" ", 1)[-1] == target_last:
            _id_cache[key] = p["id"]
            return p["id"]

    # First result
    _id_cache[key] = players[0]["id"]
    return players[0]["id"]


# ---------- Per-game stats → hit-rate summary ----------

def get_batter_game_log_rates(player_name: str, season: int) -> dict:
    """
    Returns per-game hit / total-base rates for a player's season using
    BallDontLie /stats (paginated, pulls all regular-season games).

    Returns dict:
      games, hits_1plus_rate, hits_2plus_rate,
      tb_1plus_rate, tb_2plus_rate, tb_3plus_rate
    Returns {} if player not found or no data.
    """
    bdl_id = get_player_id(player_name)
    if not bdl_id:
        return {}

    try:
        game_stats = _fetch_all(
            "stats",
            {"player_ids[]": [bdl_id], "seasons[]": [season]},
        )
    except ValueError:
        return {}

    games = 0
    hits_1, hits_2 = 0, 0
    tb_1, tb_2, tb_3 = 0, 0, 0
    hrr_1, hrr_2, hrr_3 = 0, 0, 0

    for s in game_stats:
        pa = s.get("plate_appearances") or 0
        if pa == 0:
            continue
        games += 1
        h   = s.get("hits")        or 0
        tb  = s.get("total_bases") or 0
        r   = s.get("runs")        or 0
        rbi = s.get("rbi")         or 0
        hrr = h + r + rbi
        if h   >= 1: hits_1 += 1
        if h   >= 2: hits_2 += 1
        if tb  >= 1: tb_1 += 1
        if tb  >= 2: tb_2 += 1
        if tb  >= 3: tb_3 += 1
        if hrr >= 1: hrr_1 += 1
        if hrr >= 2: hrr_2 += 1
        if hrr >= 3: hrr_3 += 1

    if games == 0:
        return {}
    return {
        "games":           games,
        "hits_1plus_rate": hits_1 / games,
        "hits_2plus_rate": hits_2 / games,
        "tb_1plus_rate":   tb_1  / games,
        "tb_2plus_rate":   tb_2  / games,
        "tb_3plus_rate":   tb_3  / games,
        "hrr_1plus_rate":  hrr_1 / games,
        "hrr_2plus_rate":  hrr_2 / games,
        "hrr_3plus_rate":  hrr_3 / games,
        "source":          "balldontlie",
    }


def get_batter_game_log_rates_by_id(bdl_id: int, season: int) -> dict:
    """
    Same as get_batter_game_log_rates but takes a BDL player ID directly,
    skipping the name-search step entirely. Preferred when player_id is
    already known (e.g. from player props).
    Also returns 'player_name' extracted from the stats record so no extra
    /players/{id} call is needed.
    """
    if not bdl_id:
        return {}
    try:
        game_stats = _fetch_all(
            "stats",
            {"player_ids[]": [bdl_id], "seasons[]": [season]},
        )
    except ValueError:
        return {}

    games = 0
    hits_1, hits_2 = 0, 0
    tb_1, tb_2, tb_3 = 0, 0, 0
    hrr_1, hrr_2, hrr_3 = 0, 0, 0
    player_name: str | None = None

    for s in game_stats:
        if player_name is None:
            p = s.get("player") or {}
            player_name = p.get("full_name") or (
                f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or None
            )
            if player_name and bdl_id not in _name_cache:
                _name_cache[bdl_id] = player_name  # warm cache for batch lookups
        pa = s.get("plate_appearances") or 0
        if pa == 0:
            continue
        games += 1
        h   = s.get("hits")        or 0
        tb  = s.get("total_bases") or 0
        r   = s.get("runs")        or 0
        rbi = s.get("rbi")         or 0
        hrr = h + r + rbi
        if h   >= 1: hits_1 += 1
        if h   >= 2: hits_2 += 1
        if tb  >= 1: tb_1 += 1
        if tb  >= 2: tb_2 += 1
        if tb  >= 3: tb_3 += 1
        if hrr >= 1: hrr_1 += 1
        if hrr >= 2: hrr_2 += 1
        if hrr >= 3: hrr_3 += 1

    if games == 0:
        return {}
    return {
        "games":           games,
        "player_name":     player_name,
        "hits_1plus_rate": hits_1 / games,
        "hits_2plus_rate": hits_2 / games,
        "tb_1plus_rate":   tb_1  / games,
        "tb_2plus_rate":   tb_2  / games,
        "tb_3plus_rate":   tb_3  / games,
        "hrr_1plus_rate":  hrr_1 / games,
        "hrr_2plus_rate":  hrr_2 / games,
        "hrr_3plus_rate":  hrr_3 / games,
        "source":          "balldontlie",
    }


# ---------- Season aggregate stats ----------

def get_season_stats(player_name: str, season: int) -> dict:
    """
    Returns season batting aggregates: avg, hr, rbi, obp, slg, ops, etc.
    Returns {} if not found.
    """
    bdl_id = get_player_id(player_name)
    if not bdl_id:
        return {}
    try:
        data = _fetch("season_stats", {"player_ids[]": [bdl_id], "season": season})
    except ValueError:
        return {}
    rows = data.get("data", [])
    if not rows:
        return {}
    return rows[0]


# ---------- Injuries ----------

def get_injuries() -> list:
    """Returns all current player injuries from BallDontLie."""
    try:
        return _fetch_all("player_injuries")
    except ValueError:
        return []


# ---------- Games ----------

def get_games_for_date(date_str: str) -> list:
    """Returns BDL game objects for a YYYY-MM-DD date."""
    try:
        return _fetch_all("games", {"dates[]": [date_str]})
    except ValueError:
        return []


def get_game_ids_for_date(date_str: str) -> dict:
    """
    Returns dict mapping (away_abbr, home_abbr) → bdl_game_id for a date.
    Useful for joining MLB API game data to BDL odds/props.
    Also maps (away_name, home_name) for broader matching.
    """
    games = get_games_for_date(date_str)
    out = {}
    for g in games:
        gid = g["id"]
        away = g.get("away_team", {})
        home = g.get("home_team", {})
        # keyed by abbr pair (ARI, NYY) and by full name pair
        out[(away.get("abbreviation", ""), home.get("abbreviation", ""))] = gid
        out[(g.get("away_team_name", "").lower(), g.get("home_team_name", "").lower())] = gid
    return out


# ---------- Odds ----------

BDL_VENDORS = ["draftkings", "fanduel", "betmgm", "caesars", "betrivers", "fanatics"]


def get_game_odds(game_id: int) -> list:
    """
    Returns moneyline / spread / total odds for a BDL game_id.
    One row per (vendor, game). Fields: vendor, moneyline_home_odds,
    moneyline_away_odds, spread_home_value, spread_home_odds,
    spread_away_value, spread_away_odds, total_value,
    total_over_odds, total_under_odds, updated_at.
    """
    try:
        data = _fetch("odds", {"game_ids[]": [game_id]})
        return data.get("data", [])
    except ValueError:
        return []


def get_odds_for_date(date_str: str) -> dict:
    """
    Returns dict mapping bdl_game_id → list of odds rows for all games on date.
    Fetches odds for each game sequentially (one request per game).
    """
    games = get_games_for_date(date_str)
    out: dict[int, list] = {}
    for g in games:
        gid = g["id"]
        rows = get_game_odds(gid)
        if rows:
            out[gid] = rows
    return out


def get_best_moneyline(game_id: int) -> dict:
    """
    Returns best (lowest vig / highest implied) moneyline for both sides
    across all BDL vendors.

    Returns: {
      away_best_odds, away_best_vendor,
      home_best_odds, home_best_vendor,
      away_implied, home_implied,
      all_rows,
    }
    """
    rows = get_game_odds(game_id)
    if not rows:
        return {}

    best_away = best_home = None
    best_away_vendor = best_home_vendor = ""
    for r in rows:
        ao = r.get("moneyline_away_odds")
        ho = r.get("moneyline_home_odds")
        if ao is not None and (best_away is None or ao > best_away):
            best_away = ao
            best_away_vendor = r.get("vendor", "")
        if ho is not None and (best_home is None or ho > best_home):
            best_home = ho
            best_home_vendor = r.get("vendor", "")

    from data.odds import american_to_implied
    return {
        "away_best_odds":   best_away,
        "away_best_vendor": best_away_vendor,
        "home_best_odds":   best_home,
        "home_best_vendor": best_home_vendor,
        "away_implied":     american_to_implied(best_away) if best_away is not None else None,
        "home_implied":     american_to_implied(best_home) if best_home is not None else None,
        "all_rows":         rows,
    }


# ---------- Player props ----------

def get_player_props(game_id: int) -> list:
    """
    Returns player props for a BDL game_id.
    Available pre-game; returns [] for completed games.
    """
    try:
        data = _fetch("odds/player_props", {"game_id": game_id})
        return data.get("data", [])
    except ValueError:
        return []


def get_player_props_for_date(date_str: str) -> dict:
    """
    Returns dict mapping bdl_game_id → list of player prop rows for all
    games on date. Skips games that return no props (e.g. already started).
    """
    games = get_games_for_date(date_str)
    out: dict[int, list] = {}
    for g in games:
        gid = g["id"]
        props = get_player_props(gid)
        if props:
            out[gid] = props
    return out


# ---------- Standings ----------

def get_standings(season: int) -> list:
    """Returns team standings for a season."""
    try:
        data = _fetch("standings", {"season": season})
        return data.get("data", [])
    except ValueError:
        return []


if __name__ == "__main__":
    import os, sys
    key = os.environ.get("BALLDONTLIE_API_KEY")
    if not key:
        print("Set BALLDONTLIE_API_KEY env var to test.")
        sys.exit(1)
    set_api_key(key)

    print("=== Injuries ===")
    inj = get_injuries()
    print(f"  {len(inj)} active injuries")
    if inj:
        i = inj[0]
        print(f"  sample: {i.get('player', {}).get('full_name')} — {i.get('type')} ({i.get('status')})")

    print("\n=== Game log rates — Aaron Judge 2025 ===")
    rates = get_batter_game_log_rates("Aaron Judge", 2025)
    if rates:
        print(f"  games={rates['games']}")
        print(f"  hits 1+: {rates['hits_1plus_rate']*100:.1f}%")
        print(f"  total bases 1+: {rates['tb_1plus_rate']*100:.1f}%")
        print(f"  total bases 2+: {rates['tb_2plus_rate']*100:.1f}%")
    else:
        print("  no data")

    print("\n=== Today's games ===")
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    games = get_games_for_date(today)
    print(f"  {len(games)} games on {today}")
    if games:
        g = games[0]
        print(f"  {g.get('away_team_name')} @ {g.get('home_team_name')} — {g.get('status')}")
