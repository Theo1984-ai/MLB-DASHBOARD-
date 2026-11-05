"""
One-off backtest: does an NHL shots-on-goal Over signal find players who score a goal?

It reads every saved NHL props snapshot (best_plays_log/snapshots/<date>/NHL_<hhmm>_props.json), finds each
"shots on goal Over" line split (a soft book's line is lower than Pinnacle's) at its first sighting, notes how
many soft books were off Pinnacle and what FanDuel's anytime-goal (Over 0.5 goals) price was at that moment, then
fetches who actually scored from ESPN and prints the results:

  * the scoring rate of signal players, by how many books were off,
  * the profit of betting FanDuel's anytime-goal price on them,
  * the same for every player FanDuel priced (the baseline), so the signal can be compared with "just bet anyone".

Run in GitHub Actions (ESPN is reachable there): python -m scripts.nhl_goal_backtest
"""
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

from scripts import best_plays as bp
from scripts import first_seen as fs

ROOT = "best_plays_log/snapshots"
BOOK = "fanduel"            # the book that posts an anytime-goal (Over 0.5) price


def _fd_price(snap, game, who):
    for g in snap.get("games", []):
        if g["game"] == game:
            fd = ((g.get("players", {}).get(who) or {}).get("player_goals") or {}).get(BOOK) or {}
            if fd.get("line") == 0.5 and fd.get("over") is not None:
                return fd["over"]
    return None


def load_snapshots(root=ROOT):
    snaps = []
    for d in sorted(os.listdir(root)):
        for n in sorted(os.listdir(os.path.join(root, d))):
            if re.fullmatch(r"NHL_\d{4}_props\.json", n):
                with open(os.path.join(root, d, n)) as f:
                    s = json.load(f)
                try:
                    snaps.append((fs._parse(s["captured_at"]), s))
                except Exception:  # noqa: BLE001
                    continue
    snaps.sort(key=lambda x: x[0])
    return snaps


def collect(snaps):
    """-> (signals, baseline, commence). signals[(game, who)] = first sighting; baseline[(game, who)] = last
    pre-game FanDuel goal price for every priced player; commence[game] = start time."""
    signals, baseline, commence = {}, {}, {}
    for t, s in snaps:
        for g in s.get("games", []):
            try:
                if fs._parse(g["commence_time"]) <= t:
                    continue
            except Exception:  # noqa: BLE001
                continue
            commence[g["game"]] = g["commence_time"]
            for who in g.get("players", {}):
                price = _fd_price(s, g["game"], who)
                if price is not None:
                    baseline[(g["game"], who)] = price
        for p in fs.split_plays("NHL", s, t):
            if p["market"] != "shots_on_goal" or p["bet"] != "OVER":
                continue
            key = (p["game"], p["who"])
            rec = signals.get(key)
            if rec is None:
                signals[key] = {"t": t.isoformat(), "stale": p["stale"], "max_stale": p["stale"],
                                "n_books": p["n_books"], "fd": _fd_price(s, p["game"], p["who"])}
            else:
                rec["max_stale"] = max(rec["max_stale"], p["stale"])
                if rec["fd"] is None:
                    rec["fd"] = _fd_price(s, p["game"], p["who"])
    return signals, baseline, commence


def goals_scored(keys, commence, fetch=None):
    """{(game, who): goals} for finished games, from ESPN box scores. Players not in the box score are left out."""
    fetch = fetch or bp._get_plain
    games = defaultdict(set)
    for game, who in keys:
        games[game].add(who)
    out, events_by_date, errors = {}, {}, []
    for game, players in games.items():
        try:
            start = fs._parse(commence[game]).astimezone(fs.EASTERN)
            date = start.strftime("%Y-%m-%d")
            if date not in events_by_date:
                events_by_date[date] = bp.espn_events("NHL", date, fetch)
            away, _, home = game.partition(" @ ")
            ev = next((e for e in events_by_date[date]
                       if bp.team_match(e["home"], home) and bp.team_match(e["away"], away)), None)
            if not ev or not ev["completed"]:
                continue
            box = bp.espn_boxscore("NHL", ev["id"], fetch)
            for who in players:
                rec = box.get(bp.norm_name(who))
                goals = bp.player_stat("NHL", "goals", rec) if rec else None
                if goals is not None:
                    out[(game, who)] = goals
        except Exception as e:  # noqa: BLE001
            errors.append(f"{game}: {e}")
    return out, errors


def _summ(rows):
    """rows: [(fd price or None, scored bool)] -> dict of counts and rates."""
    n = len(rows)
    sc = sum(1 for _, s in rows if s)
    priced = [(p, s) for p, s in rows if p is not None]
    profit = sum((bp.decimal_odds(p) - 1) if s else -1.0 for p, s in priced)
    implied = sum(bp.implied(p) for p, _ in priced) / len(priced) if priced else None
    return {"n": n, "scored": sc, "rate": sc / n if n else None, "priced": len(priced),
            "implied": implied, "units": profit, "roi": profit / len(priced) if priced else None}


def _fmt(name, r):
    if not r["n"]:
        return f"  {name:34} none"
    s = f"  {name:34} players {r['n']:4}  scored {r['scored']:3} ({100 * r['rate']:.0f}%)"
    if r["priced"]:
        s += f" | FanDuel price on {r['priced']}: avg implied {100 * r['implied']:.0f}%, {r['units']:+.1f}u, ROI {100 * r['roi']:+.0f}%"
    return s


def report(signals, baseline, goals):
    lines = []
    sig = [(k, v) for k, v in signals.items() if k in goals]
    base = [(k, v) for k, v in baseline.items() if k in goals]
    lines.append(f"Graded: {len(sig)} shots-Over signal players, {len(base)} FanDuel-priced players (finished games).")
    lines.append("")
    lines.append("SHOTS OVER SIGNAL, by soft books off Pinnacle at first sighting")
    for label, f in (("all signals", lambda v: True), ("1 book off", lambda v: v["stale"] == 1),
                     ("2 books off", lambda v: v["stale"] == 2), ("3 books off", lambda v: v["stale"] >= 3),
                     ("3 books off at any check", lambda v: v["max_stale"] >= 3)):
        rows = [(v["fd"], goals[k] >= 1) for k, v in sig if f(v)]
        lines.append(_fmt(label, _summ(rows)))
    lines.append("")
    lines.append("BASELINE: every player FanDuel priced for an anytime goal")
    lines.append(_fmt("all priced players", _summ([(p, goals[k] >= 1) for k, p in base])))
    sig_keys = {k for k, _ in sig}
    lines.append(_fmt("priced players with no signal", _summ([(p, goals[k] >= 1) for k, p in base if k not in sig_keys])))
    lines.append("")
    lines.append("BY PRICE (FanDuel anytime goal, signal players with a price)")
    for label, lo, hi in (("short  (+250 or less)", -9999, 250), ("mid    (+251 to +400)", 251, 400),
                          ("long   (+401 or more)", 401, 99999)):
        rows = [(v["fd"], goals[k] >= 1) for k, v in sig if v["fd"] is not None and lo <= v["fd"] <= hi]
        lines.append(_fmt(label, _summ(rows)))
    return "\n".join(lines)


def main():
    snaps = load_snapshots()
    signals, baseline, commence = collect(snaps)
    print(f"{len(snaps)} NHL props snapshots; {len(signals)} shots-Over signal players; "
          f"{len(baseline)} players with a FanDuel goal price")
    goals, errors = goals_scored(set(signals) | set(baseline), commence)
    for e in errors[:5]:
        print("ESPN error:", e)
    print(report(signals, baseline, goals))
    os.makedirs("/tmp/nhl_goal_backtest", exist_ok=True)
    with open("/tmp/nhl_goal_backtest/rows.json", "w") as f:
        json.dump([{"game": k[0], "who": k[1], "goals": goals.get(k), **v} for k, v in signals.items()], f)


if __name__ == "__main__":
    main()
