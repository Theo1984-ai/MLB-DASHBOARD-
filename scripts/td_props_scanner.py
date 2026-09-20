"""
TD Props scanner — NFL (and optionally CFB).

Scans player_anytime_td, player_first_td, player_last_td, player_pass_tds
across all sharp books.  Returns every player/market with:
  • consensus true probability  (average of devigged implied probs)
  • best price + book
  • value edge  (best implied - consensus, positive = value at best book)
  • sharp gap   (DK/FD implied avg - lag books implied avg;
                 positive = sharp books have player at higher prob,
                 i.e. sharp money moved the early books)
"""
from __future__ import annotations

import json
import ssl as _ssl_compat
import urllib.request
from datetime import datetime, timezone

_SSL = _ssl_compat._create_unverified_context()

SHARP_LEADS  = {"draftkings", "fanduel"}
SHARP_BOOKS  = "draftkings,fanduel,betmgm,williamhill_us,bovada,pinnacle"

# Markets fetched in one per-event call
TD_MARKETS = (
    "player_anytime_td,"
    "player_first_td,"
    "player_last_td,"
    "player_pass_tds"
)

MARKET_LABELS = {
    "player_anytime_td": "Anytime TD",
    "player_first_td":   "First TD",
    "player_last_td":    "Last TD",
    "player_pass_tds":   "Pass TDs",
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


def _fetch_td_props(api_key: str, sport: str, event_id: str) -> dict:
    url = (
        f"https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds"
        f"?apiKey={api_key}&regions=us&markets={TD_MARKETS}"
        f"&bookmakers={SHARP_BOOKS}&oddsFormat=american"
    )
    try:
        return _fetch(url)
    except Exception:
        return {}


def scan(api_key: str, sport: str = "americanfootball_nfl") -> list[dict]:
    """
    Returns a list of TD prop rows, each dict:
      player, market, market_key, game, away_team, home_team,
      first_pitch, side, point,
      consensus_prob (0-100), best_price (American), best_book,
      value_edge (pct pts, positive = value),
      sharp_gap (pct pts, positive = sharp books higher than lag),
      n_books, book_prices {book_key: American odds}
    """
    now_utc = datetime.now(timezone.utc)

    events = _fetch_events(api_key, sport)
    upcoming = []
    for e in events:
        try:
            ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
            if (ct - now_utc).total_seconds() > -1800:   # include up to 30 min past start
                upcoming.append(e)
        except Exception:
            continue
    upcoming.sort(key=lambda e: e["commence_time"])

    results = []

    for ev in upcoming:
        eid  = ev["id"]
        away = ev.get("away_team", "?")
        home = ev.get("home_team", "?")
        game_label = f"{away.split()[-1]} @ {home.split()[-1]}"
        fp   = ev.get("commence_time", "")

        data = _fetch_td_props(api_key, sport, eid)
        if not data:
            continue

        # Collect per-market, per-player/side/point → {book: price}
        by_key: dict[tuple, dict[str, int]] = {}   # (market_key, player, side, point) → {book: price}

        for bm in data.get("bookmakers", []):
            bk = bm.get("key", "")
            for mkt in bm.get("markets", []):
                mk = mkt.get("key", "")
                if mk not in MARKET_LABELS:
                    continue
                for o in mkt.get("outcomes", []):
                    name  = (o.get("name") or "").strip()
                    desc  = (o.get("description") or "").strip()
                    point = o.get("point")
                    price = o.get("price")
                    if price is None:
                        continue

                    # TD scorer markets: name=player, side="Yes"
                    # Pass TDs: name="Over"/"Under", desc=player
                    if mk in ("player_anytime_td", "player_first_td", "player_last_td"):
                        player = desc or name
                        side   = "Yes"
                        if player.lower() in ("yes", "no", "over", "under", ""):
                            continue
                    else:
                        # player_pass_tds
                        player = desc or name
                        side   = name  # "Over" or "Under"
                        if side not in ("Over", "Under"):
                            continue
                        # Skip "No" side for TD scorer markets
                        if player.lower() in ("over", "under", ""):
                            player = desc or "Game"

                    key = (mk, player, side, point)
                    if key not in by_key:
                        by_key[key] = {}
                    by_key[key][bk] = int(price)

        for (mk, player, side, point), book_prices in by_key.items():
            if len(book_prices) < 2:
                continue

            prices_list = list(book_prices.items())  # [(book, price), ...]
            imps = {bk: _amer_to_imp(pr) for bk, pr in prices_list}

            consensus = sum(imps.values()) / len(imps)

            best_book, best_price = max(prices_list, key=lambda x: x[1])
            best_imp = _amer_to_imp(best_price)
            value_edge = round((best_imp - consensus) * 100, 1)

            # Sharp gap: leading books vs lag books
            lead_imps = [imp for bk, imp in imps.items() if bk in SHARP_LEADS]
            lag_imps  = [imp for bk, imp in imps.items() if bk not in SHARP_LEADS]
            if lead_imps and lag_imps:
                sharp_gap = round(
                    (sum(lead_imps) / len(lead_imps) - sum(lag_imps) / len(lag_imps)) * 100, 1
                )
            else:
                sharp_gap = 0.0

            # Per-book price map for column display
            book_row = {bk: book_prices.get(bk) for bk in ALL_BOOKS_ORDERED}

            results.append({
                "player":        player,
                "market":        MARKET_LABELS[mk],
                "market_key":    mk,
                "game":          game_label,
                "away_team":     away,
                "home_team":     home,
                "first_pitch":   fp,
                "side":          side,
                "point":         point,
                "consensus_prob": round(consensus * 100, 1),
                "best_price":    best_price,
                "best_book":     BOOK_SHORT.get(best_book, best_book),
                "value_edge":    value_edge,
                "sharp_gap":     sharp_gap,
                "n_books":       len(book_prices),
                **{f"price_{BOOK_SHORT.get(bk, bk)}": book_row[bk]
                   for bk in ALL_BOOKS_ORDERED},
            })

    # Sort: by market order, then by consensus_prob desc
    market_order = {"Anytime TD": 0, "First TD": 1, "Last TD": 2, "Pass TDs": 3}
    results.sort(key=lambda r: (market_order.get(r["market"], 9), -r["consensus_prob"]))
    return results
