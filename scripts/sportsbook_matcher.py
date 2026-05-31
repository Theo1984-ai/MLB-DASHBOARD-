"""
Match Polymarket sharp signals against sportsbook lines from The Odds API.

For a list of Polymarket rows (from polymarket_sharp.scan), pull current
sportsbook game lines and compute the edge between Polymarket and the
best sportsbook price.
"""
from __future__ import annotations

import json
import ssl as _ssl
import urllib.request
from collections import defaultdict

_SSL = _ssl._create_unverified_context()
SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada"


def _amer_to_imp(am):
    if am > 0: return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def _norm_team(name):
    """Polymarket and Odds API sometimes spell teams slightly differently."""
    if not name: return ""
    n = name.lower().strip()
    # Common spelling differences
    n = n.replace("athletics", "athletics")  # both use this
    n = n.replace("a's", "athletics")
    n = n.replace("white sox", "white sox")
    n = n.replace("red sox", "red sox")
    n = n.replace("blue jays", "blue jays")
    return n


def fetch_game_lines(api_key):
    """Pull h2h / spreads / totals across all MLB games at the 5 sharp books."""
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
           f"?apiKey={api_key}&regions=us&markets=h2h,spreads,totals"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception:
        return []


def fetch_alt_lines(api_key, event_id):
    """Pull alternate spreads/totals for one event."""
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets=alternate_spreads,alternate_totals"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception:
        return {}


def _find_best_price(bookmakers, market_key, outcome_filter):
    """Across books, find best (highest) American price matching the filter.
    outcome_filter is a function: outcome_dict -> bool"""
    best = None
    for b in bookmakers:
        for m in b.get("markets", []):
            if m["key"] != market_key: continue
            for o in m.get("outcomes", []):
                if outcome_filter(o):
                    price = o.get("price")
                    if price is None: continue
                    if best is None or price > best["price"]:
                        best = {"price": int(price), "book": b["key"],
                                "name": o.get("name"), "point": o.get("point")}
    return best


def match_signals(polymarket_rows, api_key):
    """For each Polymarket row, find matching sportsbook line + edge."""
    games = fetch_game_lines(api_key)

    # Index games by normalized team pair
    game_by_pair = {}
    for g in games:
        a = _norm_team(g.get("away_team", ""))
        h = _norm_team(g.get("home_team", ""))
        if a and h:
            game_by_pair[(a, h)] = g

    enriched = []
    for r in polymarket_rows:
        row = dict(r)
        row["sb_best_price"] = None
        row["sb_book"] = None
        row["sb_implied_pct"] = None
        row["pm_implied_pct_yes"] = round(r["mid"] * 100, 1)
        row["edge_pp"] = None
        row["play"] = None

        # Polymarket convention: "A vs. B" → A is mentioned first
        # The Odds API: away_team @ home_team
        # Match on TEAM PAIRS, both orderings
        away_pm = _norm_team(r.get("away_team", ""))
        home_pm = _norm_team(r.get("home_team", ""))

        game = (game_by_pair.get((away_pm, home_pm))
                or game_by_pair.get((home_pm, away_pm)))
        if not game:
            # Most common reason: Polymarket lists tomorrow's games but
            # sportsbooks haven't posted lines yet. Communicate clearly.
            row["play"] = "game not on sportsbook board yet"
            enriched.append(row); continue

        books = game.get("bookmakers", [])
        # Did Polymarket name the AWAY team first?
        yes_team_is_away_in_sb = (_norm_team(r.get("away_team","")) ==
                                  _norm_team(game.get("away_team","")))

        sharp_side = r["skew_side"]  # YES or NO

        # ---- ML matching ----
        if r["match_type"] == "h2h":
            # YES = first-named team in Polymarket; need to know if that's
            # the sportsbook's away_team
            if sharp_side == "YES":
                target_team = r["away_team"]  # PM YES team
            else:
                target_team = r["home_team"]  # PM NO team
            tt_norm = _norm_team(target_team)
            best = _find_best_price(books, "h2h",
                lambda o: _norm_team(o.get("name", "")) == tt_norm)
            if best:
                row["sb_best_price"] = best["price"]
                row["sb_book"] = best["book"]
                row["sb_implied_pct"] = round(_amer_to_imp(best["price"]) * 100, 1)
                # Sharp side implied at Polymarket mid:
                pm_pct = (r["mid"] if sharp_side == "YES" else 1 - r["mid"]) * 100
                # Edge = pm_pct - sb_implied (sharps think it's higher than book)
                row["edge_pp"] = round(pm_pct - row["sb_implied_pct"], 1)
                row["play"] = f"{target_team} ML @ {best['book']} {best['price']:+d}"

        # ---- Totals matching ----
        elif r["match_type"] == "totals" and r.get("point") is not None:
            # YES = OVER, NO = UNDER
            target_side = "Over" if sharp_side == "YES" else "Under"
            tgt_pt = r["point"]
            best = _find_best_price(books, "totals",
                lambda o: (o.get("name") == target_side
                           and abs((o.get("point") or 0) - tgt_pt) < 0.01))
            if best:
                row["sb_best_price"] = best["price"]
                row["sb_book"] = best["book"]
                row["sb_implied_pct"] = round(_amer_to_imp(best["price"]) * 100, 1)
                pm_pct = (r["mid"] if sharp_side == "YES" else 1 - r["mid"]) * 100
                row["edge_pp"] = round(pm_pct - row["sb_implied_pct"], 1)
                row["play"] = f"{target_side} {tgt_pt} @ {best['book']} {best['price']:+d}"

        # ---- Spread matching ----
        elif r["match_type"] == "spreads" and r.get("point") is not None and r.get("team"):
            # YES = team covers; NO = other side covers
            if sharp_side == "YES":
                target_team = r["team"]; target_point = r["point"]
            else:
                target_team = r["team"]; target_point = -r["point"]  # other side
            tt_norm = _norm_team(target_team)
            best = _find_best_price(books, "spreads",
                lambda o: (_norm_team(o.get("name","")) == tt_norm
                           and abs((o.get("point") or 0) - target_point) < 0.01))
            if best:
                row["sb_best_price"] = best["price"]
                row["sb_book"] = best["book"]
                row["sb_implied_pct"] = round(_amer_to_imp(best["price"]) * 100, 1)
                pm_pct = (r["mid"] if sharp_side == "YES" else 1 - r["mid"]) * 100
                row["edge_pp"] = round(pm_pct - row["sb_implied_pct"], 1)
                side_str = "+" if target_point > 0 else ""
                row["play"] = f"{target_team} {side_str}{target_point} @ {best['book']} {best['price']:+d}"

        # ---- NRFI ----
        elif r["match_type"] == "nrfi":
            row["play"] = "NRFI not on standard Odds API markets"

        if row["play"] is None:
            row["play"] = "no match for market type"

        enriched.append(row)

    return enriched
