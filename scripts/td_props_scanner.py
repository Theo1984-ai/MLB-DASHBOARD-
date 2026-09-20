"""
TD Props scanner — NFL (and optionally CFB).

Fetches player_anytime_td, player_first_td, player_last_td one market at a
time per event so a missing/unsupported market never blocks the others.
"""
from __future__ import annotations

import json
import ssl as _ssl_compat
import urllib.request
from datetime import datetime, timezone

_SSL = _ssl_compat._create_unverified_context()

SHARP_LEADS = {"draftkings", "fanduel"}
SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada,pinnacle"

TD_SCORER_MARKETS = [
    "player_anytime_td",
    "player_first_td",
    "player_last_td",
]

MARKET_LABELS = {
    "player_anytime_td": "Anytime TD",
    "player_first_td":   "First TD",
    "player_last_td":    "Last TD",
}

BOOK_SHORT = {
    "draftkings":     "DK",
    "fanduel":        "FD",
    "betmgm":         "MGM",
    "williamhill_us": "CZR",
    "bovada":         "BOV",
    "pinnacle":       "PIN",
}

ALL_BOOKS = ["draftkings", "fanduel", "betmgm", "williamhill_us", "bovada", "pinnacle"]


def _amer_to_imp(am: float) -> float:
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def _get(url: str):
    return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())


def scan(api_key: str, sport: str = "americanfootball_nfl") -> tuple[list[dict], dict]:
    """Returns (rows, debug)."""
    now_utc = datetime.now(timezone.utc)
    debug: dict = {
        "n_events": 0,
        "n_upcoming": 0,
        "market_hits": {m: 0 for m in TD_SCORER_MARKETS},
        "outcomes_parsed": 0,
        "errors": [],
    }

    # 1. Fetch events
    try:
        events = _get(
            f"https://api.the-odds-api.com/v4/sports/{sport}/events?apiKey={api_key}"
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

    # 2. Per-event, per-market fetch
    # Collect (market_key, player, side, point) → {book: price}
    agg: dict[tuple, dict[str, int]] = {}

    game_meta: dict[str, dict] = {}
    for ev in upcoming:
        eid  = ev["id"]
        away = ev.get("away_team", "?")
        home = ev.get("home_team", "?")
        game_meta[eid] = {
            "game":  f"{away.split()[-1]} @ {home.split()[-1]}",
            "away":  away,
            "home":  home,
            "fp":    ev.get("commence_time", ""),
        }

        for mk in TD_SCORER_MARKETS:
            url = (
                f"https://api.the-odds-api.com/v4/sports/{sport}/events/{eid}/odds"
                f"?apiKey={api_key}&regions=us&markets={mk}"
                f"&bookmakers={SHARP_BOOKS}&oddsFormat=american"
            )
            try:
                data = _get(url)
            except Exception as exc:
                debug["errors"].append(f"{game_meta[eid]['game']} / {mk}: {exc}")
                continue

            bookmakers = data.get("bookmakers") or []
            if not bookmakers:
                continue

            debug["market_hits"][mk] += 1

            for bm in bookmakers:
                bk = bm.get("key", "")
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != mk:
                        continue
                    for o in mkt.get("outcomes", []):
                        name  = (o.get("name") or "").strip()
                        desc  = (o.get("description") or "").strip()
                        price = o.get("price")
                        if price is None:
                            continue

                        # Most books: name="Yes"/"No", description=player name
                        # Some books: name=player name, description=""
                        if name.lower() == "no":
                            continue  # skip No side
                        if desc and desc.lower() not in ("yes", "no", "over", "under"):
                            player = desc
                        elif name.lower() not in ("yes", "no", "over", "under", ""):
                            player = name
                        else:
                            continue  # can't determine player

                        key = (mk, eid, player)
                        if key not in agg:
                            agg[key] = {}
                        agg[key][bk] = int(price)
                        debug["outcomes_parsed"] += 1

    # 3. Build result rows
    results = []
    for (mk, eid, player), book_prices in agg.items():
        meta = game_meta.get(eid, {})
        prices_list = list(book_prices.items())
        imps = {bk: _amer_to_imp(pr) for bk, pr in prices_list}
        consensus  = sum(imps.values()) / len(imps)

        best_book, best_price = max(prices_list, key=lambda x: x[1])
        value_edge = round((_amer_to_imp(best_price) - consensus) * 100, 1)

        lead_imps = [imp for bk, imp in imps.items() if bk in SHARP_LEADS]
        lag_imps  = [imp for bk, imp in imps.items() if bk not in SHARP_LEADS]
        sharp_gap = 0.0
        if lead_imps and lag_imps:
            sharp_gap = round(
                (sum(lead_imps) / len(lead_imps) - sum(lag_imps) / len(lag_imps)) * 100, 1
            )

        results.append({
            "player":         player,
            "market":         MARKET_LABELS[mk],
            "market_key":     mk,
            "game":           meta.get("game", "?"),
            "away_team":      meta.get("away", "?"),
            "home_team":      meta.get("home", "?"),
            "first_pitch":    meta.get("fp", ""),
            "consensus_prob": round(consensus * 100, 1),
            "best_price":     best_price,
            "best_book":      BOOK_SHORT.get(best_book, best_book),
            "value_edge":     value_edge,
            "sharp_gap":      sharp_gap,
            "n_books":        len(book_prices),
            **{f"price_{BOOK_SHORT.get(bk, bk)}": book_prices.get(bk) for bk in ALL_BOOKS},
        })

    market_order = {"Anytime TD": 0, "First TD": 1, "Last TD": 2}
    results.sort(key=lambda r: (market_order.get(r["market"], 9), -r["consensus_prob"]))
    return results, debug
