"""
Match Polymarket CFB sharp signals against sportsbook lines from The Odds API.
Mirror of sportsbook_matcher.py for americanfootball_ncaaf.
"""
from __future__ import annotations

import json
import ssl as _ssl
import urllib.request
from collections import defaultdict

_SSL = _ssl._create_unverified_context()
SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada,pinnacle"
SPORT = "americanfootball_ncaaf"


def _amer_to_imp(am):
    if am > 0: return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def _norm_team(name):
    if not name: return ""
    n = name.lower().strip()
    return n


def fetch_game_lines(api_key):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
           f"?apiKey={api_key}&regions=us&markets=h2h,spreads,totals"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception:
        return []


def _find_all_prices(bookmakers, market_key, outcome_filter):
    out = []
    for b in bookmakers:
        for m in b.get("markets", []):
            if m["key"] != market_key: continue
            for o in m.get("outcomes", []):
                if outcome_filter(o):
                    price = o.get("price")
                    if price is None: continue
                    out.append({"price": int(price), "book": b["key"],
                                "name": o.get("name"), "point": o.get("point")})
    return out


def _find_best_price(bookmakers, market_key, outcome_filter):
    all_p = _find_all_prices(bookmakers, market_key, outcome_filter)
    if not all_p: return None
    return max(all_p, key=lambda x: x["price"])


def _consensus_implied_pct(all_prices):
    if not all_prices:
        return (None, 0)
    imps = sorted(_amer_to_imp(p["price"]) for p in all_prices)
    n = len(imps)
    med = imps[n // 2] if n % 2 == 1 else (imps[n//2 - 1] + imps[n//2]) / 2
    return (round(med * 100, 1), n)


def match_signals(polymarket_rows, api_key):
    games = fetch_game_lines(api_key)
    game_by_pair = {}
    for g in games:
        a = _norm_team(g.get("away_team", ""))
        h = _norm_team(g.get("home_team", ""))
        if a and h:
            game_by_pair[(a, h)] = g

    enriched = []
    for r in polymarket_rows:
        row = dict(r)
        row["sb_best_price"]            = None
        row["sb_book"]                  = None
        row["sb_implied_pct"]           = None
        row["sb_consensus_implied_pct"] = None
        row["sb_n_books"]               = 0
        row["pm_implied_pct_yes"]       = round(r["mid"] * 100, 1)
        row["edge_best_pp"]             = None
        row["edge_pp"]                  = None
        row["play"]                     = None

        away_pm = _norm_team(r.get("away_team", ""))
        home_pm = _norm_team(r.get("home_team", ""))

        game = (game_by_pair.get((away_pm, home_pm))
                or game_by_pair.get((home_pm, away_pm)))
        if not game:
            row["play"] = "game not on sportsbook board yet"
            enriched.append(row); continue

        books = game.get("bookmakers", [])
        sharp_side = r["skew_side"]

        row["first_pitch"] = game.get("commence_time")
        row["sb_away_team"] = game.get("away_team")
        row["sb_home_team"] = game.get("home_team")

        if r["match_type"] == "h2h":
            if sharp_side == "YES":
                target_team = r["away_team"]
            else:
                target_team = r["home_team"]
            tt_norm = _norm_team(target_team)
            all_p = _find_all_prices(books, "h2h",
                lambda o: _norm_team(o.get("name", "")) == tt_norm)
            best = max(all_p, key=lambda x: x["price"]) if all_p else None
            if best:
                row["sb_best_price"] = best["price"]
                row["sb_book"] = best["book"]
                row["sb_implied_pct"] = round(_amer_to_imp(best["price"]) * 100, 1)
                cons_pct, n_b = _consensus_implied_pct(all_p)
                row["sb_consensus_implied_pct"] = cons_pct
                row["sb_n_books"] = n_b
                pm_pct = (r["mid"] if sharp_side == "YES" else 1 - r["mid"]) * 100
                row["edge_best_pp"] = round(pm_pct - row["sb_implied_pct"], 1)
                row["edge_pp"] = round(pm_pct - cons_pct, 1) if cons_pct is not None else None
                row["play"] = f"{target_team} ML @ {best['book']} {best['price']:+d}"
                row["bet_stat_key"] = "h2h"
                row["bet_team"] = target_team
                row["bet_side"] = ("Home" if tt_norm == _norm_team(game.get("home_team","")) else "Away")
                row["bet_point"] = None
                row["best_price"] = best["price"]

        elif r["match_type"] == "totals" and r.get("point") is not None:
            target_side = "Over" if sharp_side == "YES" else "Under"
            tgt_pt = r["point"]
            all_p = _find_all_prices(books, "totals",
                lambda o: (o.get("name") == target_side
                           and abs((o.get("point") or 0) - tgt_pt) < 0.01))
            best = max(all_p, key=lambda x: x["price"]) if all_p else None
            if best:
                row["sb_best_price"] = best["price"]
                row["sb_book"] = best["book"]
                row["sb_implied_pct"] = round(_amer_to_imp(best["price"]) * 100, 1)
                cons_pct, n_b = _consensus_implied_pct(all_p)
                row["sb_consensus_implied_pct"] = cons_pct
                row["sb_n_books"] = n_b
                pm_pct = (r["mid"] if sharp_side == "YES" else 1 - r["mid"]) * 100
                row["edge_best_pp"] = round(pm_pct - row["sb_implied_pct"], 1)
                row["edge_pp"] = round(pm_pct - cons_pct, 1) if cons_pct is not None else None
                away_short = (game.get("away_team","").split()[-1] if game.get("away_team") else "")
                home_short = (game.get("home_team","").split()[-1] if game.get("home_team") else "")
                match_str = f" ({away_short} @ {home_short})" if away_short and home_short else ""
                row["play"] = f"{target_side} {tgt_pt}{match_str} @ {best['book']} {best['price']:+d}"
                row["bet_stat_key"] = "total"
                row["bet_team"] = None
                row["bet_side"] = target_side
                row["bet_point"] = tgt_pt
                row["best_price"] = best["price"]

        elif r["match_type"] == "spreads" and r.get("point") is not None and r.get("team"):
            if sharp_side == "YES":
                target_team = r["team"]; target_point = r["point"]
            else:
                target_team = r["team"]; target_point = -r["point"]
            tt_norm = _norm_team(target_team)
            all_p = _find_all_prices(books, "spreads",
                lambda o: (_norm_team(o.get("name","")) == tt_norm
                           and abs((o.get("point") or 0) - target_point) < 0.01))
            best = max(all_p, key=lambda x: x["price"]) if all_p else None
            if best:
                row["sb_best_price"] = best["price"]
                row["sb_book"] = best["book"]
                row["sb_implied_pct"] = round(_amer_to_imp(best["price"]) * 100, 1)
                cons_pct, n_b = _consensus_implied_pct(all_p)
                row["sb_consensus_implied_pct"] = cons_pct
                row["sb_n_books"] = n_b
                pm_pct = (r["mid"] if sharp_side == "YES" else 1 - r["mid"]) * 100
                row["edge_best_pp"] = round(pm_pct - row["sb_implied_pct"], 1)
                row["edge_pp"] = round(pm_pct - cons_pct, 1) if cons_pct is not None else None
                side_str = "+" if target_point > 0 else ""
                row["play"] = f"{target_team} {side_str}{target_point} @ {best['book']} {best['price']:+d}"
                row["bet_stat_key"] = "spread"
                row["bet_team"] = target_team
                row["bet_side"] = ("Home" if tt_norm == _norm_team(game.get("home_team","")) else "Away")
                row["bet_point"] = target_point
                row["best_price"] = best["price"]

        if row["play"] is None:
            row["play"] = "no match for market type"

        enriched.append(row)

    return enriched
