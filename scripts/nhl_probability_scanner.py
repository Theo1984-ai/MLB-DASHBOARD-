"""
NHL True Probability Scanner

Fetches h2h (moneyline), totals (game total), and team_totals for all
upcoming NHL games. Calculates consensus no-vig probability across books
and identifies value edges (best available price vs true probability).

Books used: Fanatics (primary coverage) + FanDuel (sharper lines).
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
SPORT = "icehockey_nhl"
BOOKS = "fanatics,fanduel"
_BOOK_KEYS = ("fanatics", "fanduel")

_SANE_TT = (1.5, 6.0)   # team total sane range
_SANE_GT = (3.5, 8.5)   # game total sane range
_MAX_PRICE = 300         # filter extreme alt-line juice


# ── Core math ─────────────────────────────────────────────────────────────────

def _imp(p):
    """American odds → implied probability (decimal)."""
    if p is None:
        return None
    return abs(p) / (abs(p) + 100) if p < 0 else 100 / (p + 100)


def _nv2(p1, p2):
    """Two-way no-vig. Returns (nv_pct_1, nv_pct_2) as percentages."""
    i1, i2 = _imp(p1), _imp(p2)
    if not i1 or not i2:
        return None, None
    t = i1 + i2
    return round(i1 / t * 100, 1), round(i2 / t * 100, 1)


def _edge(nv_pct, best_price):
    """Edge = true probability minus implied probability of best available price.
    Positive → value (best price is better than no-vig implies it should be)."""
    if nv_pct is None or best_price is None:
        return None
    return round(nv_pct - (_imp(best_price) or 0) * 100, 1)


def _best(prices_dict):
    """Pick (book, price) with highest American odds from {book: price}."""
    valid = [(b, p) for b, p in prices_dict.items() if p is not None]
    if not valid:
        return None, None
    return max(valid, key=lambda x: x[1])


# ── API helpers ───────────────────────────────────────────────────────────────

def _fetch_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/events?apiKey={api_key}"
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception:
        return []


def _fetch_odds(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us"
           f"&markets=h2h,totals,team_totals"
           f"&bookmakers={BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception:
        return {}


# ── Parsers ───────────────────────────────────────────────────────────────────

def _parse_h2h(bookmakers, away, home):
    """Returns {book: {away: price, home: price}}."""
    out = {}
    for bm in bookmakers:
        k = bm.get("key")
        if k not in _BOOK_KEYS:
            continue
        for m in bm.get("markets", []):
            if m.get("key") != "h2h":
                continue
            prices = {o["name"]: o["price"] for o in m.get("outcomes", [])}
            ap, hp = prices.get(away), prices.get(home)
            if ap is not None and hp is not None:
                out[k] = {"away": ap, "home": hp}
    return out


def _parse_totals(bookmakers, sane_min, sane_max, desc=None):
    """
    Parse game totals (desc=None) or one team's total (desc=team name).
    Returns {book: {line, over_price, under_price}} using the best paired line.
    A paired line means the same point exists in both Over and Under outcomes.
    """
    out = {}
    for bm in bookmakers:
        k = bm.get("key")
        if k not in _BOOK_KEYS:
            continue
        for m in bm.get("markets", []):
            mkey = m.get("key")
            if mkey == "team_totals" and desc is None:
                continue
            if mkey == "totals" and desc is not None:
                continue
            if mkey not in ("totals", "team_totals"):
                continue

            overs, unders = {}, {}
            for o in m.get("outcomes", []):
                if desc and o.get("description") != desc:
                    continue
                pt = o.get("point")
                pr = o.get("price")
                name = (o.get("name") or "").lower()
                if pt is None or pr is None:
                    continue
                if not (sane_min <= pt <= sane_max):
                    continue
                if abs(pr) > _MAX_PRICE:
                    continue
                if name == "over":
                    overs[pt] = pr
                elif name == "under":
                    unders[pt] = pr

            paired = [(pt, overs[pt], unders[pt]) for pt in overs if pt in unders]
            if not paired:
                continue
            # Pick the paired line closest to even money
            pt, op, up = min(paired, key=lambda x: abs(x[1]))
            out[k] = {"line": pt, "over_price": op, "under_price": up}
    return out


# ── Market result builders ────────────────────────────────────────────────────

def _totals_result(per_book):
    """
    per_book = {book: {line, over_price, under_price}}
    Consensus no-vig averaged across all books with valid paired lines.
    Primary line = Fanatics if available, else first book.
    """
    if not per_book:
        return None

    over_nvs, under_nvs = [], []
    for bk, d in per_book.items():
        nv_o, nv_u = _nv2(d.get("over_price"), d.get("under_price"))
        if nv_o is not None:
            over_nvs.append(nv_o)
        if nv_u is not None:
            under_nvs.append(nv_u)

    if not over_nvs:
        return None

    primary = per_book.get("fanatics") or next(iter(per_book.values()))
    line = primary["line"]
    cons_over = round(sum(over_nvs) / len(over_nvs), 1)
    cons_under = round(sum(under_nvs) / len(under_nvs), 1) if under_nvs else round(100 - cons_over, 1)

    best_over_bk,  best_over_p  = _best({b: d.get("over_price")  for b, d in per_book.items()})
    best_under_bk, best_under_p = _best({b: d.get("under_price") for b, d in per_book.items()})

    return {
        "line":            line,
        "over_nv":         cons_over,
        "under_nv":        cons_under,
        "best_over_price": best_over_p,
        "best_over_book":  best_over_bk,
        "best_under_price":best_under_p,
        "best_under_book": best_under_bk,
        "over_edge":       _edge(cons_over,  best_over_p),
        "under_edge":      _edge(cons_under, best_under_p),
        "n_books":         len(per_book),
        "books":           per_book,
    }


def _ml_result(per_book):
    """per_book = {book: {away: price, home: price}}"""
    if not per_book:
        return None

    away_nvs, home_nvs = [], []
    for bk, d in per_book.items():
        nv_a, nv_h = _nv2(d.get("away"), d.get("home"))
        if nv_a is not None:
            away_nvs.append(nv_a)
        if nv_h is not None:
            home_nvs.append(nv_h)

    if not away_nvs:
        return None

    cons_away = round(sum(away_nvs) / len(away_nvs), 1)
    cons_home = round(100 - cons_away, 1)

    best_away_bk, best_away_p = _best({b: d.get("away") for b, d in per_book.items()})
    best_home_bk, best_home_p = _best({b: d.get("home") for b, d in per_book.items()})

    return {
        "away_nv":          cons_away,
        "home_nv":          cons_home,
        "best_away_price":  best_away_p,
        "best_away_book":   best_away_bk,
        "best_home_price":  best_home_p,
        "best_home_book":   best_home_bk,
        "away_edge":        _edge(cons_away, best_away_p),
        "home_edge":        _edge(cons_home, best_home_p),
        "n_books":          len(per_book),
        "books":            per_book,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def scan(api_key):
    """
    Returns (games, debug).

    games: list of dicts, one per upcoming game:
      game, away_team, home_team, commence_time,
      ml      → moneyline result or None
      gt      → game total result or None
      away_tt → away team total result or None
      home_tt → home team total result or None
    """
    now_utc = datetime.now(timezone.utc)
    events = _fetch_events(api_key)
    dbg = {"n_events": len(events), "n_upcoming": 0, "n_games": 0, "errors": []}

    upcoming = [
        e for e in events
        if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) > now_utc
    ]
    dbg["n_upcoming"] = len(upcoming)

    games = []
    for ev in upcoming:
        away = ev["away_team"]
        home = ev["home_team"]
        try:
            data = _fetch_odds(api_key, ev["id"])
            bms  = data.get("bookmakers", [])

            game = {
                "game":         f"{away} @ {home}",
                "away_team":    away,
                "home_team":    home,
                "commence_time": ev["commence_time"],
                "ml":      _ml_result(_parse_h2h(bms, away, home)),
                "gt":      _totals_result(_parse_totals(bms, *_SANE_GT)),
                "away_tt": _totals_result(_parse_totals(bms, *_SANE_TT, desc=away)),
                "home_tt": _totals_result(_parse_totals(bms, *_SANE_TT, desc=home)),
            }
            if any(game[k] for k in ("ml", "gt", "away_tt", "home_tt")):
                games.append(game)
        except Exception as e:
            dbg["errors"].append(f"{away}@{home}: {e}")

    dbg["n_games"] = len(games)
    return games, dbg


if __name__ == "__main__":
    import tomllib
    with open(os.path.join(ROOT, ".streamlit", "secrets.toml"), "rb") as f:
        KEY = tomllib.load(f)["THE_ODDS_API_KEY"]
    games, dbg = scan(KEY)
    print(dbg)
    for g in games:
        print(f"\n{g['game']}")
        if g["ml"]:
            ml = g["ml"]
            print(f"  ML:  {g['away_team']} {ml['away_nv']}% (best {ml['best_away_price']:+d} @{ml['best_away_book']}, edge {ml['away_edge']:+.1f}pp)")
            print(f"       {g['home_team']} {ml['home_nv']}% (best {ml['best_home_price']:+d} @{ml['best_home_book']}, edge {ml['home_edge']:+.1f}pp)")
        if g["gt"]:
            gt = g["gt"]
            print(f"  GT {gt['line']}: Over {gt['over_nv']}% (best {gt['best_over_price']:+d}, edge {gt['over_edge']:+.1f})  Under {gt['under_nv']}% (best {gt['best_under_price']:+d}, edge {gt['under_edge']:+.1f})")
        for side, key in (("away", "away_tt"), ("home", "home_tt")):
            tt = g.get(key)
            if tt:
                team = g[f"{side}_team"]
                print(f"  TT {team} {tt['line']}: Over {tt['over_nv']}% (best {tt['best_over_price']:+d}, edge {tt['over_edge']:+.1f})  Under {tt['under_nv']}% (best {tt['best_under_price']:+d}, edge {tt['under_edge']:+.1f})")
