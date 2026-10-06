"""
NHL Player Props scanner.

Fetches player_goals and player_shots_on_goal for today's NHL games.
Returns rows suitable for display in 25_NHL_Player_Props.py.

Books: FanDuel + BetRivers (only books that post NHL player props on Odds API).
"""
from __future__ import annotations

import json
import ssl as _ssl_compat
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_SSL = _ssl_compat._create_unverified_context()
_NHL_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

SPORT = "icehockey_nhl"
BOOKS = "fanduel,betrivers"

PROP_MARKETS = [
    "player_goals",
    "player_shots_on_goal",
]

MARKET_LABELS = {
    "player_goals":         "Goals",
    "player_shots_on_goal": "Shots on Goal",
}

BOOK_SHORT = {
    "fanduel":   "FD",
    "betrivers": "BR",
}

ALL_BOOKS = ["fanduel", "betrivers"]


def _amer_to_imp(am: float) -> float:
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def _no_vig_prob(over_price: int | None, under_price: int | None) -> float | None:
    """Return no-vig Over probability given both sides from one book."""
    if over_price is None or under_price is None:
        return None
    o = _amer_to_imp(over_price)
    u = _amer_to_imp(under_price)
    total = o + u
    if total <= 0:
        return None
    return round(o / total * 100, 1)


def _get(url: str):
    return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())


def _get_nhl(url: str):
    req = urllib.request.Request(url, headers=_NHL_UA)
    return json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL).read())


def _current_nhl_season() -> str:
    now = datetime.now(timezone.utc)
    if now.month >= 10:
        return f"{now.year}{now.year + 1}"
    return f"{now.year - 1}{now.year}"


def _norm(s: str) -> str:
    return s.lower().strip() if s else ""


def _toi_fmt(seconds: float) -> str:
    """Convert fractional seconds (e.g. 1335.7) to 'MM:SS' display."""
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _fetch_skater_map() -> dict:
    """
    Fetch current-season skater stats from NHL stats REST API.
    Returns {norm_name: {gp, goals_pgp, shots_pgp, avg_toi_s, team_abbrev}}.
    """
    season = _current_nhl_season()
    sort = urllib.parse.quote('[{"property":"goals","direction":"DESC"}]')
    cayenne = urllib.parse.quote(f"gameTypeId=2 and seasonId={season}")
    all_rows: list[dict] = []
    start = 0
    total: int | None = None

    while total is None or start < total:
        url = (
            f"https://api.nhle.com/stats/rest/en/skater/summary"
            f"?isAggregate=false&isGame=false&sort={sort}"
            f"&start={start}&limit=100&cayenneExp={cayenne}"
        )
        try:
            data = _get_nhl(url)
            rows = data.get("data", [])
            total = data.get("total", 0)
            if not rows:
                break
            all_rows.extend(rows)
            start += len(rows)
        except Exception:
            break

    skater_map: dict = {}
    for s in all_rows:
        name = s.get("skaterFullName", "")
        if not name:
            continue
        gp = max(s.get("gamesPlayed") or 1, 1)
        goals = s.get("goals") or 0
        shots = s.get("shots") or 0
        toi_s = s.get("timeOnIcePerGame") or 0.0
        abbrevs = s.get("teamAbbrevs", "")
        # teamAbbrevs can be "COL" or "COL, EDM" if traded
        abbrev = abbrevs.split(",")[0].strip() if abbrevs else ""
        skater_map[_norm(name)] = {
            "gp":          gp,
            "goals_pgp":   round(goals / gp, 2),
            "shots_pgp":   round(shots / gp, 2),
            "avg_toi_s":   toi_s,
            "team_abbrev": abbrev.upper(),
        }
    return skater_map


def _fetch_team_stats() -> tuple[dict, dict]:
    """
    Fetch current standings.
    Returns:
      team_by_norm_name  {norm_full_name: {abbrev, ga_pgp, gp}}
      abbrev_to_norm     {ABBREV: norm_full_name}
    """
    try:
        standings = _get_nhl("https://api-web.nhle.com/v1/standings/now")
    except Exception:
        return {}, {}

    team_by_norm: dict = {}
    abbrev_to_norm: dict = {}
    for t in standings.get("standings", []):
        name   = t.get("teamName", {}).get("default", "")
        abbrev = t.get("teamAbbrev", {}).get("default", "")
        gp     = max(t.get("gamesPlayed") or 1, 1)
        ga     = t.get("goalAgainst") or 0
        if not name or not abbrev:
            continue
        norm = _norm(name)
        team_by_norm[norm] = {
            "abbrev":   abbrev.upper(),
            "ga_pgp":   round(ga / gp, 2),
            "gp":       gp,
        }
        abbrev_to_norm[abbrev.upper()] = norm
    return team_by_norm, abbrev_to_norm


def scan(api_key: str) -> tuple[list[dict], dict]:
    """Returns (rows, debug)."""
    now_utc = datetime.now(timezone.utc)
    debug: dict = {
        "n_events": 0,
        "n_upcoming": 0,
        "market_hits": {m: 0 for m in PROP_MARKETS},
        "outcomes_parsed": 0,
        "errors": [],
    }

    # ---- Fetch context data (team defense + skater stats) ----
    try:
        team_by_norm, abbrev_to_norm = _fetch_team_stats()
    except Exception as exc:
        debug["errors"].append(f"team_stats failed: {exc}")
        team_by_norm, abbrev_to_norm = {}, {}

    try:
        skater_map = _fetch_skater_map()
    except Exception as exc:
        debug["errors"].append(f"skater_stats failed: {exc}")
        skater_map = {}

    debug["n_skaters_loaded"] = len(skater_map)

    # ---- Fetch today's events ----
    try:
        events = _get(
            f"https://api.the-odds-api.com/v4/sports/{SPORT}/events?apiKey={api_key}"
        )
    except Exception as exc:
        debug["errors"].append(f"fetch_events failed: {exc}")
        return [], debug

    debug["n_events"] = len(events)

    upcoming = []
    for e in events:
        try:
            ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
            if (ct - now_utc).total_seconds() > -1800:
                upcoming.append(e)
        except Exception:
            continue

    debug["n_upcoming"] = len(upcoming)
    upcoming.sort(key=lambda e: e["commence_time"])

    if not upcoming:
        return [], debug

    # agg[market_key][event_id][player_name][line] = {"over": {book: price}, "under": {book: price}}
    agg: dict = {}
    game_meta: dict[str, dict] = {}

    for ev in upcoming:
        eid  = ev["id"]
        away = ev.get("away_team", "?")
        home = ev.get("home_team", "?")
        game_meta[eid] = {
            "game":  f"{away} @ {home}",
            "away":  away,
            "home":  home,
            "fp":    ev.get("commence_time", ""),
        }

        for mk in PROP_MARKETS:
            url = (
                f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{eid}/odds"
                f"?apiKey={api_key}&regions=us&markets={mk}"
                f"&bookmakers={BOOKS}&oddsFormat=american"
            )
            try:
                data = _get(url)
            except Exception as exc:
                debug["errors"].append(f"{game_meta[eid]['game']}/{mk}: {exc}")
                continue

            bookmakers = data.get("bookmakers") or []
            if not bookmakers:
                continue
            debug["market_hits"][mk] += 1

            for bm in bookmakers:
                bk = bm.get("key", "")
                if bk not in ALL_BOOKS:
                    continue
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != mk:
                        continue
                    for o in mkt.get("outcomes", []):
                        side   = (o.get("name") or "").strip().lower()
                        player = (o.get("description") or "").strip()
                        point  = o.get("point")
                        price  = o.get("price")
                        if side not in ("over", "under") or not player or point is None or price is None:
                            continue

                        agg.setdefault(mk, {}).setdefault(eid, {}).setdefault(player, {}).setdefault(point, {"over": {}, "under": {}})
                        agg[mk][eid][player][point][side][bk] = int(price)
                        debug["outcomes_parsed"] += 1

    # ---- Build result rows ----
    results = []
    for mk, ev_dict in agg.items():
        for eid, player_dict in ev_dict.items():
            meta = game_meta.get(eid, {})
            away_norm = _norm(meta.get("away", ""))
            home_norm = _norm(meta.get("home", ""))

            # Pre-resolve away/home abbrevs for opponent lookup
            away_abbrev = team_by_norm.get(away_norm, {}).get("abbrev", "")
            home_abbrev = team_by_norm.get(home_norm, {}).get("abbrev", "")

            for player, line_dict in player_dict.items():
                # Look up player context stats
                p_stats = skater_map.get(_norm(player), {})
                player_gp       = p_stats.get("gp")
                goals_pgp       = p_stats.get("goals_pgp")
                shots_pgp       = p_stats.get("shots_pgp")
                avg_toi_s       = p_stats.get("avg_toi_s")
                player_abbrev   = p_stats.get("team_abbrev", "")

                # Determine opposing team's GA/GP
                opp_ga_pgp = None
                if player_abbrev:
                    if player_abbrev == away_abbrev:
                        opp_stats = team_by_norm.get(home_norm, {})
                    elif player_abbrev == home_abbrev:
                        opp_stats = team_by_norm.get(away_norm, {})
                    else:
                        opp_stats = {}
                    opp_ga_pgp = opp_stats.get("ga_pgp")

                for line, sides in line_dict.items():
                    over_prices  = sides.get("over", {})
                    under_prices = sides.get("under", {})

                    nv_prob = None
                    for bk in ALL_BOOKS:
                        op = over_prices.get(bk)
                        up = under_prices.get(bk)
                        nv_prob = _no_vig_prob(op, up)
                        if nv_prob is not None:
                            break

                    best_over  = max(over_prices.values(),  default=None)
                    best_over_book  = next((BOOK_SHORT.get(bk, bk) for bk, p in over_prices.items()  if p == best_over),  None)
                    best_under = max(under_prices.values(), default=None)
                    best_under_book = next((BOOK_SHORT.get(bk, bk) for bk, p in under_prices.items() if p == best_under), None)

                    over_edge = None
                    if nv_prob is not None and best_over is not None:
                        over_edge = round(nv_prob - _amer_to_imp(best_over) * 100, 1)

                    results.append({
                        "player":           player,
                        "market":           MARKET_LABELS[mk],
                        "market_key":       mk,
                        "line":             line,
                        "game":             meta.get("game", "?"),
                        "away_team":        meta.get("away", "?"),
                        "home_team":        meta.get("home", "?"),
                        "first_pitch":      meta.get("fp", ""),
                        "nv_over_pct":      nv_prob,
                        "best_over_price":  best_over,
                        "best_over_book":   best_over_book,
                        "best_under_price": best_under,
                        "best_under_book":  best_under_book,
                        "over_edge":        over_edge,
                        "n_books":          max(len(over_prices), len(under_prices)),
                        # Context stats
                        "player_gp":        player_gp,
                        "goals_pgp":        goals_pgp,
                        "shots_pgp":        shots_pgp,
                        "avg_toi_s":        avg_toi_s,
                        "opp_ga_pgp":       opp_ga_pgp,
                        **{f"over_{BOOK_SHORT.get(bk, bk)}":  over_prices.get(bk)  for bk in ALL_BOOKS},
                        **{f"under_{BOOK_SHORT.get(bk, bk)}": under_prices.get(bk) for bk in ALL_BOOKS},
                    })

    market_order = {"Goals": 0, "Shots on Goal": 1}
    results.sort(key=lambda r: (
        market_order.get(r["market"], 9),
        -(r["nv_over_pct"] or 0),
    ))
    return results, debug
