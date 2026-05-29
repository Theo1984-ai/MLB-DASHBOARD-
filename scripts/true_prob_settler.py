"""
Settle True Probability snapshots — grade each pick as WIN/LOSS/PUSH/VOID
using the MLB Stats API the next day after games are final.

Reads true_prob_history/YYYY-MM-DD.json, adds a "result" field to each pick,
and writes the file back in place. Skips already-settled picks.

Usage:
    python scripts/true_prob_settler.py           # settles yesterday
    python scripts/true_prob_settler.py 2026-05-25  # settles a specific date
"""
import json
import os
import ssl as _ssl_compat
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------- MLB Stats API ----------

def _http_json(url, timeout=20):
    try:
        return json.loads(urllib.request.urlopen(url, timeout=timeout,
                                                 context=_UNVERIFIED_SSL).read())
    except Exception:
        return None


def get_schedule_with_scores(date):
    """Returns list of game dicts with final scores for `date` (YYYY-MM-DD)."""
    d = _http_json(f"https://statsapi.mlb.com/api/v1/schedule"
                   f"?sportId=1&date={date}&hydrate=team,linescore")
    if not d:
        return []
    out = []
    for day in d.get("dates", []):
        for g in day.get("games", []):
            out.append(g)
    return out


def _norm(name):
    """Normalize team name for tolerant matching."""
    return (name or "").strip().lower().replace(".", "").replace("  ", " ")


def find_game(games, away_team, home_team):
    """Find a game by team names. Tolerates trailing/leading whitespace and
    minor case/punctuation diffs. Returns the first match (handles
    doubleheaders by picking the first of the two; both have same
    final-score grading for moneyline/spread/total — no impact)."""
    aw_n, hm_n = _norm(away_team), _norm(home_team)
    for g in games:
        try:
            a = g["teams"]["away"]["team"]["name"]
            h = g["teams"]["home"]["team"]["name"]
            if a == away_team and h == home_team:
                return g
            if _norm(a) == aw_n and _norm(h) == hm_n:
                return g
        except KeyError:
            continue
    # Last-resort: match on just the last word of each team name
    # (e.g. "Toronto Blue Jays" -> "Jays" matches "Jays")
    aw_last = aw_n.split()[-1] if aw_n else ""
    hm_last = hm_n.split()[-1] if hm_n else ""
    for g in games:
        try:
            a_last = _norm(g["teams"]["away"]["team"]["name"]).split()[-1]
            h_last = _norm(g["teams"]["home"]["team"]["name"]).split()[-1]
            if a_last == aw_last and h_last == hm_last:
                return g
        except (KeyError, IndexError):
            continue
    return None


def get_batter_game_stats(player_id, date):
    """Returns {hits, hr, tb, rbi, runs, walks} or None."""
    yr = date[:4]
    d = _http_json(f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats"
                   f"?stats=gameLog&group=hitting&season={yr}")
    if not d:
        return None
    for split in (d.get("stats") or [{}])[0].get("splits", []):
        if split.get("date") == date:
            s = split.get("stat", {})
            return {
                "hits":  int(s.get("hits", 0)),
                "hr":    int(s.get("homeRuns", 0)),
                "tb":    int(s.get("totalBases", 0)),
                "rbi":   int(s.get("rbi", 0)),
                "runs":  int(s.get("runs", 0)),
                "walks": int(s.get("baseOnBalls", 0)),
            }
    return None


def get_pitcher_game_stats(player_id, date):
    """Returns {ks, win, loss} or None."""
    yr = date[:4]
    d = _http_json(f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats"
                   f"?stats=gameLog&group=pitching&season={yr}")
    if not d:
        return None
    for split in (d.get("stats") or [{}])[0].get("splits", []):
        if split.get("date") == date:
            s = split.get("stat", {})
            return {
                "ks":   int(s.get("strikeOuts", 0)),
                "win":  bool(s.get("wins", 0)),
                "loss": bool(s.get("losses", 0)),
            }
    return None


def find_player_id(name, team_name):
    """Look up MLB player id by name + team. Best-effort."""
    if not name:
        return None
    # Strip middle-initial style suffixes if present
    parts = name.replace(",", "").strip().split()
    query = " ".join(parts)
    d = _http_json(f"https://statsapi.mlb.com/api/v1/people/search?names={urllib.parse.quote(query)}")
    if not d:
        return None
    people = d.get("people", [])
    if not people:
        return None
    # If multiple matches, prefer one whose currentTeam matches
    for p in people:
        ct = p.get("currentTeam", {}).get("name")
        if ct == team_name:
            return p.get("id")
    return people[0].get("id")


import urllib.parse  # noqa: E402  (kept here to avoid top-level confusion)


# ---------- Settle a single pick ----------

def settle_pick(pick, games, player_id_cache):
    """Returns (result, detail) — result in {WIN, LOSS, PUSH, NO_DATA, VOID}."""
    stat_key = pick.get("stat_key")
    side = pick.get("side")
    point = pick.get("point")
    away = pick.get("away_team")
    home = pick.get("home_team")

    game = find_game(games, away, home)
    if not game:
        return ("NO_DATA", f"game not found ({away} @ {home})")

    status = (game.get("status") or {}).get("detailedState", "")
    # Postponed / cancelled / suspended = sportsbook void = NOT a loss
    if any(s in status for s in ("Postponed", "Cancelled", "Suspended")):
        return ("VOID", f"game {status.lower()}")
    if "Final" not in status:
        return ("NO_DATA", f"game status={status}")

    away_score = game["teams"]["away"].get("score")
    home_score = game["teams"]["home"].get("score")
    if away_score is None or home_score is None:
        return ("NO_DATA", "no final score")

    # ---- Game-line markets ----
    if stat_key == "h2h":
        if side == "Home":
            return ("WIN" if home_score > away_score else "LOSS",
                    f"{away_score}-{home_score}")
        if side == "Away":
            return ("WIN" if away_score > home_score else "LOSS",
                    f"{away_score}-{home_score}")
        return ("NO_DATA", f"unknown h2h side={side}")

    if stat_key == "spread":
        # point > 0: team gets points (covers if loses by less than point)
        # point < 0: team gives points (covers if wins by more than abs(point))
        if side == "Home":
            margin = home_score - away_score
        elif side == "Away":
            margin = away_score - home_score
        else:
            return ("NO_DATA", f"unknown spread side={side}")
        target = -point  # the spread relative to team
        # If team got +1.5 (point=1.5), they win unless they lose by 2+ → margin > -1.5
        if margin > target:
            return ("WIN", f"margin={margin:+d} vs {target:+.1f}")
        if margin < target:
            return ("LOSS", f"margin={margin:+d} vs {target:+.1f}")
        return ("PUSH", f"margin={margin:+d} vs {target:+.1f}")

    if stat_key == "total":
        total = away_score + home_score
        if side == "Over":
            if total > point: return ("WIN", f"total={total} vs O{point}")
            if total < point: return ("LOSS", f"total={total} vs O{point}")
            return ("PUSH", f"total={total} vs O{point}")
        if side == "Under":
            if total < point: return ("WIN", f"total={total} vs U{point}")
            if total > point: return ("LOSS", f"total={total} vs U{point}")
            return ("PUSH", f"total={total} vs U{point}")
        return ("NO_DATA", f"unknown total side={side}")

    # ---- Player props ----
    # Tolerate HR/H+R+R legacy field names: batter, batter_id
    player = (pick.get("player") or pick.get("selection")
              or pick.get("batter") or "")
    if not player:
        return ("NO_DATA", "no player name")

    # Prefer pre-recorded batter_id (HR/H+R+R trackers store it directly)
    pid = pick.get("batter_id") or player_id_cache.get(player)
    if pid is None:
        # Search across both teams (best-effort)
        pid = find_player_id(player, away) or find_player_id(player, home)
        player_id_cache[player] = pid
    if not pid:
        return ("NO_DATA", f"player_id not found: {player}")

    date = pick.get("first_pitch", "")[:10] if pick.get("first_pitch") else ""
    # Fallback for old HR/H+R+R saves that lack first_pitch — use snapshot date
    if not date:
        date = pick.get("date") or pick.get("snapshot_date", "")
    if stat_key == "pitcher_w":
        s = get_pitcher_game_stats(pid, date)
        if not s:
            return ("VOID", f"pitcher did not start ({player})")
        return ("WIN" if s["win"] else "LOSS", f"win={s['win']}")
    if stat_key == "ks":
        s = get_pitcher_game_stats(pid, date)
        if not s:
            return ("VOID", f"pitcher did not start ({player})")
        actual = s["ks"]
        if side == "Over":
            if actual > point: return ("WIN", f"ks={actual} vs O{point}")
            if actual < point: return ("LOSS", f"ks={actual} vs O{point}")
            return ("PUSH", f"ks={actual} vs O{point}")
        if side == "Under":
            if actual < point: return ("WIN", f"ks={actual} vs U{point}")
            if actual > point: return ("LOSS", f"ks={actual} vs U{point}")
            return ("PUSH", f"ks={actual} vs U{point}")

    # Batter markets — no game log entry for the date = batter didn't play
    # (rest day / late scratch / sub used). Sportsbooks void these.
    s = get_batter_game_stats(pid, date)
    if not s:
        return ("VOID", f"batter did not play ({player})")

    actual = None
    if stat_key == "hr":    actual = s["hr"]
    elif stat_key == "hits": actual = s["hits"]
    elif stat_key == "tb":   actual = s["tb"]
    elif stat_key == "rbi":  actual = s["rbi"]
    elif stat_key == "runs": actual = s["runs"]
    elif stat_key == "walks": actual = s["walks"]
    elif stat_key == "hrr":  actual = s["hits"] + s["runs"] + s["rbi"]

    if actual is None:
        return ("NO_DATA", f"unknown stat_key={stat_key}")

    if side == "Over":
        if actual > point: return ("WIN", f"{stat_key}={actual} vs O{point}")
        if actual < point: return ("LOSS", f"{stat_key}={actual} vs O{point}")
        return ("PUSH", f"{stat_key}={actual} vs O{point}")
    if side == "Under":
        if actual < point: return ("WIN", f"{stat_key}={actual} vs U{point}")
        if actual > point: return ("LOSS", f"{stat_key}={actual} vs U{point}")
        return ("PUSH", f"{stat_key}={actual} vs U{point}")
    if side == "Yes":
        # e.g. pitcher_w / batter_walks legacy
        return ("WIN" if actual >= 1 else "LOSS", f"{stat_key}={actual}")
    return ("NO_DATA", f"unknown side={side}")


# ---------- Settle a whole snapshot ----------

def settle_snapshot(date_str, history_dir="true_prob_history"):
    """Settle a snapshot file. Default settles True Prob snapshots, but
    can also settle Soft Scanner snapshots, HR Tracker, H+R+R Tracker,
    or any compatible file by passing the appropriate directory name."""
    path = os.path.join(ROOT, history_dir, f"{date_str}.json")
    if not os.path.exists(path):
        print(f"No snapshot found for {date_str}: {path}")
        return False

    # For HR/H+R+R: if picks don't have settle metadata yet (pre-fix saves),
    # inject defaults so settling still works.
    def _backfill_settle_fields(p, default_stat_key, default_point):
        if "stat_key" not in p or p.get("stat_key") is None:
            p["stat_key"] = default_stat_key
        if "side" not in p:
            p["side"] = "Over"
        if "point" not in p or p.get("point") is None:
            p["point"] = default_point
        if "player" not in p:
            p["player"] = p.get("batter", "")
        # Parse matchup -> away/home if not set
        mu = p.get("matchup", "")
        if "away_team" not in p and " @ " in mu:
            p["away_team"], p["home_team"] = mu.split(" @ ", 1)
        if "first_pitch" not in p or not p.get("first_pitch"):
            p["first_pitch"] = date_str + "T00:00:00"
        # Alias best_price for ROI math
        if "best_price" not in p and p.get("best_odds") is not None:
            p["best_price"] = p["best_odds"]

    with open(path) as f:
        snap = json.load(f)

    games = get_schedule_with_scores(date_str)
    if not games:
        print(f"No schedule data for {date_str} yet — try again later.")
        return False

    # Some picks may have a first_pitch on a different date than the snapshot
    # (e.g. cron ran late and captured tomorrow's slate). Pre-load schedules
    # for any other dates referenced by picks.
    extra_schedules = {}
    for p in snap.get("picks", []):
        fp = (p.get("first_pitch") or "")[:10]
        if fp and fp != date_str and fp not in extra_schedules:
            extra_schedules[fp] = get_schedule_with_scores(fp)
    if extra_schedules:
        print(f"  Also loaded schedules for {sorted(extra_schedules)}")

    # Apply backfill for older HR/H+R+R saves that lack settle metadata
    if history_dir == "hr_tracker":
        for p in snap.get("picks", []):
            _backfill_settle_fields(p, default_stat_key="hr", default_point=0.5)
    elif history_dir == "hrr_tracker":
        for p in snap.get("picks", []):
            _backfill_settle_fields(p, default_stat_key="hrr", default_point=1.5)

    player_id_cache = {}
    n_settled = 0
    n_already = 0
    n_void = 0
    n_no_data = 0

    # IMPORTANT: re-settle picks marked NO_DATA previously, in case the
    # settler logic improved since the last run. PUSH/VOID/WIN/LOSS are
    # final outcomes that don't change.
    FINAL = ("WIN", "LOSS", "PUSH", "VOID")
    for p in snap.get("picks", []):
        if p.get("result") in FINAL:
            n_already += 1
            continue
        # Use the pick's first_pitch date if it differs from snapshot date
        fp_date = (p.get("first_pitch") or "")[:10]
        games_for_pick = (extra_schedules.get(fp_date)
                          if fp_date and fp_date != date_str else None)
        if games_for_pick is None:
            games_for_pick = games
        result, detail = settle_pick(p, games_for_pick, player_id_cache)
        p["result"] = result
        p["settle_detail"] = detail
        if result in ("WIN", "LOSS", "PUSH"):
            n_settled += 1
        elif result == "VOID":
            n_void += 1
        else:
            n_no_data += 1

    snap["settled_at"] = datetime.now(tz=EASTERN).isoformat()

    # Summary stats — VOIDs excluded from W/L AND from ROI (book refunds them)
    wins = sum(1 for p in snap["picks"] if p.get("result") == "WIN")
    losses = sum(1 for p in snap["picks"] if p.get("result") == "LOSS")
    pushes = sum(1 for p in snap["picks"] if p.get("result") == "PUSH")
    voids = sum(1 for p in snap["picks"] if p.get("result") == "VOID")
    settled = wins + losses + pushes
    hit_rate = wins / (wins + losses) * 100 if (wins + losses) else 0

    # ROI
    risk_total = 0.0
    profit_total = 0.0
    for p in snap["picks"]:
        r = p.get("result")
        if r not in ("WIN", "LOSS", "PUSH"):
            continue
        am = p.get("best_price", 0)
        if am > 0:
            risk = 100; payout = am
        else:
            risk = abs(am); payout = 100
        risk_total += risk
        if r == "WIN":
            profit_total += payout
        elif r == "LOSS":
            profit_total -= risk
        # PUSH / VOID = 0 P/L

    snap["summary"] = {
        "n_total":   len(snap.get("picks", [])),
        "n_settled": settled,
        "n_void":    voids,
        "n_no_data": n_no_data,
        "wins":      wins,
        "losses":    losses,
        "pushes":    pushes,
        "hit_rate":  round(hit_rate, 1),
        "risk_total":   round(risk_total, 2),
        "profit_total": round(profit_total, 2),
        "roi_pct":   round(profit_total / risk_total * 100, 2) if risk_total else 0,
    }

    with open(path, "w") as f:
        json.dump(snap, f, indent=2)

    print(f"Settled {date_str}: {wins}W/{losses}L/{pushes}P  "
          f"hit_rate={hit_rate:.1f}%  ROI={snap['summary']['roi_pct']:+.1f}%  "
          f"(void={voids}, no_data={n_no_data}, already={n_already})")
    return True


# ---------- CLI ----------

def main():
    """CLI usage:
        python true_prob_settler.py                          # yesterday, True Prob
        python true_prob_settler.py 2026-05-27               # specific date, True Prob
        python true_prob_settler.py 2026-05-27 soft_scanner_history  # other dir
    """
    args = sys.argv[1:]
    history_dir = "true_prob_history"
    date_str = None
    # Date looks like YYYY-MM-DD; anything else is the directory name.
    import re
    date_pat = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for a in args:
        if date_pat.match(a):
            date_str = a
        else:
            history_dir = a
    if date_str is None:
        yest = datetime.now(tz=EASTERN) - timedelta(days=1)
        date_str = yest.strftime("%Y-%m-%d")
    label = "True Probability" if history_dir == "true_prob_history" else history_dir
    print(f"Settling {label} snapshot for {date_str}...")
    settle_snapshot(date_str, history_dir=history_dir)


if __name__ == "__main__":
    main()
