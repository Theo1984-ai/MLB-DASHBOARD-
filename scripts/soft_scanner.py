"""
Shared MLB Soft-Price Scanner module.

Extracted from pages/3_Soft_Scanner.py — produces the same picks that
the page would show with default filters (min_edge_pp=5, min_books=3,
no implied minimum, price band -300 to +300).

Each pick includes the metadata needed for next-day settlement by
scripts/true_prob_settler.py (away_team, home_team, first_pitch,
stat_key, side, point).
"""
from __future__ import annotations

import json
import ssl as _ssl
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

_SSL = _ssl._create_unverified_context()

SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada"

# Default filter — matches page's default-slider settings.
DEFAULT_MIN_EDGE_PP = 5.0
DEFAULT_MIN_BOOKS = 3
DEFAULT_MIN_IMPLIED_PCT = 0
MAX_PRICE_CAP = 300
MIN_PRICE_CAP = -300

# How many picks to save in the snapshot (top by edge_pp).
DEFAULT_TOP_N = 30

MARKET_GROUPS = {
    "HR":    ["batter_home_runs", "batter_home_runs_alternate"],
    "H+R+R": ["batter_hits_runs_rbis_alternate"],
    "Hits":  ["batter_hits", "batter_hits_alternate"],
    "TB":    ["batter_total_bases", "batter_total_bases_alternate"],
}
MARKET_LABELS = {
    "batter_home_runs":                "HR",
    "batter_home_runs_alternate":      "HR",
    "batter_hits_runs_rbis_alternate": "H+R+R",
    "batter_hits":                     "Hits",
    "batter_hits_alternate":           "Hits",
    "batter_total_bases":              "TB",
    "batter_total_bases_alternate":    "TB",
}
# stat_key drives next-day settlement (see scripts/true_prob_settler.py).
STAT_KEY = {
    "batter_home_runs":                "hr",
    "batter_home_runs_alternate":      "hr",
    "batter_hits":                     "hits",
    "batter_hits_alternate":           "hits",
    "batter_total_bases":              "tb",
    "batter_total_bases_alternate":    "tb",
    "batter_hits_runs_rbis_alternate": "hrr",
}


def _amer_to_imp(am):
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def _fetch_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={api_key}"
    return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())


def _fetch_event_odds(api_key, event_id, markets_tuple):
    markets = ",".join(markets_tuple)
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={markets}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except urllib.error.HTTPError:
        return {}
    except Exception:
        return {}


def _collect_offers(bookmakers, market_filter):
    """Returns {(market_key, player, side, point): [(book, american)]}."""
    offers = defaultdict(list)
    for b in bookmakers:
        for m in b.get("markets", []):
            mk = m["key"]
            if mk not in market_filter:
                continue
            for o in m.get("outcomes", []):
                name = (o.get("name") or "").strip()
                player = o.get("description") or name
                if player in ("Over", "Under"):
                    continue
                side = name if name in ("Over", "Under") else "Yes"
                pt = o.get("point")
                price = o.get("price")
                if price is None:
                    continue
                offers[(mk, player, side, pt)].append((b["key"], int(price)))
    return offers


def _build_team_lookup_for_game(away_team, home_team):
    """Returns {normalized_player_name: team_abbrev} for both teams in a game.
    Uses MLB Stats API per-team roster (one call per team, cached for the day)."""
    if not hasattr(_build_team_lookup_for_game, "_cache"):
        _build_team_lookup_for_game._cache = {}
    cache = _build_team_lookup_for_game._cache
    cache_key = (away_team, home_team)
    if cache_key in cache:
        return cache[cache_key]

    from datetime import datetime
    season = datetime.now().year
    lookup = {}

    try:
        # Resolve team names → IDs (one schedule call has them)
        url = (f"https://statsapi.mlb.com/api/v1/teams?season={season}&sportId=1")
        teams = json.loads(urllib.request.urlopen(url, timeout=10, context=_SSL).read())
        team_map = {}  # name → (id, abbr)
        for t in teams.get("teams", []):
            team_map[t.get("name")] = (t.get("id"), t.get("abbreviation"))

        for team_name in (away_team, home_team):
            entry = team_map.get(team_name)
            if not entry:
                continue
            tid, abbr = entry
            try:
                r = json.loads(urllib.request.urlopen(
                    f"https://statsapi.mlb.com/api/v1/teams/{tid}/roster?season={season}",
                    timeout=10, context=_SSL).read())
                for r_entry in r.get("roster", []):
                    name = r_entry.get("person", {}).get("fullName", "")
                    if not name: continue
                    # normalize: lowercase, strip punctuation
                    norm = name.lower().replace(",", "").replace(".", "").strip()
                    lookup[norm] = abbr
            except Exception:
                continue
    except Exception:
        pass

    cache[cache_key] = lookup
    return lookup


def _resolve_team(player, team_lookup):
    """Find team abbreviation for a player. Handles 'Last, First' style too."""
    if not player or not team_lookup: return None
    n = player.lower().replace(",", "").replace(".", "").strip()
    if n in team_lookup: return team_lookup[n]
    # Try last-name partial
    parts = n.split()
    if not parts: return None
    last = parts[-1]
    matches = [v for k, v in team_lookup.items() if k.split() and k.split()[-1] == last]
    if len(matches) == 1:
        return matches[0]
    return None


def _find_soft_picks(offers_dict, event_meta,
                     min_edge_pp, min_books, min_implied_pct):
    team_lookup = _build_team_lookup_for_game(
        event_meta.get("away_team"), event_meta.get("home_team"))
    rows = []
    for (mk, player, side, pt), book_prices in offers_dict.items():
        if len(book_prices) < min_books:
            continue
        prices = [pr for _, pr in book_prices]
        best_book, best_price = max(book_prices, key=lambda x: x[1])

        # Hard price cap (matches page)
        if best_price > MAX_PRICE_CAP or best_price < MIN_PRICE_CAP:
            continue

        best_imp = _amer_to_imp(best_price)
        worst_imp = _amer_to_imp(min(prices))
        edge_pp = (worst_imp - best_imp) * 100
        if edge_pp < min_edge_pp:
            continue

        consensus_imp = sum(_amer_to_imp(pr) for pr in prices) / len(prices)
        if consensus_imp * 100 < min_implied_pct:
            continue

        if best_price > 0:
            ev_per_100 = consensus_imp * best_price - (1 - consensus_imp) * 100
        else:
            ev_per_100 = consensus_imp * 100 - (1 - consensus_imp) * abs(best_price)

        team_abbr = _resolve_team(player, team_lookup)
        rows.append({
            # Settlement metadata (matches true_prob_settler.py expectations)
            "event_id":    event_meta["event_id"],
            "away_team":   event_meta["away_team"],
            "home_team":   event_meta["home_team"],
            "first_pitch": event_meta["first_pitch"],
            "stat_key":    STAT_KEY.get(mk),
            "market_key":  mk,
            # Display fields
            "game":          event_meta["game_label"],
            "market":        MARKET_LABELS.get(mk, mk),
            "player":        player,
            "team":          team_abbr,  # team abbreviation (e.g. 'KCR') or None
            "selection":     player,
            "side":          side,
            "point":         pt,
            "best_book":     best_book,
            "best_price":    best_price,
            "best_imp_pct":  round(best_imp * 100, 2),
            "consensus_pct": round(consensus_imp * 100, 2),
            "edge_pp":       round(edge_pp, 2),
            "ev_per_100":    round(ev_per_100, 2),
            "n_books":       len(book_prices),
            "all_prices":    sorted(book_prices, key=lambda x: -x[1]),
        })
    return rows


def scan(api_key,
         min_edge_pp=DEFAULT_MIN_EDGE_PP,
         min_books=DEFAULT_MIN_BOOKS,
         min_implied_pct=DEFAULT_MIN_IMPLIED_PCT,
         top_n=DEFAULT_TOP_N,
         markets=None):
    """Run the soft-scanner sweep across all upcoming MLB games.

    Returns a list of pick dicts sorted by edge_pp desc, capped at top_n.
    """
    if markets is None:
        markets = list(MARKET_GROUPS.keys())
    markets_to_pull = []
    for g in markets:
        if g in MARKET_GROUPS:
            markets_to_pull.extend(MARKET_GROUPS[g])
    market_filter = set(markets_to_pull)
    markets_tuple = tuple(sorted(market_filter))

    events = _fetch_events(api_key)
    now_utc = datetime.now(timezone.utc)
    upcoming = []
    for e in events:
        ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        if (ct - now_utc).total_seconds() > -1800:
            upcoming.append((e, ct))
    upcoming.sort(key=lambda x: x[1])

    all_picks = []
    for e, ct in upcoming:
        meta = {
            "event_id":    e["id"],
            "away_team":   e["away_team"],
            "home_team":   e["home_team"],
            "first_pitch": e["commence_time"],
            "game_label":  f"{e['away_team'].split()[-1]} @ {e['home_team'].split()[-1]}",
        }
        data = _fetch_event_odds(api_key, e["id"], markets_tuple)
        offers = _collect_offers(data.get("bookmakers", []), market_filter)
        all_picks.extend(_find_soft_picks(offers, meta,
                                          min_edge_pp, min_books, min_implied_pct))

    # Sort by largest edge, tiebreak by EV
    all_picks.sort(key=lambda r: (-r["edge_pp"], -r["ev_per_100"]))
    return all_picks[:top_n]
