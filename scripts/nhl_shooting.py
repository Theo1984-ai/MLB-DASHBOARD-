"""
NHL shooting data: who shot how much and who scored, from ESPN box scores.

Every finished NHL game's box score is saved once to best_plays_log/nhl_shooting/<date>.json as
{"games": {"Away @ Home": {normalized player name: {"s": shots on goal, "g": goals}}}}.
From that we get each player's goals per shot (shrunk toward the league average so a player with 3 shots
is not treated as a 33% shooter) and turn Pinnacle's shots line into an expected shot count. Together:

    chance of a goal  =  1 - exp(-expected shots x goals per shot)

That is the model the goals tracker (scripts/goal_tracker.py) grades against who actually scored.
"""
import math
from datetime import timedelta

from scripts import best_plays as bp

LOG_SUB = "nhl_shooting"
SHRINK_SHOTS = 40            # a player's goals-per-shot counts like this many shots of league-average shooting
DEFAULT_RATE = 0.10          # league goals per shot on goal, used until there is data


def parse_box(data):
    """
    Every player in an ESPN hockey box score: {normalized name: {"team", "pos" (the box score group: forwards,
    defense, goalies), "s", "g", "stats": {every stat label: number, or the text for times like TOI "18:32"}}}.
    Goalies have no S/G of their own; their stats (shots against, saves) are kept under "stats".
    """
    out = {}
    for team in (data.get("boxscore") or {}).get("players", []):
        tname = (team.get("team") or {}).get("displayName", "")
        for group in team.get("statistics", []):
            labels = group.get("labels") or []
            pos = (group.get("name") or group.get("type") or "").lower()
            for a in group.get("athletes", []):
                name = bp.norm_name((a.get("athlete") or {}).get("displayName", ""))
                vals = a.get("stats") or []
                if not name or not vals:
                    continue
                stats = {}
                for lab, val in zip(labels, vals):
                    num = bp._num(val)
                    if num is not None:
                        stats[lab] = num
                    elif val not in (None, "", "--"):
                        stats[lab] = str(val)
                out[name] = {"team": tname, "pos": pos, "s": stats.get("S") if isinstance(stats.get("S"), (int, float)) else None,
                             "g": stats.get("G") if isinstance(stats.get("G"), (int, float)) else None, "stats": stats}
    return out


def toi_minutes(value):
    """'18:32' -> 18.53 minutes (a number is returned as is, anything else as None)."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        m, s = str(value).split(":")
        return int(m) + int(s) / 60
    except Exception:  # noqa: BLE001
        return None


def harvest_date(date, load, save, fetch=None, log_dir="best_plays_log"):
    """
    Save the box score of every finished NHL game on `date`. A game saved in the old format (only shots and goals)
    is fetched again so it gains team, every stat label and the goalies. Returns the games added or upgraded.
    """
    fetch = fetch or bp._get_plain
    path = f"{log_dir}/{LOG_SUB}/{date}.json"
    data = load(path) or {}
    games = data.setdefault("games", {})
    new = 0
    for ev in bp.espn_events("NHL", date, fetch):
        if not ev["completed"]:
            continue
        key = f"{ev['away']} @ {ev['home']}"
        if key in games and any("stats" in r for r in games[key].values()):
            continue
        box = parse_box(fetch(f"{bp.ESPN_API}/{bp.SPORTS['NHL']['espn']}/summary?event={ev['id']}"))
        rows = {}
        for name, rec in box.items():
            if rec["s"] is None and rec["g"] is None and rec["pos"] != "goalies":
                continue
            rec["s"], rec["g"] = rec["s"] or 0, rec["g"] or 0
            rows[name] = rec
        games[key] = rows
        new += 1
    if new:
        save(path, data, f"NHL shooting {date}: {new} games")
    return new


def harvest_recent(now_et, load, save, days=3, fetch=None, log_dir="best_plays_log"):
    """Harvest today and the previous days-1 days (games finish after midnight). Returns the games added."""
    return sum(harvest_date((now_et - timedelta(days=i)).strftime("%Y-%m-%d"), load, save, fetch, log_dir)
               for i in range(days))


def shooting_table(load, list_names, log_dir="best_plays_log"):
    """({normalized name: [shots, goals, games]}, league goals per shot) from every saved day."""
    table = {}
    folder = f"{log_dir}/{LOG_SUB}"
    for name in sorted(n for n in list_names(folder) if n.endswith(".json")):
        for rows in ((load(f"{folder}/{name}") or {}).get("games") or {}).values():
            for who, r in rows.items():
                t = table.setdefault(who, [0, 0, 0])
                t[0] += r.get("s", 0)
                t[1] += r.get("g", 0)
                t[2] += 1
    shots = sum(t[0] for t in table.values())
    goals = sum(t[1] for t in table.values())
    return table, (goals / shots if shots >= 500 else DEFAULT_RATE)


def goal_rate(table, league, who):
    """(goals per shot shrunk toward the league rate, shots, games) for a player."""
    s, g, gp = table.get(bp.norm_name(who), [0, 0, 0])
    return (g + SHRINK_SHOTS * league) / (s + SHRINK_SHOTS), s, gp


def _p_at_least(m, lam):
    """P(Poisson(lam) >= m)."""
    term, cdf = math.exp(-lam), 0.0
    for i in range(m):
        cdf += term
        term *= lam / (i + 1)
    return 1 - cdf


def expected_shots(line, over, under=None):
    """Mean shots implied by Pinnacle's shots line and prices (Poisson fitted so P(over the line) is Pinnacle's)."""
    m = math.floor(line) + 1
    if over is None:
        return line + 0.3
    a = bp.implied(over)
    p = a / (a + bp.implied(under)) if under is not None else a * 0.95
    lo, hi = 0.05, 12.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if _p_at_least(m, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def goal_chance(shots, rate):
    return 1 - math.exp(-shots * rate)
