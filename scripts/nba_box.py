"""
NBA box scores: minutes, points and three-pointers made and attempted per player per game, from ESPN.

Every finished NBA game's box score is saved once to best_plays_log/nba_box/<date>.json as
{"games": {"Away @ Home": {normalized player name: {"team", "min", "pts", "fg3m", "fg3a", "stats": {...}}}}}.
From that we get each player's real three-point attempts per game, to compare with the line on the threes plays
(does a player who really shoots 5 threes a game make a 1.5 Over safer than one who shoots 3?).
Players who did not play (no stats) are left out.
"""
from datetime import timedelta

from scripts import best_plays as bp

LOG_SUB = "nba_box"


def _made_att(val):
    """'3-7' -> (3, 7); anything else -> None."""
    try:
        m, a = str(val).split("-")
        return int(m), int(a)
    except Exception:  # noqa: BLE001
        return None


def parse_box(data):
    """Every NBA player who played in an ESPN box score: {name: {team, min, pts, fg3m, fg3a, stats}}."""
    out = {}
    for team in (data.get("boxscore") or {}).get("players", []):
        tname = (team.get("team") or {}).get("displayName", "")
        for group in team.get("statistics", []):
            labels = group.get("labels") or []
            for a in group.get("athletes", []):
                name = bp.norm_name((a.get("athlete") or {}).get("displayName", ""))
                vals = a.get("stats") or []
                if not name or not vals:
                    continue
                stats = {}
                for lab, val in zip(labels, vals):
                    ma = _made_att(val) if lab in ("FG", "3PT", "FT") else None
                    if ma:
                        stats[lab + "M"], stats[lab + "A"] = ma
                    else:
                        num = bp._num(val)
                        if num is not None:
                            stats[lab] = num
                        elif val not in (None, "", "--"):
                            stats[lab] = str(val)
                minutes = stats.get("MIN")
                if not isinstance(minutes, (int, float)) or minutes <= 0:
                    continue                                    # did not play
                out[name] = {"team": tname, "min": minutes, "pts": stats.get("PTS", 0),
                             "fg3m": stats.get("3PTM", 0), "fg3a": stats.get("3PTA", 0), "stats": stats}
    return out


def harvest_date(date, load, save, fetch=None, log_dir="best_plays_log"):
    """Save the box score of every finished NBA game on `date` not saved yet. Returns the games added."""
    fetch = fetch or bp._get_plain
    path = f"{log_dir}/{LOG_SUB}/{date}.json"
    data = load(path) or {}
    games = data.setdefault("games", {})
    new = 0
    for ev in bp.espn_events("NBA", date, fetch):
        if not ev["completed"]:
            continue
        key = f"{ev['away']} @ {ev['home']}"
        if key in games:
            continue
        games[key] = parse_box(fetch(f"{bp.ESPN_API}/{bp.SPORTS['NBA']['espn']}/summary?event={ev['id']}"))
        new += 1
    if new:
        save(path, data, f"NBA box scores {date}: {new} games")
    return new


def harvest_recent(now_et, load, save, days=3, fetch=None, log_dir="best_plays_log"):
    """Harvest today and the previous days-1 days (games finish after midnight). Returns the games added."""
    return sum(harvest_date((now_et - timedelta(days=i)).strftime("%Y-%m-%d"), load, save, fetch, log_dir)
               for i in range(days))
