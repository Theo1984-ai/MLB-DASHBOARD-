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


def find_game(games, away_team, home_team):
    for g in games:
        try:
            a = g["teams"]["away"]["team"]["name"]
            h = g["teams"]["home"]["team"]["name"]
            if a == away_team and h == home_team:
                return g
        except KeyError:
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
        return ("NO_DATA", "game not found in schedule")

    status = (game.get("status") or {}).get("detailedState", "")
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
    player = pick.get("player") or pick.get("selection", "")
    if not player:
        return ("NO_DATA", "no player name")

    # Determine team for the player (best effort — try both teams)
    pid = player_id_cache.get(player)
    if pid is None:
        # Search across both teams (best-effort)
        pid = find_player_id(player, away) or find_player_id(player, home)
        player_id_cache[player] = pid
    if not pid:
        return ("NO_DATA", f"player_id not found: {player}")

    date = pick.get("first_pitch", "")[:10]
    if stat_key == "pitcher_w":
        s = get_pitcher_game_stats(pid, date)
        if not s:
            return ("NO_DATA", "no pitcher stat")
        return ("WIN" if s["win"] else "LOSS", f"win={s['win']}")
    if stat_key == "ks":
        s = get_pitcher_game_stats(pid, date)
        if not s:
            return ("NO_DATA", "no pitcher stat")
        actual = s["ks"]
        if side == "Over":
            if actual > point: return ("WIN", f"ks={actual} vs O{point}")
            if actual < point: return ("LOSS", f"ks={actual} vs O{point}")
            return ("PUSH", f"ks={actual} vs O{point}")
        if side == "Under":
            if actual < point: return ("WIN", f"ks={actual} vs U{point}")
            if actual > point: return ("LOSS", f"ks={actual} vs U{point}")
            return ("PUSH", f"ks={actual} vs U{point}")

    # Batter markets
    s = get_batter_game_stats(pid, date)
    if not s:
        return ("NO_DATA", "no batter stat")

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

def settle_snapshot(date_str):
    path = os.path.join(ROOT, "true_prob_history", f"{date_str}.json")
    if not os.path.exists(path):
        print(f"No snapshot found for {date_str}: {path}")
        return False

    with open(path) as f:
        snap = json.load(f)

    games = get_schedule_with_scores(date_str)
    if not games:
        print(f"No schedule data for {date_str} yet — try again later.")
        return False

    player_id_cache = {}
    n_settled = 0
    n_already = 0
    n_no_data = 0

    for p in snap.get("picks", []):
        if p.get("result") in ("WIN", "LOSS", "PUSH"):
            n_already += 1
            continue
        result, detail = settle_pick(p, games, player_id_cache)
        p["result"] = result
        p["settle_detail"] = detail
        if result in ("WIN", "LOSS", "PUSH"):
            n_settled += 1
        else:
            n_no_data += 1

    snap["settled_at"] = datetime.now(tz=EASTERN).isoformat()

    # Summary stats
    wins = sum(1 for p in snap["picks"] if p.get("result") == "WIN")
    losses = sum(1 for p in snap["picks"] if p.get("result") == "LOSS")
    pushes = sum(1 for p in snap["picks"] if p.get("result") == "PUSH")
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
        # PUSH = 0 P/L

    snap["summary"] = {
        "n_total":   len(snap.get("picks", [])),
        "n_settled": settled,
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
          f"(no_data={n_no_data}, already={n_already})")
    return True


# ---------- CLI ----------

def main():
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        # Default: yesterday (ET)
        yest = datetime.now(tz=EASTERN) - timedelta(days=1)
        date_str = yest.strftime("%Y-%m-%d")
    print(f"Settling True Probability snapshot for {date_str}...")
    settle_snapshot(date_str)


if __name__ == "__main__":
    main()
