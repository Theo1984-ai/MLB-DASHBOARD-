"""
Shared True Probability scanner module.

Importable by the Streamlit page, the daily snapshot script, and the
forward-test infrastructure. Same logic, single source of truth.

Returns a list of dict picks. Each pick is self-contained — has enough
info to settle the next day (player id where applicable, game id, line, etc.)
"""
from __future__ import annotations

import json
import ssl as _ssl_compat
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()

SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada"

# Filter constants
MIN_TRUE_PROB = 0.75
# Price cap intentionally removed — at 75%+ true probability the fair price is
# already ~-300 or steeper, so capping at -300 was rejecting most chalk that
# qualified. User opted to see ALL prices that clear the probability filter.
MAX_PRICE_CAP = None
MIN_PRICE_CAP = None
MIN_BOOKS = 4

PROP_MARKETS = (
    "batter_home_runs,batter_home_runs_alternate,"
    "batter_hits,batter_hits_alternate,"
    "batter_total_bases,batter_total_bases_alternate,"
    "batter_hits_runs_rbis_alternate,"
    "batter_rbis,batter_rbis_alternate,"
    "batter_runs_scored,batter_runs_scored_alternate,"
    "batter_walks,"
    "pitcher_strikeouts,pitcher_strikeouts_alternate,"
    "pitcher_record_a_win"
)
GAME_LINE_MARKETS = "h2h,spreads,totals"
ALT_MARKETS = "alternate_spreads,alternate_totals"

MARKET_LABELS = {
    "batter_home_runs":                "HR",
    "batter_home_runs_alternate":      "HR alt",
    "batter_hits":                     "Hits",
    "batter_hits_alternate":           "Hits alt",
    "batter_total_bases":              "TB",
    "batter_total_bases_alternate":    "TB alt",
    "batter_hits_runs_rbis_alternate": "H+R+R",
    "batter_rbis":                     "RBIs",
    "batter_rbis_alternate":           "RBIs alt",
    "batter_runs_scored":              "Runs",
    "batter_runs_scored_alternate":    "Runs alt",
    "batter_walks":                    "Walks",
    "pitcher_strikeouts":              "K's",
    "pitcher_strikeouts_alternate":    "K's alt",
    "pitcher_record_a_win":            "Pitcher W",
    "h2h":                             "Moneyline",
    "spreads":                         "Run Line",
    "totals":                          "Total",
    "alternate_spreads":               "Run Line alt",
    "alternate_totals":                "Total alt",
}

SPREAD_MARKETS = {"spreads", "alternate_spreads"}
TOTAL_MARKETS = {"totals", "alternate_totals"}


# ----- Market category for settlement -----

# What stat to check when settling each market
SETTLE_INFO = {
    "batter_home_runs":                ("hr", "Over/Under"),
    "batter_home_runs_alternate":      ("hr", "Over/Under"),
    "batter_hits":                     ("hits", "Over/Under"),
    "batter_hits_alternate":           ("hits", "Over/Under"),
    "batter_total_bases":              ("tb", "Over/Under"),
    "batter_total_bases_alternate":    ("tb", "Over/Under"),
    "batter_hits_runs_rbis_alternate": ("hrr", "Over/Under"),
    "batter_rbis":                     ("rbi", "Over/Under"),
    "batter_rbis_alternate":           ("rbi", "Over/Under"),
    "batter_runs_scored":              ("runs", "Over/Under"),
    "batter_runs_scored_alternate":    ("runs", "Over/Under"),
    "batter_walks":                    ("walks", "Over/Under"),
    "pitcher_strikeouts":              ("ks", "Over/Under"),
    "pitcher_strikeouts_alternate":    ("ks", "Over/Under"),
    "pitcher_record_a_win":            ("pitcher_w", "Yes"),
    "h2h":                             ("h2h", "Team"),
    "spreads":                         ("spread", "Team"),
    "totals":                          ("total", "Over/Under"),
    "alternate_spreads":               ("spread", "Team"),
    "alternate_totals":                ("total", "Over/Under"),
}


# ---------- Utilities ----------

def amer_to_imp(am):
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def ev_per_100(am, p):
    if am > 0:
        return p * am - (1 - p) * 100
    return p * 100 - (1 - p) * abs(am)


def format_selection(market_key, selection, side, point, game_label=None):
    """Returns a human-readable bet description.
    Totals/Spreads include the game so they're not ambiguous when listed
    alongside other plays. game_label like 'Yankees @ Guardians'."""
    match_suffix = f" ({game_label})" if game_label else ""
    if market_key in SPREAD_MARKETS:
        if point is None:
            return (selection or side) + match_suffix
        sign = "+" if point > 0 else ""
        return f"{selection} {sign}{point}"   # team name already in selection
    if market_key in TOTAL_MARKETS:
        if point is None:
            return f"{side}{match_suffix}"
        return f"{side} {point}{match_suffix}"
    return selection or side


# ---------- Fetchers ----------

def fetch_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={api_key}"
    return json.loads(urllib.request.urlopen(url, timeout=15, context=_UNVERIFIED_SSL).read())


def fetch_game_lines(api_key):
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
           f"?apiKey={api_key}&regions=us&markets={GAME_LINE_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_UNVERIFIED_SSL).read())
    except Exception:
        return []


def fetch_event_props(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={PROP_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_UNVERIFIED_SSL).read())
    except Exception:
        return {}


def fetch_event_alts(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={ALT_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_UNVERIFIED_SSL).read())
    except Exception:
        return {}


# ---------- Scanner ----------

def _process(market_key, outcomes_by_key, game_label, away_team, home_team,
             event_id, first_pitch_iso, plays):
    """outcomes_by_key: {(side, point, player): [(book, price)]}."""
    stat_key, settle_type = SETTLE_INFO.get(market_key, (None, None))
    for (side, point, player), book_prices in outcomes_by_key.items():
        if len(book_prices) < MIN_BOOKS:
            continue
        best_book, best_price = max(book_prices, key=lambda x: x[1])
        # Price cap intentionally disabled — see top of file.
        if MAX_PRICE_CAP is not None and best_price > MAX_PRICE_CAP:
            continue
        if MIN_PRICE_CAP is not None and best_price < MIN_PRICE_CAP:
            continue
        imps = [amer_to_imp(pr) for _, pr in book_prices]
        consensus = sum(imps) / len(imps)
        if consensus < MIN_TRUE_PROB:
            continue
        ev = ev_per_100(best_price, consensus)
        plays.append({
            "event_id":      event_id,
            "game":          game_label,
            "away_team":     away_team,
            "home_team":     home_team,
            "first_pitch":   first_pitch_iso,
            "market":        MARKET_LABELS.get(market_key, market_key),
            "market_key":    market_key,
            "stat_key":      stat_key,
            "settle_type":   settle_type,
            "selection":     format_selection(market_key, player, side, point, game_label),
            "player":        player,
            "side":          side,   # Over/Under/Home/Away/Yes
            "point":         point,  # numerical line
            "best_book":     best_book,
            "best_price":    best_price,
            "true_prob_pct": round(consensus * 100, 2),
            "ev_per_100":    round(ev, 2),
            "n_books":       len(book_prices),
            "all_prices":    sorted(book_prices, key=lambda x: -x[1]),
        })


def scan(api_key, include_alts=True):
    """Run the full True Probability scan. Returns list of pick dicts."""
    events = fetch_events(api_key)
    now_utc = datetime.now(timezone.utc)

    upcoming = []
    for e in events:
        ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        hours_to_start = (ct - now_utc).total_seconds() / 3600
        if hours_to_start > -0.5:
            upcoming.append((e, hours_to_start))
    upcoming.sort(key=lambda x: x[1])

    if not upcoming:
        return []

    event_meta = {}
    for e, hrs in upcoming:
        away = e["away_team"].split()[-1]
        home = e["home_team"].split()[-1]
        event_meta[e["id"]] = {
            "game_label": f"{away} @ {home}",
            "away_team": e["away_team"],
            "home_team": e["home_team"],
            "commence_time": e["commence_time"],
            "hours_to_start": hrs,
        }

    plays = []

    # 1) Game lines (one call covers all games)
    game_lines = fetch_game_lines(api_key)
    for g in game_lines:
        eid = g.get("id")
        if eid not in event_meta:
            continue
        meta = event_meta[eid]
        by_market = defaultdict(lambda: defaultdict(list))
        for b in g.get("bookmakers", []):
            for m in b.get("markets", []):
                mk = m["key"]
                for o in m.get("outcomes", []):
                    name = (o.get("name") or "").strip()
                    point = o.get("point")
                    price = o.get("price")
                    if price is None:
                        continue
                    if mk == "h2h":
                        side = "Home" if name == meta["home_team"] else \
                               ("Away" if name == meta["away_team"] else name)
                        selection = name
                    elif mk == "spreads":
                        side = "Home" if name == meta["home_team"] else \
                               ("Away" if name == meta["away_team"] else name)
                        selection = name
                    elif mk == "totals":
                        side = name
                        selection = "Game Total"
                    else:
                        side = name
                        selection = name
                    by_market[mk][(side, point, selection)].append((b["key"], int(price)))
        for mk, outcomes in by_market.items():
            _process(mk, outcomes, meta["game_label"],
                     meta["away_team"], meta["home_team"], eid,
                     meta["commence_time"], plays)

    # 2) Props per event
    for e, _ in upcoming:
        eid = e["id"]
        meta = event_meta[eid]
        data = fetch_event_props(api_key, eid)
        by_market = defaultdict(lambda: defaultdict(list))
        for b in data.get("bookmakers", []):
            for m in b.get("markets", []):
                mk = m["key"]
                for o in m.get("outcomes", []):
                    name = (o.get("name") or "").strip()
                    player = o.get("description") or name
                    if player in ("Over", "Under"):
                        continue
                    side = name if name in ("Over", "Under") else "Yes"
                    point = o.get("point")
                    price = o.get("price")
                    if price is None:
                        continue
                    by_market[mk][(side, point, player)].append((b["key"], int(price)))
        for mk, outcomes in by_market.items():
            _process(mk, outcomes, meta["game_label"],
                     meta["away_team"], meta["home_team"], eid,
                     meta["commence_time"], plays)

    # 3) Alts per event (optional)
    if include_alts:
        for e, _ in upcoming:
            eid = e["id"]
            meta = event_meta[eid]
            data = fetch_event_alts(api_key, eid)
            by_market = defaultdict(lambda: defaultdict(list))
            for b in data.get("bookmakers", []):
                for m in b.get("markets", []):
                    mk = m["key"]
                    for o in m.get("outcomes", []):
                        name = (o.get("name") or "").strip()
                        point = o.get("point")
                        price = o.get("price")
                        if price is None:
                            continue
                        if mk == "alternate_spreads":
                            side = "Home" if name == meta["home_team"] else \
                                   ("Away" if name == meta["away_team"] else name)
                            selection = name
                        elif mk == "alternate_totals":
                            side = name
                            selection = "Game Total"
                        else:
                            side = name
                            selection = name
                        by_market[mk][(side, point, selection)].append((b["key"], int(price)))
            for mk, outcomes in by_market.items():
                _process(mk, outcomes, meta["game_label"],
                         meta["away_team"], meta["home_team"], eid,
                         meta["commence_time"], plays)

    plays.sort(key=lambda r: (-r["true_prob_pct"], -r["ev_per_100"]))
    return plays
