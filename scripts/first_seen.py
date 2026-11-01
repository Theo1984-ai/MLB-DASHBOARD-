"""
First-seen tracker for player-prop line splits.

A "line split" is a prop where a soft book's line differs from Pinnacle's (Pinnacle is the sharp anchor).
Every hourly snapshot is scanned; the first time a split appears it is logged with the time, the line,
the price and how far the soft books are from Pinnacle. Logged records use the same shape as the Best
Plays pick log, so the existing grader settles them, and then `summarize` shows whether plays that appear
early (or late) before tip-off do better.
"""
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts import best_plays as bp

EASTERN = ZoneInfo("America/New_York")
FOLLOWERS = ["draftkings", "fanduel", "williamhill_us", "fanatics", "betrivers", "betonlineag", "espnbet"]
MIN_GAP = 0.5
LOG_SUB = "first_seen"


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def split_plays(sport, props_snap, snap_time):
    """Open line splits in one props snapshot: [dict] (one per player/market/side)."""
    out = []
    for g in (props_snap or {}).get("games", []):
        try:
            if _parse(g["commence_time"]) <= snap_time:
                continue
        except Exception:  # noqa: BLE001
            continue
        for who, mks in g.get("players", {}).items():
            for market, bd in mks.items():
                pin = bd.get("pinnacle") or {}
                L = pin.get("line")
                if L is None or market == "player_anytime_td":
                    continue
                soft = [(b, bd[b]["line"], bd[b].get("over"), bd[b].get("under"))
                        for b in FOLLOWERS if b in bd and bd[b].get("line") is not None]
                if not soft:
                    continue
                above = [x for x in soft if x[1] > L]      # books with a higher line -> UNDER is the value
                below = [x for x in soft if x[1] < L]      # books with a lower line  -> OVER is the value
                up = [x for x in above if x[3] is not None and x[1] - L >= MIN_GAP]
                down = [x for x in below if x[2] is not None and L - x[1] >= MIN_GAP]
                if up and len(above) >= len(below):
                    bet, stale = "UNDER", above
                    pick = max(up, key=lambda x: (x[1], x[3]))
                    price = pick[3]
                elif down:
                    bet, stale = "OVER", below
                    pick = min(down, key=lambda x: (x[1], -x[2]))
                    price = pick[2]
                elif up:
                    bet, stale = "UNDER", above
                    pick = max(up, key=lambda x: (x[1], x[3]))
                    price = pick[3]
                else:
                    continue
                out.append({"sport": sport, "kind": "prop", "game": g["game"], "who": who,
                            "market": market.replace("player_", ""), "bet": bet,
                            "commence_time": g["commence_time"],
                            "line": pick[1], "price": price, "book": pick[0], "pin_line": L,
                            "gap": round(abs(pick[1] - L), 1), "stale": len(stale), "n_books": len(soft)})
    return out


def update_log(log, plays, iso, date):
    """Add splits not seen before (keeping the first sighting) and refresh last_seen. Returns new count."""
    new = 0
    for p in plays:
        pid = bp.pick_id(date, p)
        stamp = {"t": iso, "line": p["line"], "price": p["price"], "book": p["book"],
                 "pin_line": p["pin_line"], "gap": p["gap"], "stale": p["stale"], "n_books": p["n_books"]}
        if pid not in log:
            rec = {k: p[k] for k in ("sport", "kind", "game", "who", "market", "bet", "commence_time")}
            rec.update({"first_seen": stamp, "last_seen": stamp, "result": None})
            log[pid] = rec
            new += 1
        else:
            log[pid]["last_seen"] = stamp
    return new


def hours_before_tip(rec):
    try:
        return (_parse(rec["commence_time"]) - _parse(rec["first_seen"]["t"])).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return None


def first_seen_hour(rec):
    return _parse(rec["first_seen"]["t"]).astimezone(EASTERN).hour


def bucket_lead(h):
    if h is None:
        return "?"
    for cap, name in ((1, "under 1 h"), (3, "1-3 h"), (6, "3-6 h"), (12, "6-12 h")):
        if h < cap:
            return name
    return "12 h +"


LEAD_ORDER = ["12 h +", "6-12 h", "3-6 h", "1-3 h", "under 1 h"]


def summarize(recs, key):
    """{group: {w, l, p, units, n}} for graded records; key(rec) names the group."""
    agg = defaultdict(lambda: {"w": 0, "l": 0, "p": 0, "units": 0.0, "n": 0})
    for r in recs:
        res = r.get("result") or {}
        o = res.get("outcome")
        if o not in ("win", "loss", "push"):
            continue
        a = agg[key(r)]
        a["n"] += 1
        a["w"] += o == "win"
        a["l"] += o == "loss"
        a["p"] += o == "push"
        a["units"] += res.get("profit") or 0.0
    return dict(agg)
