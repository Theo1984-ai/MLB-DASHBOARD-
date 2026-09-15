"""
NFL True Probability scanner.

Same consensus-devig logic as true_prob_scanner.py — just NFL sport key
and NFL prop markets. Scans sharp books for every NFL game this week and
returns plays where the consensus true probability >= 75%.
"""
from __future__ import annotations

import json
import ssl as _ssl_compat
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()

SPORT           = "americanfootball_nfl"
SHARP_BOOKS     = "draftkings,fanduel,betmgm,williamhill_us,bovada,pinnacle"
MIN_TRUE_PROB   = 0.75
MIN_PRICE_CAP   = -400   # reject juice worse than -400 (same as MLB — high-juice bleeder)
MAX_PRICE_CAP   = None
MIN_EV_PER_100  = 3.0
MIN_BOOKS       = 4

GAME_LINE_MARKETS = "h2h,spreads,totals"
ALT_MARKETS       = "alternate_spreads,alternate_totals"

# NFL player prop markets available on the Odds API
PROP_MARKETS = (
    "player_pass_yards,player_pass_tds,player_pass_attempts,player_pass_completions,"
    "player_rush_yards,player_rush_attempts,"
    "player_reception_yards,player_receptions,"
    "player_anytime_td,player_first_td,"
    "player_kicking_points"
)

MARKET_LABELS = {
    # Game lines
    "h2h":                    "Moneyline",
    "spreads":                "Spread",
    "totals":                 "Total",
    "alternate_spreads":      "Spread alt",
    "alternate_totals":       "Total alt",
    # Passing
    "player_pass_yards":      "Pass Yds",
    "player_pass_tds":        "Pass TDs",
    "player_pass_attempts":   "Pass Att",
    "player_pass_completions":"Pass Comp",
    # Rushing
    "player_rush_yards":      "Rush Yds",
    "player_rush_attempts":   "Rush Att",
    # Receiving
    "player_reception_yards": "Rec Yds",
    "player_receptions":      "Receptions",
    # TDs
    "player_anytime_td":      "Anytime TD",
    "player_first_td":        "First TD",
    # Kicking
    "player_kicking_points":  "Kicking Pts",
}

SPREAD_MARKETS = {"spreads", "alternate_spreads"}
TOTAL_MARKETS  = {"totals", "alternate_totals"}
PROP_MARKET_SET = set(PROP_MARKETS.split(","))

SETTLE_INFO = {
    "h2h":                    ("h2h", "Team"),
    "spreads":                ("spread", "Team"),
    "totals":                 ("total", "Over/Under"),
    "alternate_spreads":      ("spread", "Team"),
    "alternate_totals":       ("total", "Over/Under"),
    "player_pass_yards":      ("pass_yards", "Over/Under"),
    "player_pass_tds":        ("pass_tds", "Over/Under"),
    "player_pass_attempts":   ("pass_att", "Over/Under"),
    "player_pass_completions":("pass_comp", "Over/Under"),
    "player_rush_yards":      ("rush_yards", "Over/Under"),
    "player_rush_attempts":   ("rush_att", "Over/Under"),
    "player_reception_yards": ("rec_yards", "Over/Under"),
    "player_receptions":      ("receptions", "Over/Under"),
    "player_anytime_td":      ("anytime_td", "Yes"),
    "player_first_td":        ("first_td", "Yes"),
    "player_kicking_points":  ("kick_pts", "Over/Under"),
}


# ---------- Utilities (identical to MLB scanner) ----------

def amer_to_imp(am):
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def ev_per_100(am, p):
    if am > 0:
        return p * am - (1 - p) * 100
    return p * 100 - (1 - p) * abs(am)


def format_selection(market_key, selection, side, point, game_label=None):
    match_suffix = f" ({game_label})" if game_label else ""
    if market_key in SPREAD_MARKETS:
        if point is None:
            return (selection or side) + match_suffix
        sign = "+" if point > 0 else ""
        return f"{selection} {sign}{point}"
    if market_key in TOTAL_MARKETS:
        if point is None:
            return f"{side}{match_suffix}"
        return f"{side} {point}{match_suffix}"
    return selection or side


# ---------- Fetchers ----------

def fetch_events(api_key):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/events"
           f"?apiKey={api_key}")
    return json.loads(urllib.request.urlopen(url, timeout=15,
                                              context=_UNVERIFIED_SSL).read())


def fetch_game_lines(api_key):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
           f"?apiKey={api_key}&regions=us&markets={GAME_LINE_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20,
                                                  context=_UNVERIFIED_SSL).read())
    except Exception:
        return []


def fetch_event_props(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={PROP_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20,
                                                  context=_UNVERIFIED_SSL).read())
    except Exception:
        return {}


def fetch_event_alts(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={ALT_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20,
                                                  context=_UNVERIFIED_SSL).read())
    except Exception:
        return {}


# ---------- Scanner ----------

def _process(market_key, outcomes_by_key, game_label, away_team, home_team,
             event_id, first_pitch_iso, plays):
    stat_key, settle_type = SETTLE_INFO.get(market_key, (None, None))
    for (side, point, player), book_prices in outcomes_by_key.items():
        if len(book_prices) < MIN_BOOKS:
            continue
        best_book, best_price = max(book_prices, key=lambda x: x[1])
        if MAX_PRICE_CAP is not None and best_price > MAX_PRICE_CAP:
            continue
        if MIN_PRICE_CAP is not None and best_price < MIN_PRICE_CAP:
            continue
        imps = [amer_to_imp(pr) for _, pr in book_prices]
        consensus = sum(imps) / len(imps)
        if consensus < MIN_TRUE_PROB:
            continue
        ev = ev_per_100(best_price, consensus)
        if ev < MIN_EV_PER_100:
            continue
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
            "side":          side,
            "point":         point,
            "best_book":     best_book,
            "best_price":    best_price,
            "true_prob_pct": round(consensus * 100, 2),
            "ev_per_100":    round(ev, 2),
            "n_books":       len(book_prices),
            "all_prices":    sorted(book_prices, key=lambda x: -x[1]),
        })


def scan(api_key, include_alts=True):
    """Run the full NFL True Probability scan. Returns list of pick dicts."""
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
            "game_label":    f"{away} @ {home}",
            "away_team":     e["away_team"],
            "home_team":     e["home_team"],
            "commence_time": e["commence_time"],
            "hours_to_start": hrs,
        }

    plays = []

    # 1) Game lines
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
                    name  = (o.get("name") or "").strip()
                    point = o.get("point")
                    price = o.get("price")
                    if price is None:
                        continue
                    if mk == "h2h":
                        side = ("Home" if name == meta["home_team"] else
                                "Away" if name == meta["away_team"] else name)
                        selection = name
                    elif mk == "spreads":
                        side = ("Home" if name == meta["home_team"] else
                                "Away" if name == meta["away_team"] else name)
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

    # 2) Player props per event
    for e, _ in upcoming:
        eid   = e["id"]
        meta  = event_meta[eid]
        data  = fetch_event_props(api_key, eid)
        by_market = defaultdict(lambda: defaultdict(list))
        for b in data.get("bookmakers", []):
            for m in b.get("markets", []):
                mk = m["key"]
                for o in m.get("outcomes", []):
                    name   = (o.get("name") or "").strip()
                    player = o.get("description") or name
                    if player in ("Over", "Under", "Yes", "No"):
                        continue
                    side  = name if name in ("Over", "Under", "Yes", "No") else "Yes"
                    point = o.get("point")
                    price = o.get("price")
                    if price is None:
                        continue
                    by_market[mk][(side, point, player)].append((b["key"], int(price)))
        for mk, outcomes in by_market.items():
            _process(mk, outcomes, meta["game_label"],
                     meta["away_team"], meta["home_team"], eid,
                     meta["commence_time"], plays)

    # 3) Alt spreads/totals per event
    if include_alts:
        for e, _ in upcoming:
            eid  = e["id"]
            meta = event_meta[eid]
            data = fetch_event_alts(api_key, eid)
            by_market = defaultdict(lambda: defaultdict(list))
            for b in data.get("bookmakers", []):
                for m in b.get("markets", []):
                    mk = m["key"]
                    for o in m.get("outcomes", []):
                        name  = (o.get("name") or "").strip()
                        point = o.get("point")
                        price = o.get("price")
                        if price is None:
                            continue
                        if mk == "alternate_spreads":
                            side = ("Home" if name == meta["home_team"] else
                                    "Away" if name == meta["away_team"] else name)
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
