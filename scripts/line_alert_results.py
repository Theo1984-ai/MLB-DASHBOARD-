"""
Grade and save the NHL line alert plays (team totals).

The line alert workflow saves plays to nhl_line_alert_history/<date>.json but never settles them, and the Line Alert
page only works results out while it is open from the Odds API scores feed, which keeps just a few days. This turns each
unique play into a pick-like record (same shape as the Best Plays pick log), so the existing grader settles it from
ESPN's final score, and keeps the settled results in best_plays_log/line_alert_results/<SPORT>/<date>.json
(the results file is separate from the history file, which the Actions workflow commits).
"""
from datetime import datetime

from scripts import best_plays as bp
from scripts import line_alert_snapshot as las
from scripts import nhl_line_alert_snapshot as nlas

EASTERN = bp.EASTERN
RESULTS_DIR = "best_plays_log/line_alert_results"
SPORTS = {"NHL": "nhl_line_alert_history", "NBA": "nba_line_alert_history", "NFL": "nfl_line_alert_history",
          "CFB": "cfb_line_alert_history", "NCAAB": "ncaab_line_alert_history"}
HOURLY_DIR = "best_plays_log/line_alert_hourly"      # the hourly job's own line alert snapshots, same shape as the history


def play_id(day, sport, p):
    return "|".join([day, sport, "team total", p["game"], p["team"], str(p["line"]), p["bet"]])


def _commence(day, p):
    if p.get("commence_time"):
        return p["commence_time"]
    try:    # older plays only carry the start time as text ("07:10 PM") on the snapshot's date
        return datetime.strptime(f"{day} {p['time']}", "%Y-%m-%d %I:%M %p").replace(tzinfo=EASTERN).isoformat()
    except Exception:  # noqa: BLE001
        return None


def sync(day, sport, history, results):
    """Add every unique play in `history` ({snapshot time: {plays: [...]}}) to `results` that isn't there yet.
    Returns the number added. A play keeps the first snapshot where it appeared."""
    added = 0
    for snap_time in sorted(history):
        snap = history[snap_time] or {}
        for p in snap.get("plays", []):
            pid = play_id(day, sport, p)
            ct = _commence(day, p)
            if pid in results or not ct:
                continue
            results[pid] = {
                "sport": sport, "kind": "team total", "game": p["game"], "who": p["team"],
                "market": "team total", "bet": p["bet"], "commence_time": ct,
                "first_seen": {"t": snap.get("captured_at") or snap_time, "line": p["line"], "price": p["price"],
                               "book": p.get("book"), "why": p.get("why", "")},
                "result": None}
            added += 1
    return added


def grade_day(day, sport, history, results, now_et, fetch=None, finished_after_hours=1.5):
    """Sync new plays, then settle the finished ones. Returns (added, graded, errors). `results` is changed in place."""
    added = sync(day, sport, history, results)
    c = bp.grade_log(results, now_et, fetch=fetch or bp._get_plain, finished_after_hours=finished_after_hours)
    return added, c["graded"] + c.get("no_data", 0), c["errors"]


def summarize(all_results):
    """{'overall': ..., 'bet': ..., 'line': ...} W-L-P and units over graded plays (an iterable of records)."""
    out = {"overall": {}, "bet": {}, "line": {}}
    for r in all_results:
        res = r.get("result") or {}
        o = res.get("outcome")
        if o not in ("win", "loss", "push"):
            continue
        ln = r["first_seen"]["line"]
        for bucket, key in (("overall", "all"), ("bet", r["bet"]), ("line", ln)):
            a = out[bucket].setdefault(key, {"w": 0, "l": 0, "p": 0, "u": 0.0})
            a["w"] += o == "win"
            a["l"] += o == "loss"
            a["p"] += o == "push"
            a["u"] += res.get("profit") or 0.0
    return out


def hourly_plays(tt_snap, now_et, sport="NHL"):
    """Line alert plays (same detection as the scheduled workflow for that sport) from the hourly job's team totals
    snapshot, for games that have not started."""
    open_games = []
    for g in (tt_snap or {}).get("games", []):
        try:
            if datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")) > now_et:
                open_games.append(g)
        except Exception:  # noqa: BLE001
            continue
    if sport == "NHL":
        return nlas.detect_plays(open_games)
    followers = sorted({b for g in open_games for sd in g.get("sides", {}).values() for b in sd if b != "pinnacle"})
    return las.detect_plays(open_games, followers)


def record_hourly(hourly, plays, now_et):
    """Add this hour's plays to the day's hourly history ({HH:MM: {captured_at, plays}}) unless they are the same as
    the latest snapshot (an empty list is recorded too, so a signal that has gone away stops being used).
    Returns True when something was added."""
    if hourly:
        last = hourly[max(hourly, key=lambda k: hourly[k].get("captured_at", k))]
        if last.get("plays") == plays:
            return False
    elif not plays:
        return False
    hourly[now_et.strftime("%H:%M")] = {"captured_at": now_et.isoformat(), "plays": plays}
    return True
