"""
CFB True Probability scanner.
Mirrors nfl_true_prob_scanner.py for americanfootball_ncaaf.
"""
import json
import os
import ssl as _ssl
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_SSL = _ssl._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")

SPORT = "americanfootball_ncaaf"
SHARP_BOOKS = ["draftkings", "fanduel", "betmgm", "williamhill_us", "bovada"]
GAME_MARKETS = ["h2h", "spreads", "totals"]
# CFB has fewer prop markets available than NFL
PROP_MARKETS = [
    "player_pass_yards", "player_pass_tds", "player_rush_yards",
    "player_rush_attempts", "player_reception_yards", "player_receptions",
    "player_anytime_td", "player_first_td",
]
ALT_MARKETS = ["alternate_spreads", "alternate_totals"]

MIN_TRUE_PROB  = 0.75
MIN_PRICE_CAP  = -400
MIN_EV_PER_100 = 3.0
MIN_BOOKS      = 4

MARKET_LABELS = {
    "h2h":                    "Moneyline",
    "spreads":                "Spread",
    "totals":                 "Total",
    "alternate_spreads":      "Spread alt",
    "alternate_totals":       "Total alt",
    "player_pass_yards":      "Pass Yds",
    "player_pass_tds":        "Pass TDs",
    "player_rush_yards":      "Rush Yds",
    "player_rush_attempts":   "Rush Att",
    "player_reception_yards": "Rec Yds",
    "player_receptions":      "Receptions",
    "player_anytime_td":      "Anytime TD",
    "player_first_td":        "First TD",
}


def _fetch(url):
    return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())


def _american_to_prob(price):
    if price >= 100:
        return 100 / (price + 100)
    return abs(price) / (abs(price) + 100)


def _true_prob(prices):
    probs = [_american_to_prob(p) for p in prices]
    total = sum(probs)
    if total <= 0:
        return None
    return probs[0] / total


def _ev_per_100(true_prob, best_price):
    if best_price >= 100:
        win_pct = best_price / 100
    else:
        win_pct = 100 / abs(best_price)
    return round(true_prob * win_pct * 100 - (1 - true_prob) * 100, 2)


def _scan_event_odds(api_key, event_id, markets_str, regions="us"):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{event_id}/odds"
           f"?apiKey={api_key}&regions={regions}&markets={markets_str}"
           f"&bookmakers={','.join(SHARP_BOOKS)}&oddsFormat=american")
    try:
        return _fetch(url)
    except Exception:
        return {}


def _extract_plays(event_data, market_key):
    plays = []
    game   = f"{event_data.get('away_team','?')} @ {event_data.get('home_team','?')}"
    fp     = event_data.get("commence_time", "")
    label  = MARKET_LABELS.get(market_key, market_key)

    # Gather all prices per (selection, side, point)
    bucket = {}
    for bm in event_data.get("bookmakers", []):
        book = bm.get("key")
        if book not in SHARP_BOOKS:
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_key:
                continue
            for o in mkt.get("outcomes", []):
                name  = o.get("name") or o.get("description") or "?"
                side  = o.get("name", "")
                pt    = o.get("point")
                price = o.get("price")
                if price is None:
                    continue
                desc  = o.get("description") or ""
                sel   = f"{desc} {name}".strip() if desc else name
                key   = (sel, side, pt)
                bucket.setdefault(key, {})[book] = price

    for (sel, side, pt), book_prices in bucket.items():
        if len(book_prices) < MIN_BOOKS:
            continue
        prices = list(book_prices.values())
        # Need both sides for true-prob calc on 2-outcome markets
        # For player props, just use the over/yes side
        tp = _true_prob(prices) if len(prices) >= 2 else None
        if tp is None:
            continue
        if tp < MIN_TRUE_PROB:
            continue
        best_book  = min(book_prices, key=lambda b: book_prices[b])
        best_price = book_prices[best_book]
        if best_price < MIN_PRICE_CAP:
            continue
        ev = _ev_per_100(tp, best_price)
        if ev < MIN_EV_PER_100:
            continue
        plays.append({
            "game":         game,
            "first_pitch":  fp,
            "market":       label,
            "selection":    sel,
            "side":         side,
            "point":        pt,
            "best_book":    best_book,
            "best_price":   best_price,
            "true_prob_pct": round(tp * 100, 1),
            "ev_per_100":   ev,
            "n_books":      len(book_prices),
            "all_prices":   sorted(book_prices.items(), key=lambda x: x[1]),
        })
    return plays


def scan(api_key, include_alts=True):
    # Pass 1: game lines for all events at once
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
           f"?apiKey={api_key}&regions=us&markets={','.join(GAME_MARKETS)}"
           f"&bookmakers={','.join(SHARP_BOOKS)}&oddsFormat=american")
    try:
        events = _fetch(url)
    except Exception:
        events = []

    now_utc = datetime.now(tz=timezone.utc)
    plays = []

    for ev in events:
        try:
            ct = datetime.fromisoformat(
                (ev.get("commence_time") or "").replace("Z", "+00:00"))
            if ct < now_utc:
                continue
        except Exception:
            continue
        for mk in GAME_MARKETS:
            plays.extend(_extract_plays(ev, mk))

    # Pass 2: props + alts per event (only for upcoming games)
    event_ids = [ev["id"] for ev in events
                 if ev.get("id") and ev.get("commence_time")]

    prop_mks = PROP_MARKETS + (ALT_MARKETS if include_alts else [])
    for eid in event_ids[:20]:  # cap at 20 to limit API calls
        data = _scan_event_odds(api_key, eid, ",".join(prop_mks))
        if not data:
            continue
        for mk in prop_mks:
            plays.extend(_extract_plays(data, mk))

    plays.sort(key=lambda p: -p["true_prob_pct"])
    # Deduplicate
    seen = set()
    unique = []
    for p in plays:
        k = (p["game"], p["market"], p["selection"], p["side"], p["point"])
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique
