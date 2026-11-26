"""
Market flow: where the real money is going on each NFL game's moneyline, spread and total (Polymarket trades).

For every market the trades are split by the side that was bet, and for each side we work out:

  cash %     share of all the dollars traded on the market that went to this side
  orders %   share of all the trades (orders) on the market that went to this side
  delta      cash % minus orders %. Positive = this side's orders are bigger than average = a few large bettors
  avg order  dollars per trade on this side
  top wallet the biggest single wallet's share of this side's dollars (how concentrated the money is)

The side with the biggest positive delta (enough dollars and trades behind it) is the "heaviest side". A BUY of an outcome is a
bet on it; a SELL of an outcome is counted as a bet on the other outcome (cost = size x (1 - price)).

Run in GitHub Actions (Polymarket is reachable there, not from the cloud sandbox):

    python -m scripts.market_flow [NFL]

It prints a text report for each game and saves best_plays_log/market_flow/<date>/<SPORT>_<hhmm>.json. If a trades call fails or has
an unexpected shape, the first raw trade is printed so the parser can be fixed from real data.
"""
import gzip
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
TAGS = {"NFL": "nfl", "NBA": "nba", "NHL": "nhl", "CFB": "college-football"}
MIN_SIDE_CASH = 5_000          # a side needs this many dollars ...
MIN_SIDE_ORDERS = 20           # ... and this many trades behind it to count as the heaviest side
MAX_TRADES = 2500              # newest trades read per market
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mlb-dashboard/1.0)", "Accept": "application/json",
            "Accept-Encoding": "gzip"}
_CTX = ssl.create_default_context()


# ── Fetching ─────────────────────────────────────────────────────────────────

def _get(url):
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=30, context=_CTX)
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return json.loads(body)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {e.read()[:200]!r}") from e


def _json(v, default):
    try:
        return json.loads(v) if isinstance(v, str) else (v if v is not None else default)
    except Exception:  # noqa: BLE001
        return default


def fetch_events(sport):
    return _get(f"{GAMMA}/events?closed=false&tag_slug={TAGS[sport]}&limit=100")


def fetch_trades(condition_id, max_trades=MAX_TRADES, page=500):
    """Newest trades of one market (Polymarket data API), newest first."""
    out = []
    for off in range(0, max_trades, page):
        batch = _get(f"{DATA}/trades?market={condition_id}&limit={page}&offset={off}")
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < page:
            break
    return out


# ── Markets ──────────────────────────────────────────────────────────────────

def classify(question, outcomes):
    """'Total', 'Spread' or 'Moneyline' from the market's question and outcome labels."""
    q = (question or "").lower()
    outs = [str(o).lower() for o in outcomes]
    if outs == ["yes", "no"]:                      # a prop or a yes/no question, not a game market
        return "Other"
    if "over" in outs and "under" in outs or "o/u" in q or "over/under" in q:
        return "Total"
    if "spread" in q or any(re.search(r"[+-]\d+(\.\d+)?\)?\s*$", o) for o in outs):
        return "Spread"
    return "Moneyline"


def american(p):
    """American odds from a probability (0-1)."""
    if not p or p <= 0 or p >= 1:
        return None
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def _vol(m):
    try:
        return float(m.get("volume") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def event_markets(event):
    """{'Moneyline'|'Spread'|'Total': the highest-volume market of that kind} for one Gamma event (a game)."""
    best = {}
    for m in event.get("markets") or []:
        outs = _json(m.get("outcomes"), [])
        if len(outs) != 2 or not m.get("conditionId"):
            continue
        kind = classify(m.get("question"), outs)
        if kind == "Other":
            continue
        if kind not in best or _vol(m) > _vol(best[kind]):
            best[kind] = m
    return best


# ── The math ─────────────────────────────────────────────────────────────────

def bet_side(trade, outcomes):
    """(outcome bet on, dollars) for one trade, or None if it can't be read. outcomes = the market's two labels."""
    try:
        size, price = float(trade["size"]), float(trade["price"])
    except (KeyError, TypeError, ValueError):
        return None
    name = trade.get("outcome")
    if name not in outcomes:
        idx = trade.get("outcomeIndex")
        if not isinstance(idx, int) or not 0 <= idx < len(outcomes):
            return None
        name = outcomes[idx]
    if str(trade.get("side", "BUY")).upper() == "SELL":
        other = [o for o in outcomes if o != name]
        if len(other) != 1:
            return None
        return other[0], size * (1 - price)
    return name, size * price


def side_stats(trades, outcomes):
    """
    [{name, cash, orders, cash_pct, orders_pct, delta, avg, top_wallet_pct}] for the market's two outcomes, from raw trades.
    delta = cash_pct - orders_pct (percentage points).
    """
    cash, orders = defaultdict(float), defaultdict(int)
    wallets = defaultdict(lambda: defaultdict(float))
    for t in trades:
        b = bet_side(t, outcomes)
        if not b:
            continue
        name, usd = b
        cash[name] += usd
        orders[name] += 1
        w = t.get("proxyWallet") or t.get("maker") or t.get("user")
        if w:
            wallets[name][w] += usd
    tc, to = sum(cash.values()), sum(orders.values())
    out = []
    for name in outcomes:
        c, n = cash[name], orders[name]
        cp = 100 * c / tc if tc else 0.0
        op = 100 * n / to if to else 0.0
        top = max(wallets[name].values()) / c * 100 if wallets[name] and c else None
        out.append({"name": name, "cash": round(c, 2), "orders": n, "cash_pct": round(cp, 1), "orders_pct": round(op, 1),
                    "delta": round(cp - op, 1), "avg": round(c / n, 2) if n else 0.0,
                    "top_wallet_pct": round(top) if top is not None else None})
    return out


def summarize_market(kind, market, trades):
    """One market: its kind, question, odds per side and the side stats. Odds come from the market's outcome prices."""
    outcomes = [str(o) for o in _json(market.get("outcomes"), [])]
    prices = [float(x) for x in _json(market.get("outcomePrices"), [])]
    sides = side_stats(trades, outcomes)
    for s, p in zip(sides, prices):
        s["odds"] = american(p)
    return {"kind": kind, "question": market.get("question"), "volume": round(sum(s["cash"] for s in sides), 2),
            "trades": sum(s["orders"] for s in sides), "sides": sides}


def heaviest(markets):
    """(market kind, side name, delta) for the side with the biggest positive delta that has enough dollars and trades, or None."""
    best = None
    for m in markets:
        for s in m["sides"]:
            if s["cash"] >= MIN_SIDE_CASH and s["orders"] >= MIN_SIDE_ORDERS and s["delta"] > 0:
                if best is None or s["delta"] > best[2]:
                    best = (m["kind"], s["name"], s["delta"])
    return best


def money(x):
    return f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x / 1e3:.0f}K" if x >= 1e3 else f"${x:.0f}"


def format_game(game, markets):
    """Plain-text report for one game (the layout of the Discord post: cash %, orders %, delta, avg order, top wallet)."""
    lines = [f"🏈 {game}"]
    h = heaviest(markets)
    if h:
        lines.append(f"🐋 Heaviest side: {h[1]} ({h[0]}) at Δ {h[2]:+.1f}%")
    for m in markets:
        lines.append(f"\n{m['kind']} — {money(m['volume'])} traded, {m['trades']} orders")
        for s in m["sides"]:
            odds = f" ({s['odds']:+d})" if s.get("odds") else ""
            lines.append(f"■ {s['name']}{odds}")
            lines.append(f"   💰 {s['cash_pct']:.1f}% cash ({money(s['cash'])}) | 🎟 {s['orders_pct']:.1f}% orders ({s['orders']})")
            lines.append(f"   📊 Δ {s['delta']:+.1f}% | avg order ${s['avg']:.2f}"
                         + (f" | 🐋 top wallet {s['top_wallet_pct']}%" if s.get("top_wallet_pct") is not None else ""))
    return "\n".join(lines)


# ── Run ──────────────────────────────────────────────────────────────────────

def run(sport="NFL"):
    events = fetch_events(sport)
    print(f"{sport}: {len(events)} Polymarket events")
    games, shown_raw = {}, False
    for ev in events:
        mk = event_markets(ev)
        if not mk or not ev.get("title"):
            continue
        summaries = []
        for kind in ("Moneyline", "Spread", "Total"):
            m = mk.get(kind)
            if not m:
                continue
            try:
                trades = fetch_trades(m["conditionId"])
            except Exception as e:  # noqa: BLE001
                print(f"  {ev['title']} / {kind}: trades failed: {e}")
                continue
            if trades and not shown_raw:
                print("  sample raw trade:", json.dumps(trades[0])[:400])
                shown_raw = True
            s = summarize_market(kind, m, trades)
            if s["trades"]:
                summaries.append(s)
            elif trades and not shown_raw:
                print("  trades had an unexpected shape:", json.dumps(trades[0])[:400])
        if summaries:
            games[ev["title"]] = summaries
            print("\n" + format_game(ev["title"], summaries))
    now = datetime.now(EASTERN)
    folder = f"best_plays_log/market_flow/{now.strftime('%Y-%m-%d')}"
    os.makedirs(folder, exist_ok=True)
    with open(f"{folder}/{sport}_{now.strftime('%H%M')}.json", "w") as f:
        json.dump({"captured_at": now.isoformat(), "sport": sport, "games": games}, f)
    print(f"\nsaved {len(games)} games")
    return games


if __name__ == "__main__":
    sys.exit(0 if run(sys.argv[1] if len(sys.argv) > 1 else "NFL") is not None else 1)
