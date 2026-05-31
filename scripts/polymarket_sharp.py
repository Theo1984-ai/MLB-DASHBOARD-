"""
Polymarket sharp-money scanner.

For each currently-open MLB game-level market, compute the bid-side
imbalance within the inside spread. Heavy skew on one side = where the
limit-order book wants to bet at better prices = sharp money.

Strategy:
  - Pull MLB events from Gamma API
  - Filter to game-level markets (no futures)
  - For each candidate (above a volume/liquidity threshold), pull the
    CLOB order book and measure depth within +/-5 cents of the mid
  - Return rows sorted by skew strength
"""
from __future__ import annotations

import json
import ssl as _ssl
import time
import urllib.request
from collections import defaultdict

_SSL = _ssl._create_unverified_context()
_UA = {"User-Agent": "mlb-dashboard/1.0"}

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _get(url):
    req = urllib.request.Request(url, headers=_UA)
    return json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL).read())


# ---------- Market filtering ----------

FUTURES_TOKENS = (
    "world series", "world champ", "mvp", "cy young", "rookie of",
    "division", "clinch", "playoff", "2026 al", "2026 nl",
    "cba", "perfect game", "regular season", "season wins",
    "season hr", "season home run", "season strikeout",
)


def _is_daily_game(title, slug):
    blob = (title + " " + slug).lower()
    if any(b in blob for b in FUTURES_TOKENS):
        return False
    return any(s in blob for s in (" vs.", " vs ", " @ ", " at "))


def _categorize(question, slug):
    q = question.lower()
    s = slug.lower()
    if "nrfi" in s or "first inning" in q or "yrfi" in s:
        return "NRFI / 1st inning"
    if "first 5 innings" in q or "-f5-" in s:
        return "First 5 innings"
    if "o/u" in q or "over/under" in q or "total" in q:
        return "Total"
    if "spread" in q or "run line" in q or "(-1.5)" in q or "(+1.5)" in q:
        return "Run line"
    return "Moneyline"


def _parse_op(m):
    try:
        op = m.get("outcomePrices", "[]")
        return json.loads(op) if isinstance(op, str) else op
    except Exception:
        return [0, 0]


# ---------- Order book metrics ----------

def _book_metrics(token_id, band=0.05):
    """For a single CLOB token, return depth within `band` cents of mid."""
    try:
        ob = _get(f"{CLOB}/book?token_id={token_id}")
    except Exception:
        return None
    bids = [(float(b["price"]), float(b["size"])) for b in ob.get("bids", [])]
    asks = [(float(a["price"]), float(a["size"])) for a in ob.get("asks", [])]
    if not bids or not asks:
        return None
    best_bid = max(bids, key=lambda x: x[0])
    best_ask = min(asks, key=lambda x: x[0])
    mid = (best_bid[0] + best_ask[0]) / 2
    bid_depth = sum(s for p, s in bids if p >= mid - band)
    return {
        "mid": mid,
        "best_bid": best_bid[0],
        "best_ask": best_ask[0],
        "spread": best_ask[0] - best_bid[0],
        "bid_depth_5c": bid_depth,
    }


# ---------- Public scan ----------

def sharp_pick_label(parsed, skew_side):
    """Translate a parsed market + sharp side into the explicit pick.
    Returns e.g. 'Cubs ML', 'OVER 8.5', 'Braves -1.5' — always says what to bet."""
    mt = parsed.get("market_type")
    if mt == "h2h":
        team = parsed.get("away_team") if skew_side == "YES" else parsed.get("home_team")
        return f"{team} ML" if team else f"{skew_side} (team unknown)"
    if mt == "totals":
        pt = parsed.get("point")
        side = "OVER" if skew_side == "YES" else "UNDER"
        return f"{side} {pt}" if pt is not None else side
    if mt == "spreads":
        team = parsed.get("team")
        pt = parsed.get("point") or 0
        # YES = team covers (+/-pt as listed). NO = team does NOT cover.
        if skew_side == "YES":
            sign = "+" if pt > 0 else ""
            return f"{team} {sign}{pt}"
        # For NO, the other side's spread = the inverse
        opp_pt = -pt
        sign = "+" if opp_pt > 0 else ""
        # We don't have the opponent's name in the spread title; describe by relation
        return f"NOT {team} {('+' if pt>0 else '')}{pt}  (other team covers)"
    if mt == "nrfi":
        return "YES = run in 1st" if skew_side == "YES" else "NRFI"
    return skew_side


def parse_market_for_match(question, slug):
    """Decode a Polymarket question into structured fields for sportsbook matching.

    Returns dict with:
      market_type: 'h2h' / 'totals' / 'spreads' / 'nrfi' / 'unknown'
      yes_means:   plain-English description of what YES outcome wins on
      away_team, home_team: best-effort parse (Polymarket convention: "A vs. B" → A=away)
      point: number (for totals + spreads) or None
      team:  team named in spread (for spreads only) or None
    """
    q = question
    out = {"market_type": "unknown", "yes_means": q,
           "away_team": None, "home_team": None,
           "point": None, "team": None}

    # NRFI
    if "first inning" in q.lower():
        out["market_type"] = "nrfi"
        out["yes_means"] = "YES = run scored in 1st inning"
        # Pull teams from the suffix
        if ":" in q:
            sides = q.split(":", 1)[1].strip()
            for sep in (" vs. ", " vs ", " @ "):
                if sep in sides:
                    a, b = sides.split(sep, 1)
                    out["away_team"], out["home_team"] = a.strip(), b.strip()
                    break
        return out

    # Spread
    if q.lower().startswith("spread:"):
        out["market_type"] = "spreads"
        body = q.split(":", 1)[1].strip()
        # "Team Name (-1.5)" or "Team Name (+2.5)"
        if "(" in body and ")" in body:
            team = body.split("(")[0].strip()
            point_str = body[body.index("(")+1:body.index(")")]
            try: point = float(point_str)
            except: point = None
            out["team"] = team
            out["point"] = point
            sign = "+" if (point or 0) > 0 else ""
            out["yes_means"] = f"YES = {team} covers ({sign}{point})"
        return out

    # Totals
    if "o/u" in q.lower() or "over/under" in q.lower() or "total" in q.lower():
        out["market_type"] = "totals"
        # "Team A vs. Team B: O/U 8.5"
        for sep in (" vs. ", " vs ", " @ "):
            if sep in q:
                left, rest = q.split(sep, 1)
                out["away_team"] = left.strip()
                if ":" in rest:
                    right_team, total_part = rest.split(":", 1)
                    out["home_team"] = right_team.strip()
                    # Extract the number
                    import re
                    m = re.search(r"(\d+(?:\.\d+)?)", total_part)
                    if m: out["point"] = float(m.group(1))
                break
        out["yes_means"] = f"YES = OVER {out['point']}" if out["point"] else "YES = OVER"
        return out

    # Moneyline (default)
    for sep in (" vs. ", " vs ", " @ "):
        if sep in q:
            a, b = q.split(sep, 1)
            out["away_team"] = a.strip()
            out["home_team"] = b.strip()
            out["market_type"] = "h2h"
            out["yes_means"] = f"YES = {a.strip()} wins"
            break
    return out


def scan(min_volume=500, min_liquidity=20000, top_n=30, sleep_between=0.15):
    """Returns (rows, debug_stats).

    rows: list of dicts with sharp-money metrics per market
    debug_stats: {'total_events', 'daily_markets', 'candidates', 'with_book'}
    """
    events = _get(f"{GAMMA}/events?closed=false&tag_slug=mlb&limit=200")
    daily_markets = []
    for ev in events:
        title = ev.get("title", "")
        slug = ev.get("slug", "")
        if not _is_daily_game(title, slug):
            continue
        for m in (ev.get("markets") or []):
            if m.get("closed") or not m.get("active", True):
                continue
            m["_event_title"] = title
            m["_event_slug"] = slug
            daily_markets.append(m)

    def _vol(m):
        try: return float(m.get("volume", 0) or 0)
        except: return 0
    def _liq(m):
        try: return float(m.get("liquidity", 0) or 0)
        except: return 0

    candidates = [m for m in daily_markets
                  if _vol(m) > min_volume or _liq(m) > min_liquidity]
    candidates.sort(key=lambda m: -_vol(m))

    rows = []
    for m in candidates[:top_n]:
        ids = m.get("clobTokenIds", "[]")
        try:
            ids = json.loads(ids) if isinstance(ids, str) else ids
        except Exception:
            continue
        if not isinstance(ids, list) or len(ids) < 2:
            continue

        yes_book = _book_metrics(ids[0])
        no_book = _book_metrics(ids[1])
        if not yes_book or not no_book:
            continue

        # YES bid_depth = orders trying to BUY YES (bullish on YES)
        # NO bid_depth = orders trying to BUY NO (bullish on NO)
        yes_bid = yes_book["bid_depth_5c"]
        no_bid = no_book["bid_depth_5c"]
        total = yes_bid + no_bid
        if total < 100:
            continue

        yes_skew_pct = yes_bid / total * 100
        no_skew_pct = no_bid / total * 100

        question = m.get("question") or ""
        slug = m.get("slug") or m.get("_event_slug") or ""
        parsed = parse_market_for_match(question, slug)
        skew_side = "YES" if yes_skew_pct > no_skew_pct else "NO"

        rows.append({
            "event":       m.get("_event_title", ""),
            "question":    question,
            "category":    _categorize(question, slug),
            "sharp_pick":  sharp_pick_label(parsed, skew_side),
            "mid":         round(yes_book["mid"], 3),
            "best_bid":    round(yes_book["best_bid"], 3),
            "best_ask":    round(yes_book["best_ask"], 3),
            "spread":      round(yes_book["spread"], 3),
            "yes_bid_depth": int(yes_bid),
            "no_bid_depth": int(no_bid),
            "yes_skew_pct": round(yes_skew_pct, 1),
            "no_skew_pct":  round(no_skew_pct, 1),
            "skew_side":   "YES" if yes_skew_pct > no_skew_pct else "NO",
            "skew_strength": round(max(yes_skew_pct, no_skew_pct), 1),
            "volume":      round(_vol(m), 2),
            "liquidity":   round(_liq(m), 2),
            "slug":        slug,
            # Sportsbook matching fields
            "match_type":  parsed["market_type"],
            "yes_means":   parsed["yes_means"],
            "away_team":   parsed["away_team"],
            "home_team":   parsed["home_team"],
            "point":       parsed["point"],
            "team":        parsed["team"],
        })
        time.sleep(sleep_between)

    debug = {
        "total_events":   len(events),
        "daily_markets":  len(daily_markets),
        "candidates":     len(candidates),
        "with_book":      len(rows),
    }
    return rows, debug
