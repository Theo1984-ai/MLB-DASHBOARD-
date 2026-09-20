"""
TD Props scanner — NFL (and optionally CFB).

Scans player_anytime_td, player_first_td, player_last_td, player_pass_tds
across all sharp books.  Returns (rows, debug) where debug has diagnostic info.
"""
from __future__ import annotations

import json
import ssl as _ssl_compat
import urllib.request
from datetime import datetime, timezone

_SSL = _ssl_compat._create_unverified_context()

SHARP_LEADS  = {"draftkings", "fanduel"}
SHARP_BOOKS  = "draftkings,fanduel,betmgm,williamhill_us,bovada,pinnacle"

# TD scorer markets only
SCORER_MARKETS = "player_anytime_td,player_first_td,player_last_td"

MARKET_LABELS = {
    "player_anytime_td": "Anytime TD",
    "player_first_td":   "First TD",
    "player_last_td":    "Last TD",
}

BOOK_SHORT = {
    "draftkings":    "DK",
    "fanduel":       "FD",
    "betmgm":        "MGM",
    "williamhill_us":"CZR",
    "bovada":        "BOV",
    "pinnacle":      "PIN",
}

ALL_BOOKS_ORDERED = [
    "draftkings", "fanduel", "betmgm", "williamhill_us", "bovada", "pinnacle"
]


def _amer_to_imp(am: float) -> float:
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def _fetch(url: str):
    return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())


def _fetch_events(api_key: str, sport: str):
    return _fetch(
        f"https://api.the-odds-api.com/v4/sports/{sport}/events?apiKey={api_key}"
    )


def _fetch_props(api_key: str, sport: str, event_id: str, markets: str) -> dict:
    url = (
        f"https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds"
        f"?apiKey={api_key}&regions=us&markets={markets}"
        f"&bookmakers={SHARP_BOOKS}&oddsFormat=american"
    )
    try:
        return _fetch(url)
    except Exception as exc:
        return {"_error": str(exc)}


def _parse_outcomes(data: dict, by_key: dict, debug: dict):
    """Merge bookmaker outcomes from an API response into by_key."""
    for bm in data.get("bookmakers", []):
        bk = bm.get("key", "")
        for mkt in bm.get("markets", []):
            mk = mkt.get("key", "")
            if mk not in MARKET_LABELS:
                continue
            debug["markets_seen"].add(mk)
            for o in mkt.get("outcomes", []):
                name  = (o.get("name") or "").strip()
                desc  = (o.get("description") or "").strip()
                point = o.get("point")
                price = o.get("price")
                if price is None:
                    continue

                if mk in ("player_anytime_td", "player_first_td", "player_last_td"):
                    # name = "Yes"/"No", description = player name  (most books)
                    # OR name = player name, description = ""       (some books)
                    player = desc if desc and desc.lower() not in ("yes", "no", "") else name
                    side   = "Yes"
                    if not player or player.lower() in ("yes", "no", "over", "under"):
                        continue
                    # Skip the "No" outcome
                    if name.lower() == "no":
                        continue
                else:
                    # player_pass_tds: name = "Over"/"Under", description = player name
                    player = desc or name
                    side   = name  # "Over" or "Under"
                    if side not in ("Over", "Under"):
                        continue
                    if not player or player.lower() in ("over", "under"):
                        player = desc or "Game"

                key = (mk, player, side, point)
                if key not in by_key:
                    by_key[key] = {}
                by_key[key][bk] = int(price)
                debug["outcomes_parsed"] += 1


def scan(api_key: str, sport: str = "americanfootball_nfl") -> tuple[list[dict], dict]:
    """
    Returns (rows, debug).

    rows: list of TD prop dicts
    debug: diagnostic info — n_events, n_upcoming, n_with_props, markets_seen, errors
    """
    now_utc = datetime.now(timezone.utc)
    debug: dict = {
        "n_events": 0,
        "n_upcoming": 0,
        "n_with_scorer_props": 0,
        "markets_seen": set(),
        "outcomes_parsed": 0,
        "errors": [],
    }

    try:
        events = _fetch_events(api_key, sport)
    except Exception as exc:
        debug["errors"].append(f"fetch_events: {exc}")
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

    results = []

    for ev in upcoming:
        eid  = ev["id"]
        away = ev.get("away_team", "?")
        home = ev.get("home_team", "?")
        game_label = f"{away.split()[-1]} @ {home.split()[-1]}"
        fp   = ev.get("commence_time", "")

        by_key: dict = {}

        # Scorer props (anytime / first / last TD)
        scorer_data = _fetch_props(api_key, sport, eid, SCORER_MARKETS)
        if "_error" in scorer_data:
            debug["errors"].append(f"{game_label} scorer: {scorer_data['_error']}")
        elif scorer_data.get("bookmakers"):
            debug["n_with_scorer_props"] += 1
            _parse_outcomes(scorer_data, by_key, debug)

        for (mk, player, side, point), book_prices in by_key.items():
            prices_list = list(book_prices.items())
            imps = {bk: _amer_to_imp(pr) for bk, pr in prices_list}
            consensus = sum(imps.values()) / len(imps)

            best_book, best_price = max(prices_list, key=lambda x: x[1])
            best_imp   = _amer_to_imp(best_price)
            value_edge = round((best_imp - consensus) * 100, 1)

            lead_imps = [imp for bk, imp in imps.items() if bk in SHARP_LEADS]
            lag_imps  = [imp for bk, imp in imps.items() if bk not in SHARP_LEADS]
            if lead_imps and lag_imps:
                sharp_gap = round(
                    (sum(lead_imps) / len(lead_imps) - sum(lag_imps) / len(lag_imps)) * 100, 1
                )
            else:
                sharp_gap = 0.0

            book_row = {bk: book_prices.get(bk) for bk in ALL_BOOKS_ORDERED}

            results.append({
                "player":         player,
                "market":         MARKET_LABELS[mk],
                "market_key":     mk,
                "game":           game_label,
                "away_team":      away,
                "home_team":      home,
                "first_pitch":    fp,
                "side":           side,
                "point":          point,
                "consensus_prob": round(consensus * 100, 1),
                "best_price":     best_price,
                "best_book":      BOOK_SHORT.get(best_book, best_book),
                "value_edge":     value_edge,
                "sharp_gap":      sharp_gap,
                "n_books":        len(book_prices),
                **{f"price_{BOOK_SHORT.get(bk, bk)}": book_row[bk]
                   for bk in ALL_BOOKS_ORDERED},
            })

    market_order = {"Anytime TD": 0, "First TD": 1, "Last TD": 2, "Pass TDs": 3}
    results.sort(key=lambda r: (market_order.get(r["market"], 9), -r["consensus_prob"]))
    debug["markets_seen"] = list(debug["markets_seen"])
    return results, debug
