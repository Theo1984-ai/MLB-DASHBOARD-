"""
Shared H+R+R Tracker pick generator.

Extracted from pages/2_HR_Tracker.py — produces the same picks that the
"Save H+R+R to GitHub" button would produce.

STRICT filter:
  - best_odds >= -180 (cap juice)
  - implied_pct >= 50
  - top 6 by combined score
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from data import mlb_api, odds as odds_api
from data.stadiums import get_stadium

EASTERN = ZoneInfo("America/New_York")
HRR_POINT = 1.5


def generate_hrr_picks(odds_key, season=None, top_n=6, strict=True):
    """Generate H+R+R picks matching the H+R+R Tracker page Save flow."""
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    if season is None:
        season = int(today[:4])

    sched = mlb_api.get_schedule(today)
    upcoming = []
    now_utc = datetime.now(tz=timezone.utc)
    for g in sched:
        try:
            fp_dt, _ = mlb_api.parse_first_pitch(g.get("gameDate", ""))
            if fp_dt and fp_dt.astimezone(timezone.utc) > now_utc:
                upcoming.append((g, fp_dt))
        except Exception:
            continue
    upcoming.sort(key=lambda x: x[1])

    if not upcoming:
        return {
            "date": today, "saved_at": datetime.now(tz=EASTERN).isoformat(),
            "top_n": top_n, "n_games": 0, "n_total": 0, "point": HRR_POINT, "picks": [],
        }

    all_hrr = []
    for g, _fp in upcoming:
        away_name = g["teams"]["away"]["team"]["name"]
        home_name = g["teams"]["home"]["team"]["name"]
        home_id = g["teams"]["home"]["team"]["id"]
        away_id = g["teams"]["away"]["team"]["id"]

        # Find the Odds API event id by matching team names
        eid = None
        try:
            import json, ssl as _ssl, urllib.request
            _SSL = _ssl._create_unverified_context()
            events = json.loads(urllib.request.urlopen(
                f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={odds_key}",
                timeout=15, context=_SSL).read())
            for e in events:
                if (e["away_team"] == away_name and e["home_team"] == home_name):
                    eid = e["id"]; break
        except Exception:
            pass

        if not eid:
            continue

        hrr_offers = odds_api.get_hrr_odds(odds_key, eid, point=HRR_POINT)
        if not hrr_offers:
            continue

        best_by_player = {}
        for o in hrr_offers:
            n = odds_api.normalize_name(o["player"])
            if n not in best_by_player or o["american_odds"] > best_by_player[n][0]:
                best_by_player[n] = (o["american_odds"], o["bookmaker"], o["player"])

        il = mlb_api.get_team_il(home_id, season) | mlb_api.get_team_il(away_id, season)
        roster_map = {}
        for tid in (home_id, away_id):
            team_name = (home_name if tid == home_id else away_name)
            for r in mlb_api.get_team_roster(tid, season):
                if r.get("position", {}).get("type") == "Pitcher": continue
                person = r.get("person", {})
                pid = person.get("id")
                name = person.get("fullName", "")
                if pid and pid not in il and name:
                    roster_map[odds_api.normalize_name(name)] = (pid, team_name, name)

        away_p = g["teams"]["away"].get("probablePitcher") or {}
        home_p = g["teams"]["home"].get("probablePitcher") or {}
        stadium = get_stadium(home_id)

        for norm, (am, book, _raw_name) in best_by_player.items():
            pid_info = roster_map.get(norm)
            if not pid_info:
                last = norm.split()[-1] if norm else ""
                candidates = [(n, info) for n, info in roster_map.items()
                              if n.split()[-1] == last]
                if len(candidates) == 1:
                    pid_info = candidates[0][1]
            if not pid_info:
                continue
            pid, team, mlb_name = pid_info
            is_home = (team == home_name)
            opp_sp = (away_p.get("fullName", "TBD") if is_home
                      else home_p.get("fullName", "TBD"))
            imp = (100 / (am + 100) if am > 0
                   else abs(am) / (abs(am) + 100))
            all_hrr.append({
                "batter":      mlb_name,
                "batter_id":   int(pid),
                "team":        team,
                "matchup":     f"{away_name} @ {home_name}",
                "game_pk":     g.get("gamePk"),
                "vs_sp":       opp_sp,
                "park":        stadium["park"],
                "point":       HRR_POINT,
                "best_odds":   am,
                "best_book":   book,
                "implied_pct": round(imp * 100, 2),
                "model_p_pct": None,
                "edge_pp":     None,
                # Settler metadata (H+R+R Over HRR_POINT)
                "stat_key":    "hrr",
                "side":        "Over",
                "player":      mlb_name,
                "away_team":   away_name,
                "home_team":   home_name,
                "first_pitch": _fp.isoformat() if _fp else None,
                "best_price":  am,  # alias so settler ROI calc works
            })

    # STRICT filter (matches page)
    if strict:
        qualifying = [
            p for p in all_hrr
            if p.get("best_odds") is not None
            and p["best_odds"] >= -180
            and p.get("implied_pct") is not None
            and p["implied_pct"] >= 50
        ]
        def score(p):
            am = p["best_odds"]; imp = p["implied_pct"]
            juice_bonus = 5 if -180 <= am <= -120 else (10 if -120 < am <= 100 else 0)
            return imp + juice_bonus
        qualifying.sort(key=lambda x: -score(x))
        picks = qualifying[:top_n]
    else:
        all_hrr.sort(key=lambda x: -x["implied_pct"])
        picks = all_hrr[:top_n]

    return {
        "date":     today,
        "saved_at": datetime.now(tz=EASTERN).isoformat(),
        "top_n":    top_n,
        "n_games":  len(upcoming),
        "n_total":  len(all_hrr),
        "point":    HRR_POINT,
        "picks":    picks,
    }
