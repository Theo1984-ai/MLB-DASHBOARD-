"""
Prop signals tracker: grades the Player Props page's "steam" signals.

The Player Props page watches Over prices. The sequence it looks for:
  1. Pinnacle's Over price drops (gets more expensive),
  2. a soft book follows (its Over price drops too),
  3. a price crosses zero, or the middle line drops.
Steps 1-2 are "building"; steps 1-3 are the page's "full sequence"; step 1 alone is a "pin_drop".

Every saved props snapshot is compared with the one before it for the same sport. When a prop shows the
sequence, an OVER bet is logged at the best soft-book number at that moment (lowest line, then best price).
Each tier is logged on its own, so a play that is a pin_drop at 7 and a full sequence at 8 is two records,
each with the price that was there at the time. Records have the same shape as the First Seen log, so the
existing grader settles them after the game.
"""
import re
from datetime import datetime

from scripts import best_plays as bp
from scripts import first_seen as fs

LOG_SUB = "prop_signals"
DONE_FILE = "prop_signals_done.json"
FOLLOWERS = fs.FOLLOWERS
TIER_ORDER = ["full", "building", "pin_drop"]
TIER_LABEL = {"full": "Full sequence", "building": "Sequence building", "pin_drop": "Pinnacle drop only"}


def tier_of(cur, prev):
    """Tier of one prop between two snapshots, or None. cur/prev: {book: {line, over, under}}."""
    if not cur or not prev:
        return None
    p0, p1 = (prev.get("pinnacle") or {}).get("over"), (cur.get("pinnacle") or {}).get("over")
    if p0 is None or p1 is None or p1 >= p0:
        return None                                              # step 1: Pinnacle's Over price dropped
    followed = False
    for bk in FOLLOWERS:
        a, b = (prev.get(bk) or {}).get("over"), (cur.get(bk) or {}).get("over")
        if a is not None and b is not None and b < a:
            followed = True                                      # step 2: a soft book followed
    if not followed:
        return "pin_drop"
    crossed = False
    for bk, v in cur.items():
        a, b = (prev.get(bk) or {}).get("over"), v.get("over")
        if a is not None and b is not None and ((a > 0 > b) or (a < 0 < b)):
            crossed = True                                       # step 3a: a price crossed zero
    pl = sorted(v["line"] for v in prev.values() if v.get("line") is not None)
    cl = sorted(v["line"] for v in cur.values() if v.get("line") is not None)
    dropped = bool(pl and cl and cl[len(cl) // 2] < pl[len(pl) // 2])   # step 3b: the middle line dropped
    return "full" if (crossed or dropped) else "building"


def detect_plays(sport, cur_snap, prev_snap, snap_time):
    """OVER plays for props showing the sequence between two snapshots: [dict]."""
    out = []
    prev_games = {g["game"]: g for g in (prev_snap or {}).get("games", [])}
    for g in (cur_snap or {}).get("games", []):
        try:
            if fs._parse(g["commence_time"]) <= snap_time:
                continue                                         # already started
        except Exception:  # noqa: BLE001
            continue
        pg = prev_games.get(g["game"])
        if not pg:
            continue
        for who, mks in g.get("players", {}).items():
            for market, cur in mks.items():
                if market == "player_anytime_td":
                    continue
                tier = tier_of(cur, (pg.get("players", {}).get(who) or {}).get(market))
                if not tier:
                    continue
                soft = [(b, cur[b]["line"], cur[b]["over"]) for b in FOLLOWERS
                        if b in cur and cur[b].get("line") is not None and cur[b].get("over") is not None]
                if not soft:
                    continue
                book, line, price = min(soft, key=lambda x: (x[1], -x[2]))
                pin = cur.get("pinnacle") or {}
                prev_pin = (pg["players"][who][market].get("pinnacle") or {})
                out.append({"sport": sport, "kind": "prop", "game": g["game"], "who": who,
                            "market": market.replace("player_", ""), "bet": "OVER", "tier": tier,
                            "commence_time": g["commence_time"], "line": line, "price": price, "book": book,
                            "pin_line": pin.get("line"), "pin_from": prev_pin.get("over"),
                            "pin_to": pin.get("over")})
    return out


def play_id(date, p):
    return bp.pick_id(date, p) + "|" + p["tier"]


def update_log(log, plays, iso, date):
    """Add signals not logged before (the first sighting of each tier is kept). Returns the new count."""
    new = 0
    for p in plays:
        pid = play_id(date, p)
        if pid in log:
            continue
        stamp = {"t": iso, "line": p["line"], "price": p["price"], "book": p["book"],
                 "pin_line": p["pin_line"], "pin_from": p["pin_from"], "pin_to": p["pin_to"]}
        rec = {k: p[k] for k in ("sport", "kind", "game", "who", "market", "bet", "tier", "commence_time")}
        rec.update({"first_seen": stamp, "result": None})
        log[pid] = rec
        new += 1
    return new


def catch_up(date, list_names, load, save, log_dir="best_plays_log"):
    """
    Compare every saved props snapshot of `date` that has not been processed with the one before it
    (same sport) and log the signals, stamped with the time the snapshot was taken. A small progress file
    remembers which snapshots are done. Returns the number of new signals.
    list_names(folder) -> [file names]; load(path) -> data or {}; save(path, data, commit_msg).
    """
    done_path = f"{log_dir}/{DONE_FILE}"
    done = load(done_path) or {}
    seen = set(done.get(date, []))
    by_sport = {}
    for n in sorted(list_names(f"{log_dir}/snapshots/{date}")):
        m = re.fullmatch(r"([A-Z]+)_(\d{4})_props\.json", n)
        if m:
            by_sport.setdefault(m.group(1), []).append((m.group(2), n))
    work = []
    for sport, items in by_sport.items():
        for i, (hhmm, n) in enumerate(items):
            if n not in seen and i > 0:
                work.append((hhmm, sport, n, items[i - 1][1]))
            elif n not in seen:
                seen.add(n)                                      # the day's first snapshot has nothing to compare
    if not work and not seen - set(done.get(date, [])):
        return 0
    path = f"{log_dir}/{LOG_SUB}/{date}.json"
    log = load(path) or {}
    new = 0
    for hhmm, sport, name, prev_name in sorted(work):
        snap = load(f"{log_dir}/snapshots/{date}/{name}") or {}
        prev = load(f"{log_dir}/snapshots/{date}/{prev_name}") or {}
        try:
            t = fs._parse(snap["captured_at"])
        except Exception:  # noqa: BLE001
            t = datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=fs.EASTERN)
        new += update_log(log, detect_plays(sport, snap, prev, t), t.isoformat(), date)
        seen.add(name)
    if new or work:
        save(path, log, f"Prop signals {date}: {new} new")
    done[date] = sorted(seen)
    for old in sorted(done)[:-3]:
        del done[old]
    save(done_path, done, f"Prop signals progress {date}")
    return new
