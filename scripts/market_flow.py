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

    python -m scripts.market_flow [NFL | NBA | NHL | CFB | NCAAB | MLB | ALL]

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
TAGS = {"NFL": ("nfl",), "NBA": ("nba",), "NHL": ("nhl",), "MLB": ("mlb",), "CFB": ("college-football", "ncaaf", "ncaa-football"),
        "NCAAB": ("cbb", "college-basketball", "ncaab")}      # Polymarket tag slugs, tried in order and merged
ALL_SPORTS = ("NFL", "NBA", "NHL", "CFB", "NCAAB", "MLB")
MAX_GAMES = 25                 # per sport: the busiest games (by Polymarket volume) are read, so a big college slate stays quick
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


FUTURES_TOKENS = ("season series", "world series", "super bowl", "champion", "mvp", "rookie of", "division", "clinch", "playoff", "make the",
                  "regular season", "season wins", "coach of", "win the", "most ", "leader", "draft")


def is_game_event(title, slug=""):
    """True for a single game ("A vs. B", "A @ B"), not a futures or season-long market."""
    blob = f"{title} {slug}".lower()
    if any(t in blob for t in FUTURES_TOKENS):
        return False
    return any(x in blob for x in (" vs.", " vs ", " @ ", " at "))


def fetch_events(sport, page=100, max_pages=10):
    """Open Polymarket events with the sport's tag(s), merged. Polymarket caps a page at 100 events, so this reads up to
    `max_pages` pages per tag (the first pages are mostly novelty and futures markets; the games come later)."""
    out, seen = [], set()
    for tag in TAGS[sport]:
        for i in range(max_pages):
            try:
                batch = _get(f"{GAMMA}/events?closed=false&tag_slug={tag}&limit={page}&offset={i * page}")
            except Exception as e:  # noqa: BLE001
                print(f"  tag {tag} offset {i * page}: {e}")
                break
            for ev in batch if isinstance(batch, list) else []:
                if ev.get("id") not in seen:
                    seen.add(ev.get("id"))
                    out.append(ev)
            if not isinstance(batch, list) or len(batch) < page:
                break
    return out


def _text(url, limit=6000):
    """First `limit` characters of a page (used to read Polymarket's docs from Actions, where the sandbox can't)."""
    req = urllib.request.Request(url, headers={**_HEADERS, "Accept": "text/markdown,text/plain,*/*"})
    resp = urllib.request.urlopen(req, timeout=30, context=_CTX)
    body = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return body.decode("utf-8", "replace")[:limit]


_PROBED = []
META = {}                      # what the last fetch_trades call saw (rows per page, pagination block), printed for diagnosis


def probe_v2(condition_id):
    """Polymarket retired the v1 data API (HTTP 410). Print the migration docs and the replies of likely v2 trades URLs once,
    so the parser can be written from real output."""
    if _PROBED:
        return
    _PROBED.append(1)
    print("\n=== Polymarket Data API v2 probe ===")
    for url in ("https://docs.polymarket.com/api-reference/data-api/overview.md",
                "https://docs.polymarket.com/api-reference/data-api/overview",
                "https://docs.polymarket.com/llms.txt"):
        try:
            t = _text(url, 5000 if "llms" not in url else 200000)
            if "llms" in url:                                   # only the lines about trades / data api
                t = "\n".join(l for l in t.splitlines() if "trade" in l.lower() or "data-api" in l.lower())[:4000]
            print(f"--- {url}\n{t}")
            if "overview" in url and t.strip():
                break
        except Exception as e:  # noqa: BLE001
            print(f"--- {url}: {e}")
    for url in (f"{DATA}/v2/", f"{DATA}/v2/trades?market={condition_id}&limit=3",
                f"{DATA}/v2/trades?conditionId={condition_id}&limit=3"):
        try:
            print(f"--- {url}\n{_text(url, 1500)}")
        except Exception as e:  # noqa: BLE001
            print(f"--- {url}: {e}")
    print("=== end probe ===\n")


class DataApiRetired(RuntimeError):
    pass


def fetch_trades(condition_id, max_trades=MAX_TRADES, limit=500):
    """
    Newest trades of one market, newest first, from Polymarket's data API v2 (GET /v2/trades?conditionId=0x...; the v1 /trades
    route was retired). The reply is {"data": [trade, ...], "pagination": {...}}; later pages are fetched with the cursor the reply
    gives, until `max_trades` or no cursor. A trade has proxy_wallet, side, outcome, outcome_index, size (shares), price, timestamp.
    """
    out, cursor = [], None
    META.clear()
    for _ in range(max_trades // 100 + 1):
        url = f"{DATA}/v2/trades?conditionId={condition_id}" + (f"&cursor={cursor}" if cursor else f"&limit={limit}")
        try:
            resp = _get(url)
        except RuntimeError as e:
            if "HTTP 400" in str(e) and not cursor and limit > 100:        # page size too big: try 100
                limit = 100
                continue
            if "HTTP 410" in str(e):                                        # this API version was retired again
                raise DataApiRetired(str(e)) from e
            raise
        batch = resp.get("data") if isinstance(resp, dict) else resp
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        pag = (resp.get("pagination") if isinstance(resp, dict) else None) or {}
        META.setdefault("pages", []).append({"rows": len(batch), "pagination": pag,
                                              "other_keys": [k for k in (resp if isinstance(resp, dict) else {}) if k != "data"]})
        cursor = next((pag[k] for k in ("next_cursor", "nextCursor", "cursor") if pag.get(k)), None)
        if not cursor or len(out) >= max_trades:
            break
    return out[:max_trades]


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


def game_start(event):
    """Earliest gameStartTime (aware datetime) among an event's markets, or None. Gamma writes it like '2026-11-26 18:00:00+00'."""
    from datetime import timezone
    best = None
    for m in event.get("markets") or []:
        gs = str(m.get("gameStartTime") or "").replace(" ", "T")
        if not gs:
            continue
        if gs.endswith("+00"):
            gs += ":00"
        try:
            d = datetime.fromisoformat(gs)
        except ValueError:
            continue
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        best = d if best is None or d < best else best
    return best


def pick_games(events, now=None, ahead_hours=48, behind_hours=4, limit=MAX_GAMES):
    """
    The games to read: single-game events that start within the next `ahead_hours` (or began within `behind_hours`), soonest
    first. Events with no start time are used only when no game has one, busiest first (by Polymarket volume).
    """
    from datetime import timedelta, timezone
    now = now or datetime.now(timezone.utc)
    games = [e for e in events if is_game_event(e.get("title", ""), e.get("slug", ""))]
    timed = [(game_start(e), e) for e in games]
    soon = sorted([(t, e) for t, e in timed if t and now - timedelta(hours=behind_hours) <= t <= now + timedelta(hours=ahead_hours)],
                  key=lambda x: x[0])
    if soon or any(t for t, _ in timed):
        return [e for _, e in soon][:limit]
    return sorted(games, key=lambda e: -sum(_vol(m) for m in e.get("markets") or []))[:limit]


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
        idx = trade.get("outcome_index", trade.get("outcomeIndex"))
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
        w = t.get("proxy_wallet") or t.get("proxyWallet") or t.get("maker") or t.get("user")
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


ICON = {"NFL": "🏈", "CFB": "🎓", "NBA": "🏀", "NCAAB": "🏀", "NHL": "🏒", "MLB": "⚾"}


def format_game(game, markets, sport=None):
    """Plain-text report for one game (the layout of the Discord post: cash %, orders %, delta, avg order, top wallet)."""
    lines = [f"{ICON.get(sport, '🎯')} {game}"]
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
    game_events = [ev for ev in events if is_game_event(ev.get("title", ""), ev.get("slug", ""))]
    print(f"{sport}: {len(events)} Polymarket events, {len(game_events)} look like single games")
    for ev in game_events[:6]:
        print("  game event:", ev.get("title"), "|", len(ev.get("markets") or []), "markets")
    games, shown_raw = {}, False
    picked = pick_games(events)
    print(f"  reading {len(picked)} games that start in the next 48 hours (soonest first)")
    for ev in picked:
        mk = event_markets(ev)
        if not mk:
            ms = ev.get("markets") or []
            print(f"  skipped {ev.get('title')}: no moneyline/spread/total market found among {len(ms)} markets; first: "
                  + json.dumps({k: ms[0].get(k) for k in ("question", "outcomes", "conditionId")})[:300] if ms else "  (no markets)")
            continue
        summaries = []
        for kind in ("Moneyline", "Spread", "Total"):
            m = mk.get(kind)
            if not m:
                continue
            try:
                trades = fetch_trades(m["conditionId"])
            except DataApiRetired as e:
                print(f"  trades API retired: {e}")
                probe_v2(m["conditionId"])
                print(f"{sport}: stopping, the trades data source has to be updated first")
                return games
            except Exception as e:  # noqa: BLE001
                print(f"  {ev['title']} / {kind}: trades failed: {e}")
                continue
            if trades and not shown_raw:
                print("  sample raw trade:", json.dumps(trades[0])[:400])
                shown_raw = True
            ts = [t.get("timestamp") for t in trades if t.get("timestamp")]
            print(f"  {ev['title']} / {kind}: Polymarket volume ${_vol(m):,.0f}, {len(trades)} trades read"
                  + (f", oldest {min(ts)}, newest {max(ts)}" if ts else "")
                  + f" | pages {json.dumps(META.get('pages'))[:300]}")
            s = summarize_market(kind, m, trades)
            if s["trades"]:
                summaries.append(s)
            elif trades and not shown_raw:
                print("  trades had an unexpected shape:", json.dumps(trades[0])[:400])
        if summaries:
            games[ev["title"]] = summaries
            print("\n" + format_game(ev["title"], summaries, sport))
    now = datetime.now(EASTERN)
    folder = f"best_plays_log/market_flow/{now.strftime('%Y-%m-%d')}"
    os.makedirs(folder, exist_ok=True)
    with open(f"{folder}/{sport}_{now.strftime('%H%M')}.json", "w") as f:
        json.dump({"captured_at": now.isoformat(), "sport": sport, "games": games}, f)
    print(f"\nsaved {len(games)} games")
    return games


def run_all(sports=ALL_SPORTS):
    """run() for every sport; one sport failing does not stop the others. {sport: games}."""
    out = {}
    for sp in sports:
        print(f"\n{'=' * 20} {sp} {'=' * 20}")
        try:
            out[sp] = run(sp)
        except Exception as e:  # noqa: BLE001
            print(f"{sp} failed: {e}")
            out[sp] = {}
    return out


if __name__ == "__main__":
    arg = (sys.argv[1] if len(sys.argv) > 1 else "NFL").upper()
    sys.exit(0 if (run_all() if arg == "ALL" else run(arg)) is not None else 1)
