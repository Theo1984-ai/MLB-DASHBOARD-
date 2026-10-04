"""
Polymarket sharp-money scanner — NHL edition.

Mirrors polymarket_sharp.py (MLB) exactly, swapping in NHL-specific
futures token filters and market categories. The core order-book logic
and scan() interface are unchanged.
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
CLOB  = "https://clob.polymarket.com"


def _get(url):
    req = urllib.request.Request(url, headers=_UA)
    return json.loads(urllib.request.urlopen(req, timeout=20, context=_SSL).read())


# ---------- Market filtering ----------

FUTURES_TOKENS = (
    "stanley cup", "cup champion", "hart trophy", "vezina", "norris",
    "calder", "conn smythe", "rocket richard", "playoff", "president",
    "regular season", "season goals", "season wins", "win the cup",
)


def _is_daily_game(title, slug):
    blob = (title + " " + slug).lower()
    if any(b in blob for b in FUTURES_TOKENS):
        return False
    return any(s in blob for s in (" vs.", " vs ", " @ ", " at "))


def _categorize(question, slug):
    q = question.lower()
    s = slug.lower()
    if "first period" in q or "1st period" in q or "-p1-" in s:
        return "1st period"
    if "o/u" in q or "over/under" in q or "total" in q:
        return "Total"
    if "puck line" in q or "(-1.5)" in q or "(+1.5)" in q:
        return "Puck line"
    return "Moneyline"


def _parse_op(m):
    try:
        op = m.get("outcomePrices", "[]")
        return json.loads(op) if isinstance(op, str) else op
    except Exception:
        return [0, 0]


# ---------- Order book metrics (identical to MLB version) ----------

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
        "mid":         mid,
        "best_bid":    best_bid[0],
        "best_ask":    best_ask[0],
        "spread":      best_ask[0] - best_bid[0],
        "bid_depth_5c": bid_depth,
        "n_bids":       n_bids,
        "largest_bid":  largest_bid,
        "whale_share":  whale_share,
    }


# ---------- Public scan ----------

def _short_matchup(parsed):
    a = parsed.get("away_team") or ""
    h = parsed.get("home_team") or ""
    a_short = a.split()[-1] if a else "?"
    h_short = h.split()[-1] if h else "?"
    return f"{a_short} @ {h_short}"


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
    q = question
    out = {"market_type": "unknown", "yes_means": q,
           "away_team": None, "home_team": None,
           "point": None, "team": None}
    q_l = q.lower()
    # Total
    import re
    tot = re.search(r"over[/ ]under\s+(\d+\.?\d*)", q_l) or re.search(r"total\s+(\d+\.?\d*)", q_l)
    if tot or "o/u" in q_l:
        out["market_type"] = "totals"
        if tot:
            out["point"] = float(tot.group(1))
        out["yes_means"] = f"OVER {out['point']}"
        for sep in (" vs. ", " vs ", " @ ", " at "):
            if sep in q:
                a, b = q.split(sep, 1)
                out["away_team"] = a.strip().split(":")[-1].strip()
                out["home_team"] = b.strip().split("?")[0].strip()
                break
        return out
    # Puck line spread
    spread = re.search(r"([\w\s]+)\s*\(([+-]?\d+\.?\d*)\)", q)
    if spread and ("puck line" in q_l or "(-1.5)" in q_l or "(+1.5)" in q_l):
        out["market_type"] = "spreads"
        out["team"] = spread.group(1).strip()
        out["point"] = float(spread.group(2))
        out["yes_means"] = f"{out['team']} covers {out['point']}"
        return out
    # Moneyline — parse teams
    for sep in (" vs. ", " vs ", " @ ", " at "):
        if sep in q:
            a, b = q.split(sep, 1)
            out["away_team"] = a.strip().split(":")[-1].strip()
            out["home_team"] = b.strip().split("?")[0].strip()
            out["market_type"] = "h2h"
            out["yes_means"] = f"{out['away_team']} win"
            break
    return out


def scan(min_volume=500, min_liquidity=10000, top_n=50):
    """
    Scan Polymarket for open NHL game markets and compute bid-side imbalance.

    Returns (rows, debug) matching the interface of polymarket_sharp.scan().
    """
    debug = {"total_events": 0, "daily_markets": 0, "candidates": 0, "with_book": 0}

    # Pull NHL-tagged events from Gamma
    try:
        events = _get(f"{GAMMA}/events?tag_slug=nhl&limit=100&active=true&closed=false")
    except Exception:
        events = []

    if not events:
        # Broader search: pull by category
        try:
            events = _get(f"{GAMMA}/events?tag_slug=sports&limit=200&active=true&closed=false")
        except Exception:
            events = []

    debug["total_events"] = len(events)

    # Flatten all markets from all events
    all_markets = []
    for ev in events:
        for m in ev.get("markets", [ev]) if "markets" in ev else [ev]:
            title = m.get("title") or m.get("question") or ""
            slug  = m.get("slug") or ""
            if not _is_daily_game(title, slug):
                continue
            vol = float(m.get("volume") or 0)
            liq = float(m.get("liquidity") or 0)
            if vol < min_volume:
                continue
            all_markets.append({**m, "_title": title, "_slug": slug,
                                 "_vol": vol, "_liq": liq})

    debug["daily_markets"] = len(all_markets)

    # Sort by volume and take top_n
    all_markets.sort(key=lambda m: -m["_vol"])
    candidates = all_markets[:top_n]
    debug["candidates"] = len(candidates)

    rows = []
    for m in candidates:
        title    = m["_title"]
        liq      = m["_liq"]
        category = _categorize(title, m["_slug"])
        parsed   = parse_market_for_match(title, m["_slug"])
        parsed["match_type"] = parsed.get("market_type")

        # Gamma API stores clobTokenIds as a JSON string: '["id1","id2"]'
        # index 0 = Yes token, index 1 = No token (Polymarket convention)
        raw_ids = m.get("clobTokenIds", "[]")
        try:
            cid_list = json.loads(raw_ids) if isinstance(raw_ids, str) else (raw_ids or [])
        except Exception:
            cid_list = []

        if len(cid_list) < 2:
            continue

        yes_id = str(cid_list[0])
        no_id  = str(cid_list[1])
        if not yes_id or not no_id:
            continue

        yes_m = _book_metrics(yes_id)
        if not yes_m:
            continue
        no_m = _book_metrics(no_id)
        if not no_m:
            continue

        time.sleep(0.05)
        debug["with_book"] += 1

        yes_depth = yes_m["bid_depth_5c"]
        no_depth  = no_m["bid_depth_5c"]
        total_depth = yes_depth + no_depth
        if total_depth <= 0:
            continue

        yes_skew = yes_depth / total_depth * 100
        no_skew  = no_depth  / total_depth * 100
        skew_side   = "YES" if yes_skew >= no_skew else "NO"
        skew_strength = yes_skew if skew_side == "YES" else no_skew

        sharp_whale = yes_m["whale_share"] if skew_side == "YES" else no_m["whale_share"]
        sharp_n     = yes_m["n_bids"]      if skew_side == "YES" else no_m["n_bids"]

        ops = _parse_op(m)
        mid_yes = float(ops[0]) if ops else yes_m["mid"]

        sharp_pick = sharp_pick_label(parsed, skew_side)

        row = {
            **parsed,
            "event":            title,
            "category":         category,
            "skew_side":        skew_side,
            "skew_strength":    round(skew_strength, 1),
            "yes_bid_depth":    round(yes_depth, 0),
            "no_bid_depth":     round(no_depth, 0),
            "mid":              round(mid_yes, 4),
            "spread":           round(yes_m["spread"], 4),
            "volume":           m["_vol"],
            "liquidity":        liq,
            "sharp_pick":       sharp_pick,
            "sharp_whale_share": sharp_whale,
            "sharp_n_bids":     sharp_n,
        }
        rows.append(row)

    rows.sort(key=lambda r: -r["skew_strength"])
    return rows, debug
