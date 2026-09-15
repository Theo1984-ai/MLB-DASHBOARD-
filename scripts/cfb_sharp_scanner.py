"""
CFB Sharp Money scanner via Odds API.

Detects sharp signals by comparing lines across books:
  - Off-market line: one book significantly differs from consensus (sharps already moved it)
  - Juice imbalance: book pricing one side cheaper = taking sharp action on that side
  - Steam: DK and FD both moved but slower books haven't caught up

Sport: americanfootball_ncaaf
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
SHARP_BOOKS = ["draftkings", "fanduel", "betmgm", "williamhill_us", "bovada", "betonlineag"]
BOOK_LABELS = {
    "draftkings":    "DK",
    "fanduel":       "FD",
    "betmgm":        "MGM",
    "williamhill_us": "CZR",
    "bovada":        "BOV",
    "betonlineag":   "BOL",
}

OFF_MARKET_SPREAD_THRESHOLD = 1.5   # pts — book differs from consensus by this much
OFF_MARKET_TOTAL_THRESHOLD  = 1.5   # pts
JUICE_IMBALANCE_THRESHOLD   = 15    # cents — one side priced 15+ cheaper than other
STEAM_DIFF_THRESHOLD        = 1.0   # pts — lead books vs lag books differ by this much


def _fetch(api_key, markets="spreads,totals,h2h"):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
           f"?apiKey={api_key}&regions=us"
           f"&markets={markets}"
           f"&bookmakers={','.join(SHARP_BOOKS)}"
           f"&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception as e:
        print(f"ERROR: {e}")
        return []


def _consensus(values):
    if not values:
        return None
    return sum(values) / len(values)


def _juice_sharp_side(outcomes):
    """Return the side where sharp money went (the expensive side).

    When sharps hammer one side, books move that side's price up (-110 → -130)
    and sweeten the other side (+110) to attract balancing bets.
    The EXPENSIVE side (-130) is where sharp money is.
    """
    if len(outcomes) < 2:
        return None, None
    prices = [(o.get("name", ""), o.get("price", 0), o.get("point")) for o in outcomes]
    sorted_p = sorted(prices, key=lambda x: abs(x[1]))
    cheap_side     = sorted_p[0]   # book wants bets here to balance
    expensive_side = sorted_p[-1]  # sharp money came in here
    imbalance = abs(expensive_side[1]) - abs(cheap_side[1])
    if imbalance >= JUICE_IMBALANCE_THRESHOLD:
        return expensive_side[0], imbalance  # sharp side is the expensive one
    return None, None


def scan(api_key):
    events = _fetch(api_key)
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

        away = ev.get("away_team", "?")
        home = ev.get("home_team", "?")
        game = f"{away} @ {home}"
        fp   = ev.get("commence_time", "")

        book_data = {}
        for bm in ev.get("bookmakers", []):
            key = bm.get("key")
            if key not in SHARP_BOOKS:
                continue
            book_data[key] = {}
            for mkt in bm.get("markets", []):
                mk = mkt.get("key")
                book_data[key][mk] = mkt.get("outcomes", [])

        if len(book_data) < 3:
            continue

        # ── Spread analysis ──
        spread_lines = {}
        for bk, mkts in book_data.items():
            for o in mkts.get("spreads", []):
                if o.get("name") == away:
                    spread_lines[bk] = o.get("point")

        if len(spread_lines) >= 3:
            vals = [v for v in spread_lines.values() if v is not None]
            cons = _consensus(vals)
            if cons is not None:
                # Off-market check
                for bk, line in spread_lines.items():
                    if line is None:
                        continue
                    diff = abs(line - cons)
                    if diff >= OFF_MARKET_SPREAD_THRESHOLD:
                        direction = "gave more pts" if line > cons else "taking fewer pts"
                        sharp_side = away if line < cons else home
                        plays.append({
                            "game":       game,
                            "first_pitch": fp,
                            "signal":     "🔴 Off-market spread",
                            "sharp_pick": f"{sharp_side} spread",
                            "detail":     f"{BOOK_LABELS.get(bk, bk)} at {line:+.1f} vs consensus {cons:+.1f} ({direction})",
                            "strength":   round(diff, 1),
                            "book":       BOOK_LABELS.get(bk, bk),
                            "market":     "Spread",
                        })

                # Steam check: sharp books (DK/FD) vs lag books
                sharp_bks = {b: v for b, v in spread_lines.items()
                             if b in ("draftkings", "fanduel") and v is not None}
                lag_bks   = {b: v for b, v in spread_lines.items()
                             if b not in ("draftkings", "fanduel") and v is not None}
                if sharp_bks and lag_bks:
                    sharp_avg = _consensus(list(sharp_bks.values()))
                    lag_avg   = _consensus(list(lag_bks.values()))
                    if sharp_avg is not None and lag_avg is not None:
                        steam_diff = abs(sharp_avg - lag_avg)
                        if steam_diff >= STEAM_DIFF_THRESHOLD:
                            sharp_side = away if sharp_avg < lag_avg else home
                            plays.append({
                                "game":        game,
                                "first_pitch": fp,
                                "signal":      "⚡ Steam move",
                                "sharp_pick":  f"{sharp_side} spread",
                                "detail":      f"DK/FD avg {sharp_avg:+.1f} vs lag books {lag_avg:+.1f} (Δ {steam_diff:.1f})",
                                "strength":    round(steam_diff, 1),
                                "book":        "DK+FD",
                                "market":      "Spread",
                            })

            # Juice imbalance per book
            for bk, mkts in book_data.items():
                outs = mkts.get("spreads", [])
                side, imbalance = _juice_sharp_side(outs)
                if side:
                    plays.append({
                        "game":        game,
                        "first_pitch": fp,
                        "signal":      "💧 Juice imbalance",
                        "sharp_pick":  f"{side} spread",
                        "detail":      f"{BOOK_LABELS.get(bk, bk)}: {side} is the expensive side ({imbalance} cent gap) — sharps bet this",
                        "strength":    round(imbalance, 0),
                        "book":        BOOK_LABELS.get(bk, bk),
                        "market":      "Spread",
                    })

        # ── Total analysis ──
        total_lines = {}
        for bk, mkts in book_data.items():
            for o in mkts.get("totals", []):
                if (o.get("name") or "").lower() == "over":
                    total_lines[bk] = o.get("point")

        if len(total_lines) >= 3:
            vals = [v for v in total_lines.values() if v is not None]
            cons = _consensus(vals)
            if cons is not None:
                for bk, line in total_lines.items():
                    if line is None:
                        continue
                    diff = abs(line - cons)
                    if diff >= OFF_MARKET_TOTAL_THRESHOLD:
                        sharp_side = "Under" if line > cons else "Over"
                        plays.append({
                            "game":        game,
                            "first_pitch": fp,
                            "signal":      "🔴 Off-market total",
                            "sharp_pick":  f"{sharp_side} {line}",
                            "detail":      f"{BOOK_LABELS.get(bk, bk)} at {line} vs consensus {cons:.1f}",
                            "strength":    round(diff, 1),
                            "book":        BOOK_LABELS.get(bk, bk),
                            "market":      "Total",
                        })

            for bk, mkts in book_data.items():
                outs = mkts.get("totals", [])
                side, imbalance = _juice_sharp_side(outs)
                if side:
                    plays.append({
                        "game":        game,
                        "first_pitch": fp,
                        "signal":      "💧 Juice imbalance",
                        "sharp_pick":  f"{side} total",
                        "detail":      f"{BOOK_LABELS.get(bk, bk)}: {side} is the expensive side ({imbalance} cent gap) — sharps bet this",
                        "strength":    round(imbalance, 0),
                        "book":        BOOK_LABELS.get(bk, bk),
                        "market":      "Total",
                    })

    # Sort: off-market first, then steam, then juice; within each by strength desc
    signal_rank = {"🔴 Off-market spread": 0, "🔴 Off-market total": 0,
                   "⚡ Steam move": 1, "💧 Juice imbalance": 2}
    plays.sort(key=lambda p: (signal_rank.get(p["signal"], 9), -p["strength"]))

    # Deduplicate same game+signal+market+book
    seen = set()
    unique = []
    for p in plays:
        k = (p["game"], p["signal"], p["market"], p["book"])
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique
