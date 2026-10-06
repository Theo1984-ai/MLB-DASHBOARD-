"""
NHL Player Props scanner.

Fetches player_goals and player_shots_on_goal for today's NHL games.
Returns rows suitable for display in 25_NHL_Player_Props.py.

Books: FanDuel + BetRivers (only books that post NHL player props on Odds API).
"""
from __future__ import annotations

import json
import ssl as _ssl_compat
import urllib.request
from datetime import datetime, timezone

_SSL = _ssl_compat._create_unverified_context()

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
            # Include games up to 30 min after puck drop (props still useful)
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
                        side  = (o.get("name") or "").strip().lower()   # "over" / "under"
                        player = (o.get("description") or "").strip()
                        point = o.get("point")
                        price = o.get("price")
                        if side not in ("over", "under") or not player or point is None or price is None:
                            continue

                        agg.setdefault(mk, {}).setdefault(eid, {}).setdefault(player, {}).setdefault(point, {"over": {}, "under": {}})
                        agg[mk][eid][player][point][side][bk] = int(price)
                        debug["outcomes_parsed"] += 1

    # Build result rows
    results = []
    for mk, ev_dict in agg.items():
        for eid, player_dict in ev_dict.items():
            meta = game_meta.get(eid, {})
            for player, line_dict in player_dict.items():
                for line, sides in line_dict.items():
                    over_prices  = sides.get("over", {})
                    under_prices = sides.get("under", {})

                    # Compute no-vig probability (use FD first as primary, then BR)
                    nv_prob = None
                    for bk in ALL_BOOKS:
                        op = over_prices.get(bk)
                        up = under_prices.get(bk)
                        nv_prob = _no_vig_prob(op, up)
                        if nv_prob is not None:
                            break

                    # Best over / under prices across books
                    best_over  = max(over_prices.values(),  default=None)
                    best_over_book  = next((BOOK_SHORT.get(bk, bk) for bk, p in over_prices.items()  if p == best_over),  None)
                    best_under = max(under_prices.values(), default=None)
                    best_under_book = next((BOOK_SHORT.get(bk, bk) for bk, p in under_prices.items() if p == best_under), None)

                    # Value edge for over: no-vig over prob vs best over price implied
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
                        **{f"over_{BOOK_SHORT.get(bk, bk)}":  over_prices.get(bk)  for bk in ALL_BOOKS},
                        **{f"under_{BOOK_SHORT.get(bk, bk)}": under_prices.get(bk) for bk in ALL_BOOKS},
                    })

    # Sort: by market, then by no-vig over % descending (likeliest overs first)
    market_order = {"Goals": 0, "Shots on Goal": 1}
    results.sort(key=lambda r: (
        market_order.get(r["market"], 9),
        -(r["nv_over_pct"] or 0),
    ))
    return results, debug
