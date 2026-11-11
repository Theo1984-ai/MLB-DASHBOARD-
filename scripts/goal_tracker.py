"""
NHL goals tracker: does a shots-on-goal Over signal plus shooting percentage predict a goal?

Only players who have a shots-on-goal **Over** line split (a soft book's line below Pinnacle's, the play shown on
First Seen and Player Props) are tracked. For each one we log, the first time it shows up:

  expected shots   from Pinnacle's shots line and prices
  goals per shot   the player's own goals/shots this season, shrunk toward the league (scripts/nhl_shooting.py)
  chance of a goal 1 - exp(-expected shots x goals per shot)
  price            the best soft-book anytime-goal price (Over 0.5 goals) at that moment, and its expected value

The existing grader settles each record after the game (market "goals" = the player's goals, bet Over 0.5), so the
Results table shows whether the chance is calibrated and whether betting the price makes money.
"""
import re
from datetime import datetime

from scripts import best_plays as bp
from scripts import first_seen as fs
from scripts import nhl_shooting as shoot

LOG_SUB = "goal_tracker"
DONE_FILE = "goal_tracker_done.json"
SHOTS = "player_shots_on_goal"
GOALS = "player_goals"


def _goal_price(players, who):
    """(best soft-book Over 0.5 goals price, book) or (None, None)."""
    bd = (players.get(who) or {}).get(GOALS) or {}
    soft = [(b, bd[b]["over"]) for b in fs.FOLLOWERS if b in bd and bd[b].get("over") is not None
            and bd[b].get("line") == 0.5]
    if not soft:
        return None, None
    book, price = max(soft, key=lambda x: x[1])
    return price, book


def detect_plays(snap, snap_time, table, league):
    """One dict per player with a shots Over split, a Pinnacle shots price and an anytime-goal price."""
    out = []
    games = {g["game"]: g for g in (snap or {}).get("games", [])}
    for p in fs.split_plays("NHL", snap, snap_time):
        if p["market"] != "shots_on_goal" or p["bet"] != "OVER":
            continue
        g = games.get(p["game"])
        pin = (((g or {}).get("players", {}).get(p["who"]) or {}).get(SHOTS) or {}).get("pinnacle") or {}
        if pin.get("line") is None or pin.get("over") is None:
            continue
        price, book = _goal_price(g["players"], p["who"])
        if price is None:
            continue
        exp_shots = shoot.expected_shots(pin["line"], pin["over"], pin.get("under"))
        rate, shots, gp = shoot.goal_rate(table, league, p["who"])
        chance = shoot.goal_chance(exp_shots, rate)
        out.append({"sport": "NHL", "kind": "prop", "game": p["game"], "who": p["who"], "market": "goals",
                    "bet": "OVER", "commence_time": p["commence_time"], "line": 0.5, "price": price, "book": book,
                    "shots_line": p["line"], "stale": p["stale"], "n_books": p["n_books"],
                    "exp_shots": round(exp_shots, 2), "rate": round(rate, 4), "sample_shots": shots,
                    "sample_games": gp, "chance": round(chance, 4),
                    "ev": round(chance * bp.decimal_odds(price) - 1, 4)})
    return out


def update_log(log, plays, iso, date):
    """Log the first sighting of each player; later sightings refresh last_seen. Returns the new count."""
    new = 0
    for p in plays:
        pid = bp.pick_id(date, p)
        stamp = {k: p[k] for k in ("line", "price", "book", "shots_line", "stale", "n_books", "exp_shots", "rate",
                                   "sample_shots", "sample_games", "chance", "ev")}
        stamp["t"] = iso
        if pid not in log:
            rec = {k: p[k] for k in ("sport", "kind", "game", "who", "market", "bet", "commence_time")}
            rec.update({"first_seen": stamp, "last_seen": stamp, "result": None})
            log[pid] = rec
            new += 1
        else:
            log[pid]["last_seen"] = stamp
    return new


def catch_up(date, list_names, load, save, log_dir="best_plays_log"):
    """Log every saved NHL props snapshot of `date` that has not been processed. Returns the new count."""
    done_path = f"{log_dir}/{DONE_FILE}"
    done = load(done_path) or {}
    seen = set(done.get(date, []))
    todo = sorted((m.group(1), n) for n in list_names(f"{log_dir}/snapshots/{date}")
                  for m in [re.fullmatch(r"NHL_(\d{4})_props\.json", n)] if m and n not in seen)
    if not todo:
        return 0
    table, league = shoot.shooting_table(load, list_names, log_dir)
    path = f"{log_dir}/{LOG_SUB}/{date}.json"
    log = load(path) or {}
    new = 0
    for hhmm, name in todo:
        snap = load(f"{log_dir}/snapshots/{date}/{name}") or {}
        try:
            t = fs._parse(snap["captured_at"])
        except Exception:  # noqa: BLE001
            t = datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=fs.EASTERN)
        new += update_log(log, detect_plays(snap, t, table, league), t.isoformat(), date)
        seen.add(name)
    save(path, log, f"Goal tracker {date}: {new} new")
    done[date] = sorted(seen)
    for old in sorted(done)[:-3]:
        del done[old]
    save(done_path, done, f"Goal tracker progress {date}")
    return new
