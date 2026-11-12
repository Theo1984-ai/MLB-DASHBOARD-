"""
Closing line value (CLV) for the First Seen plays: did the market move toward our side before the game?

For every First Seen play we find the last props snapshot taken before tip-off that still shows Pinnacle's line
for that player and market (the "close"), then compare it with the line when the play was first seen:

  pin_move   Pinnacle's line move since first seen, counted in our favour: Over = close - first, Under = first - close.
             Positive means Pinnacle moved toward our side (the soft books were right to be off), negative means
             Pinnacle moved back toward the soft book (the soft book was just stale and caught up).
  gap_close  how much better our number still is than Pinnacle's close: Over = close - our line, Under = our line - close.
             Positive means we would still be getting a better number than the market's final line.

A play "beat the close" when gap_close > 0. Nothing is written back to the logs; this only reads them.

    python -m scripts.clv <best_plays_log folder>      (prints the report)
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from scripts import best_plays as bp
from scripts import first_seen as fs

SNAP_RE = re.compile(r"([A-Z0-9]+)_(\d{4})_props\.json")
STORE_SUB = "clv"                 # best_plays_log/clv/<date>.json = {pick id: {pin_move, gap_close, close_line}}
LOOKBACK_H = 8                    # only snapshots taken within this many hours before the first tip-off are read
GIVE_UP_H = 12                    # a play with no close line this long after tip-off is stored as "none" and not retried


def load_snapshots(root, sports=None):
    """{sport: [(captured_at, snapshot), ...] sorted by time} from root/snapshots/<date>/<SPORT>_<hhmm>_props.json."""
    out = defaultdict(list)
    snaps = os.path.join(root, "snapshots")
    for d in sorted(os.listdir(snaps)):
        for name in sorted(os.listdir(os.path.join(snaps, d))):
            m = SNAP_RE.fullmatch(name)
            if not m or (sports and m.group(1) not in sports):
                continue
            with open(os.path.join(snaps, d, name)) as f:
                snap = json.load(f)
            try:
                out[m.group(1)].append((fs._parse(snap["captured_at"]), snap))
            except Exception:  # noqa: BLE001
                continue
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def close_line(snaps, game, who, market, commence):
    """Pinnacle's (line, over, under) for the player's market in the last snapshot taken before tip-off, or None."""
    tip = fs._parse(commence)
    best = None
    for t, snap in snaps:
        if t >= tip:
            break
        for g in snap.get("games", []):
            if g.get("game") != game:
                continue
            pin = ((g.get("players", {}).get(who) or {}).get("player_" + market) or {}).get("pinnacle") or {}
            if pin.get("line") is not None:
                best = (pin["line"], pin.get("over"), pin.get("under"), t)
    return best


def clv(rec, close):
    """pin_move and gap_close for one play (see the module docstring); None without a close line."""
    if not close:
        return None
    f = rec["first_seen"]
    sign = 1 if rec["bet"] == "OVER" else -1
    return {"pin_move": sign * (close[0] - f["pin_line"]),
            "gap_close": sign * (close[0] - f["line"]),
            "close_line": close[0]}


def catch_up(date, now, list_names, load, save, log_dir="best_plays_log"):
    """
    Save the closing line of every First Seen play of `date` whose game has started and that has none stored yet
    (best_plays_log/clv/<date>.json). Only the snapshots taken shortly before the tip-offs are read. Returns the
    number of plays stored. list_names(folder) -> [file names]; load(path) -> data or {}; save(path, data, msg).
    """
    log = load(f"{log_dir}/{fs.LOG_SUB}/{date}.json") or {}
    path = f"{log_dir}/{STORE_SUB}/{date}.json"
    store = load(path) or {}
    todo = {}
    for pid, rec in log.items():
        try:
            if pid not in store and fs._parse(rec["commence_time"]) <= now:
                todo[pid] = rec
        except Exception:  # noqa: BLE001
            continue
    if not todo:
        return 0
    tips = [fs._parse(r["commence_time"]).astimezone(fs.EASTERN).replace(tzinfo=None) for r in todo.values()]
    lo, hi = min(tips) - timedelta(hours=LOOKBACK_H), max(tips)
    days = (date, (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))
    added = 0
    for sport in sorted({r["sport"] for r in todo.values()}):
        snaps = []
        for d in days:
            for name in list_names(f"{log_dir}/snapshots/{d}"):
                m = SNAP_RE.fullmatch(name)
                if not m or m.group(1) != sport:
                    continue
                at = datetime.strptime(f"{d} {m.group(2)}", "%Y-%m-%d %H%M")
                if not lo <= at <= hi:
                    continue
                snap = load(f"{log_dir}/snapshots/{d}/{name}") or {}
                try:
                    snaps.append((fs._parse(snap["captured_at"]), snap))
                except Exception:  # noqa: BLE001
                    continue
        snaps.sort(key=lambda x: x[0])
        for pid, rec in todo.items():
            if rec["sport"] != sport:
                continue
            m = clv(rec, close_line(snaps, rec["game"], rec["who"], rec["market"], rec["commence_time"]))
            if m:
                store[pid] = m
                added += 1
            elif (now - fs._parse(rec["commence_time"])).total_seconds() / 3600 > GIVE_UP_H:
                store[pid] = {"close_line": None}
    if added or any(pid in store for pid in todo):
        save(path, store, f"CLV {date}: {added} closing lines")
    return added


def collect(root, sports=None):
    """[(record, clv dict)] for every First Seen play that has a close line."""
    snaps = load_snapshots(root, sports)
    out = []
    folder = os.path.join(root, fs.LOG_SUB)
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(folder, name)) as f:
            log = json.load(f)
        for rec in log.values():
            if sports and rec["sport"] not in sports:
                continue
            c = close_line(snaps.get(rec["sport"], []), rec["game"], rec["who"], rec["market"], rec["commence_time"])
            m = clv(rec, c)
            if m:
                out.append((rec, m))
    return out


def summarize(rows, key):
    """{group: {n, beat, kept, moved_for, moved_against, w, l, units}} grouped by key(rec)."""
    agg = defaultdict(lambda: {"n": 0, "beat": 0, "pin_for": 0, "pin_against": 0, "w": 0, "l": 0, "units": 0.0,
                               "move": 0.0})
    for rec, m in rows:
        a = agg[key(rec)]
        a["n"] += 1
        a["beat"] += m["gap_close"] > 0
        a["pin_for"] += m["pin_move"] > 0
        a["pin_against"] += m["pin_move"] < 0
        a["move"] += m["pin_move"]
        out = (rec.get("result") or {}).get("outcome")
        if out in ("win", "loss"):
            a["w" if out == "win" else "l"] += 1
            a["units"] += rec["result"]["profit"] if out == "win" else -1
    return agg


def _peak(rec):
    return max(rec["first_seen"]["stale"], rec["last_seen"]["stale"])


def report(rows):
    pct = lambda x, n: f"{100 * x / n:.0f}%" if n else "-"

    def show(title, key):
        agg = summarize(rows, key)
        print(f"\n{title}")
        print(f"  {'group':26} {'plays':>5} {'beat close':>10} {'pin moved for/against':>22} {'avg move':>9}  record")
        for k in sorted(agg, key=str):
            a = agg[k]
            n = a["n"]
            rec = f"{a['w']}-{a['l']} {a['units']:+.1f}u" if a["w"] + a["l"] else "-"
            print(f"  {str(k):26} {n:>5} {pct(a['beat'], n):>10} "
                  f"{pct(a['pin_for'], n) + ' / ' + pct(a['pin_against'], n):>22} {a['move'] / n:>+9.2f}  {rec}"
                  f"{'  small' if n < 30 else ''}")

    show("ALL PLAYS", lambda r: "all")
    show("BY SPORT", lambda r: r["sport"])
    show("BY BOOKS STALE (peak)", lambda r: _peak(r) if _peak(r) < 4 else "4+")
    show("BY SPORT + MARKET", lambda r: f"{r['sport']} {r['market']}")
    show("BY SIDE", lambda r: r["bet"])
    show("FILTERS", lambda r: ("NHL shots Over 2+ stale" if r["sport"] == "NHL" and r["market"] == "shots_on_goal"
                               and r["bet"] == "OVER" and _peak(r) >= 2
                               else "NBA threes" if r["sport"] == "NBA" and r["market"] == "threes"
                               else "4+ stale, no points" if _peak(r) >= 4 and "points" not in r["market"]
                               else "other"))


def main(root):
    rows = collect(root)
    print(f"{len(rows)} First Seen plays with a Pinnacle close line")
    report(rows)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "best_plays_log")
