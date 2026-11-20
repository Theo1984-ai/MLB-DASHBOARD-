"""
One-off probe: which player props does Polymarket carry, and how do they price against our books?

Run in GitHub Actions (Polymarket is reachable there): python -m scripts.polymarket_props_probe
Pulls active Polymarket sports events (Gamma API), picks out player-prop markets ("<Player>: Points O/U 24.5"), saves them to
best_plays_log/polymarket_props/<date>.json and prints a summary: counts by sport and stat, the prices, and, where the newest
saved props snapshot has the same player, market and line at Pinnacle, how far Polymarket is from Pinnacle's no-vig price.
Questions that mention a stat but do not parse are kept in "unparsed" so the parser can be fixed from real data.
"""
import glob
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts import best_plays as bp

GAMMA = "https://gamma-api.polymarket.com"
TAGS = ("nba", "nfl", "nhl", "mlb", "cfb", "cbb", "ncaab", "sports", "basketball", "football", "hockey")
STATS = {  # words in the question -> our market name (the part after "player_" in the snapshots)
    "points": "points", "rebounds": "rebounds", "assists": "assists", "threes": "threes", "three-pointers": "threes",
    "3-pointers": "threes", "passing yards": "pass_yds", "rushing yards": "rush_yds", "receiving yards": "reception_yds",
    "receptions": "receptions", "shots on goal": "shots_on_goal", "touchdowns": "anytime_td", "strikeouts": "strikeouts",
    "hits": "hits", "home runs": "home_runs", "goals": "goals", "saves": "saves",
}
_STAT_RE = "|".join(sorted((re.escape(s) for s in STATS), key=len, reverse=True))
PROP_RE = re.compile(rf"^(?P<who>[^:]+?)\s*[:\-–]\s*(?P<stat>{_STAT_RE})\s*(?:o/u|over/under|over)\s*(?P<line>\d+(?:\.\d+)?)",
                     re.I)
_CTX = ssl.create_default_context()


_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mlb-dashboard/1.0)", "Accept": "application/json"}


def _get(url):
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30, context=_CTX).read())
    except urllib.error.HTTPError as e:                      # show what Polymarket said, not just the code
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {e.read()[:200]!r}") from e


def _json(v, default):
    try:
        return json.loads(v) if isinstance(v, str) else (v if v is not None else default)
    except Exception:  # noqa: BLE001
        return default


def parse_prop(question):
    """{who, stat (our market name), line} from a Polymarket question like 'Stephen Curry: Points O/U 24.5', or None."""
    m = PROP_RE.match((question or "").strip())
    if not m:
        return None
    return {"who": m.group("who").strip(), "stat": STATS[m.group("stat").lower()], "line": float(m.group("line"))}


def prop_from_market(m, event_title=""):
    """A saved prop record from one Gamma market dict (or None if it is not a player prop)."""
    p = parse_prop(m.get("question"))
    if not p:
        return None
    outs = [str(o).lower() for o in _json(m.get("outcomes"), [])]
    prices = [float(x) for x in _json(m.get("outcomePrices"), [])]
    if len(outs) != len(prices) or "over" not in outs:
        return None
    over = prices[outs.index("over")]
    under = prices[outs.index("under")] if "under" in outs else 1 - over
    def f(k):
        try:
            return float(m.get(k) or 0)
        except Exception:  # noqa: BLE001
            return 0.0
    return {**p, "over": over, "under": under, "event": event_title, "question": m.get("question"),
            "volume": f("volume"), "liquidity": f("liquidity"), "end": m.get("endDate")}


def fetch_events():
    seen, events = set(), []
    for tag in TAGS:
        for off in range(0, 1000, 100):
            try:
                page = _get(f"{GAMMA}/events?closed=false&tag_slug={tag}&limit=100" + (f"&offset={off}" if off else ""))
            except Exception as e:  # noqa: BLE001
                print(f"tag {tag} offset {off}: {e}")
                break
            if not page:
                break
            for ev in page:
                if ev.get("id") not in seen:
                    seen.add(ev.get("id"))
                    events.append(ev)
    return events


def compare_to_pinnacle(props, root="best_plays_log"):
    """For props whose player, market and line match Pinnacle in the newest saved props snapshot of each sport:
    [(who, stat, line, polymarket over, pinnacle no-vig over)]."""
    snaps = {}
    for f in sorted(glob.glob(f"{root}/snapshots/*/*_props.json")):
        snaps[re.search(r"/([A-Z0-9]+)_\d{4}_props", f).group(1)] = f
    lookup = {}
    for sport, f in snaps.items():
        for g in json.load(open(f)).get("games", []):
            for who, mk in g["players"].items():
                for market, bd in mk.items():
                    pin = bd.get("pinnacle") or {}
                    if pin.get("over") is not None and pin.get("under") is not None:
                        lookup[(bp.norm_name(who), market.replace("player_", ""), pin["line"])] = pin
    imp = lambda p: 100 / (p + 100) if p > 0 else -p / (-p + 100)
    out = []
    for p in props:
        pin = lookup.get((bp.norm_name(p["who"]), p["stat"], p["line"]))
        if pin:
            o, u = imp(pin["over"]), imp(pin["under"])
            out.append((p["who"], p["stat"], p["line"], p["over"], round(o / (o + u), 3)))
    return out


def main():
    events = fetch_events()
    props, unparsed = [], []
    for ev in events:
        for m in ev.get("markets") or []:
            r = prop_from_market(m, ev.get("title", ""))
            if r:
                props.append(r)
            elif re.search(_STAT_RE, (m.get("question") or "").lower()):
                unparsed.append(m.get("question"))
    cmp_rows = compare_to_pinnacle(props)
    print(f"events: {len(events)} | player props parsed: {len(props)} | unparsed stat questions: {len(unparsed)}")
    by = Counter(p["stat"] for p in props)
    print("props by stat:", dict(by))
    for p in sorted(props, key=lambda p: -p["volume"])[:20]:
        print(f"  {p['question']} | over {p['over']:.2f} under {p['under']:.2f} | vol ${p['volume']:,.0f} liq ${p['liquidity']:,.0f}")
    print(f"matched to a Pinnacle line: {len(cmp_rows)}")
    for r in cmp_rows[:30]:
        print(f"  {r[0]} {r[1]} {r[2]}: Polymarket Over {r[3]:.3f} vs Pinnacle {r[4]:.3f} (diff {r[3] - r[4]:+.3f})")
    print("sample unparsed:", unparsed[:15])
    day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    os.makedirs("best_plays_log/polymarket_props", exist_ok=True)
    with open(f"best_plays_log/polymarket_props/{day}.json", "w") as f:
        json.dump({"events": len(events), "props": props, "unparsed": unparsed[:200], "compare": cmp_rows}, f)


if __name__ == "__main__":
    sys.exit(main())
