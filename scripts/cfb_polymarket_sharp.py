"""
CFB Polymarket sharp-money scanner.
Mirrors nfl_polymarket_sharp.py — uses college-football tag on Gamma.
"""
from __future__ import annotations

import json
import ssl as _ssl
import time
import urllib.request
from collections import defaultdict

_SSL = _ssl._create_unverified_context()
_UA = {"User-Agent": "cfb-dashboard/1.0"}

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"


def _get(url):
    req = urllib.request.Request(url, headers=_UA)
    return json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL).read())


FUTURES_TOKENS = (
    "college football playoff", "cfp", "national championship", "heisman",
    "win the big", "win the sec", "win the acc", "win the big ten", "win the pac",
    "win the big 12", "win the mountain west", "conference champion",
    "make the playoff", "go undefeated", "bowl game", "season wins",
    "coach of the year", "player of the year",
)


def _is_weekly_game(title, slug):
    blob = (title + " " + slug).lower()
    if any(b in blob for b in FUTURES_TOKENS):
        return False
    return any(s in blob for s in (" vs.", " vs ", " @ ", " at "))


def _categorize(question, slug):
    q = question.lower()
    s = slug.lower()
    if "first half" in q or "1st half" in q or "-1h-" in s:
        return "1st Half"
    if "first quarter" in q or "1st quarter" in q or "-1q-" in s:
        return "1st Quarter"
    if "touchdown" in q or " td " in q:
        return "Player TD"
    if "o/u" in q or "over/under" in q or "total" in q:
        return "Total"
    if "spread" in q or "(-" in q or "(+" in q:
        return "Spread"
    return "Moneyline"


def _parse_op(m):
    try:
        op = m.get("outcomePrices", "[]")
        return json.loads(op) if isinstance(op, str) else op
    except Exception:
        return [0, 0]


def _book_metrics(token_id, band=0.05):
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
    in_band = [(p, s) for p, s in bids if p >= mid - band]
    bid_depth = sum(s for _, s in in_band)
    n_bids = len(in_band)
    largest_bid = max((s for _, s in in_band), default=0.0)
    whale_share = (largest_bid / bid_depth) if bid_depth > 0 else 0.0
    return {
        "mid": mid,
        "best_bid": best_bid[0],
        "best_ask": best_ask[0],
        "spread": best_ask[0] - best_bid[0],
        "bid_depth_5c": bid_depth,
        "n_bids": n_bids,
        "largest_bid": largest_bid,
        "whale_share": whale_share,
    }


def _short_matchup(parsed):
    a = parsed.get("away_team") or ""
    h = parsed.get("home_team") or ""
    a_short = a.split()[-1] if a else "?"
    h_short = h.split()[-1] if h else "?"
    return f"{a_short} @ {h_short}" if a and h else ""


def sharp_pick_label(parsed, skew_side):
    mt = parsed.get("market_type")
    if mt == "h2h":
        team = parsed.get("away_team") if skew_side == "YES" else parsed.get("home_team")
        return f"{team} ML" if team else f"{skew_side} (team unknown)"
    if mt == "totals":
        pt = parsed.get("point")
        side = "OVER" if skew_side == "YES" else "UNDER"
        match = _short_matchup(parsed)
        body = f"{side} {pt}" if pt is not None else side
        return f"{body} ({match})" if match else body
    if mt == "spreads":
        team = parsed.get("team")
        pt = parsed.get("point") or 0
        if skew_side == "YES":
            sign = "+" if pt > 0 else ""
            return f"{team} {sign}{pt}"
        return f"NOT {team} {('+' if pt>0 else '')}{pt}  (other team covers)"
    return skew_side


def parse_market_for_match(question, slug):
    import re
    q = question
    out = {"market_type": "unknown", "yes_means": q,
           "away_team": None, "home_team": None,
           "point": None, "team": None, "player": None}

    if "touchdown" in q.lower() or " td " in q.lower():
        out["market_type"] = "player_td"
        out["yes_means"] = f"YES = {q}"
        return out

    if q.lower().startswith("spread:"):
        out["market_type"] = "spreads"
        body = q.split(":", 1)[1].strip()
        if "(" in body and ")" in body:
            team = body.split("(")[0].strip()
            point_str = body[body.index("(")+1:body.index(")")]
            try:
                point = float(point_str)
            except Exception:
                point = None
            out["team"] = team
            out["point"] = point
            sign = "+" if (point or 0) > 0 else ""
            out["yes_means"] = f"YES = {team} covers ({sign}{point})"
        return out

    if "o/u" in q.lower() or "over/under" in q.lower() or "total" in q.lower():
        out["market_type"] = "totals"
        for sep in (" vs. ", " vs ", " @ "):
            if sep in q:
                left, rest = q.split(sep, 1)
                out["away_team"] = left.strip()
                if ":" in rest:
                    right_team, total_part = rest.split(":", 1)
                    out["home_team"] = right_team.strip()
                    m = re.search(r"(\d+(?:\.\d+)?)", total_part)
                    if m:
                        out["point"] = float(m.group(1))
                break
        out["yes_means"] = f"YES = OVER {out['point']}" if out["point"] else "YES = OVER"
        return out

    for sep in (" vs. ", " vs ", " @ "):
        if sep in q:
            a, b = q.split(sep, 1)
            out["away_team"] = a.strip()
            out["home_team"] = b.strip()
            out["market_type"] = "h2h"
            out["yes_means"] = f"YES = {a.strip()} wins"
            break
    return out


def _parse_game_start(market):
    from datetime import datetime
    gs = market.get("gameStartTime") or ""
    if not gs:
        return None
    try:
        gs = gs.replace(" ", "T")
        if gs.endswith("+00"):
            gs = gs[:-3] + "+00:00"
        return datetime.fromisoformat(gs)
    except Exception:
        return None


def scan(min_volume=200, min_liquidity=10000, top_n=30, sleep_between=0.15,
         week_only=True, skip_started=True, started_grace_min=5):
    """Returns (rows, debug_stats)."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    now_utc = datetime.now(tz=timezone.utc)
    today_et = datetime.now(tz=ET)
    days_since_monday = today_et.weekday()
    week_start = (today_et - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=8)
    started_cutoff = now_utc + timedelta(minutes=started_grace_min)

    # Try college-football tag; fall back to ncaaf
    weekly_markets = []
    filtered_future = 0
    filtered_started = 0
    events = []
    for tag in ("college-football", "ncaaf", "ncaa-football"):
        try:
            result = _get(f"{GAMMA}/events?closed=false&tag_slug={tag}&limit=200")
            if result:
                events = result
                break
        except Exception:
            continue

    for ev in events:
        title = ev.get("title", "")
        slug = ev.get("slug", "")
        if not _is_weekly_game(title, slug):
            continue
        for m in (ev.get("markets") or []):
            if m.get("closed") or not m.get("active", True):
                continue
            game_dt = _parse_game_start(m)
            if week_only:
                if game_dt is None:
                    filtered_future += 1
                    continue
                game_dt_et = game_dt.astimezone(ET)
                if not (week_start <= game_dt_et <= week_end):
                    filtered_future += 1
                    continue
                m["_game_start"] = game_dt.isoformat()
            if skip_started and game_dt is not None:
                if game_dt < started_cutoff - timedelta(minutes=started_grace_min):
                    filtered_started += 1
                    continue
            m["_event_title"] = title
            m["_event_slug"] = slug
            weekly_markets.append(m)

    def _vol(m):
        try: return float(m.get("volume", 0) or 0)
        except: return 0

    def _liq(m):
        try: return float(m.get("liquidity", 0) or 0)
        except: return 0

    candidates = [m for m in weekly_markets
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
        no_book  = _book_metrics(ids[1])
        if not yes_book or not no_book:
            continue

        yes_bid = yes_book["bid_depth_5c"]
        no_bid  = no_book["bid_depth_5c"]
        total   = yes_bid + no_bid
        if total < 100:
            continue

        yes_skew_pct = yes_bid / total * 100
        no_skew_pct  = no_bid  / total * 100

        question = m.get("question") or ""
        slug = m.get("slug") or m.get("_event_slug") or ""
        parsed    = parse_market_for_match(question, slug)
        skew_side = "YES" if yes_skew_pct > no_skew_pct else "NO"

        sharp_book        = yes_book if skew_side == "YES" else no_book
        sharp_n_bids      = sharp_book.get("n_bids", 0)
        sharp_largest     = sharp_book.get("largest_bid", 0)
        sharp_whale_share = sharp_book.get("whale_share", 0)

        rows.append({
            "event":         m.get("_event_title", ""),
            "question":      question,
            "category":      _categorize(question, slug),
            "sharp_pick":    sharp_pick_label(parsed, skew_side),
            "game_start":    m.get("_game_start"),
            "mid":           round(yes_book["mid"], 3),
            "best_bid":      round(yes_book["best_bid"], 3),
            "best_ask":      round(yes_book["best_ask"], 3),
            "spread":        round(yes_book["spread"], 3),
            "yes_bid_depth": int(yes_bid),
            "no_bid_depth":  int(no_bid),
            "yes_skew_pct":  round(yes_skew_pct, 1),
            "no_skew_pct":   round(no_skew_pct, 1),
            "skew_side":     skew_side,
            "skew_strength": round(max(yes_skew_pct, no_skew_pct), 1),
            "volume":        round(_vol(m), 2),
            "liquidity":     round(_liq(m), 2),
            "slug":          slug,
            "sharp_n_bids":       sharp_n_bids,
            "sharp_largest_bid":  int(sharp_largest),
            "sharp_whale_share":  round(sharp_whale_share, 3),
            "yes_n_bids":         yes_book.get("n_bids", 0),
            "yes_whale_share":    round(yes_book.get("whale_share", 0), 3),
            "no_n_bids":          no_book.get("n_bids", 0),
            "no_whale_share":     round(no_book.get("whale_share", 0), 3),
            "match_type":    parsed["market_type"],
            "yes_means":     parsed["yes_means"],
            "away_team":     parsed["away_team"],
            "home_team":     parsed["home_team"],
            "point":         parsed["point"],
            "team":          parsed["team"],
        })
        time.sleep(sleep_between)

    debug = {
        "total_events":           len(events),
        "weekly_markets":         len(weekly_markets),
        "candidates":             len(candidates),
        "with_book":              len(rows),
        "filtered_future_games":  filtered_future,
        "filtered_started_games": filtered_started,
    }
    return rows, debug
