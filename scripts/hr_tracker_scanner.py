"""
Shared HR Tracker pick generator.

Extracted from pages/2_HR_Tracker.py so daily_all.py can produce the
SAME picks that the page's "Save HR picks to GitHub" button would.

Uses the STRICT filter (top 7, requires odds + edge >= -2pp + confidence >= 45).
"""
from __future__ import annotations

import json
import ssl as _ssl
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from data import mlb_api, savant, weather as wx, odds as odds_api
from data.stadiums import get_stadium
from models import hr_model, calibration as cal_model

_SSL = _ssl._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")


def generate_hr_picks(odds_key, season=None, top_n=7, strict=True):
    """Generate HR picks matching the HR Tracker page Save flow.

    Returns: dict with date, saved_at, top_n, n_games, n_total, picks
    (ready to pass to gh.save_json).
    """
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    if season is None:
        season = int(today[:4])

    # Today's schedule
    sched = mlb_api.get_schedule(today)
    upcoming = []
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    for g in sched:
        try:
            fp_dt, _ = mlb_api.parse_first_pitch(g.get("gameDate", ""))
            if fp_dt and fp_dt.astimezone(ZoneInfo("UTC")) > now_utc:
                upcoming.append((g, fp_dt))
        except Exception:
            continue
    upcoming.sort(key=lambda x: x[1])

    if not upcoming:
        return {
            "date": today, "saved_at": datetime.now(tz=EASTERN).isoformat(),
            "top_n": top_n, "n_games": 0, "n_total": 0, "picks": [],
        }

    # Heavy lifts: load Statcast data once
    bs_df = savant.batter_statcast(season)
    bx_df = savant.batter_xstats(season)
    px_df = savant.pitcher_xstats(season)
    arsenal_df = savant.pitcher_arsenal(season)
    bvp_df = savant.batter_vs_pitch_types(season)
    pxL_df = savant.pitcher_xstats_split(season, "L")
    pxR_df = savant.pitcher_xstats_split(season, "R")
    recent_hitting = mlb_api.get_recent_hitting_leaderboard(season, 15)
    cal_params = cal_model.load_params()

    # Events for odds matching
    events = json.loads(urllib.request.urlopen(
        f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={odds_key}",
        timeout=15, context=_SSL).read())
    event_map = {(e["away_team"] + "|" + e["home_team"]).lower(): e["id"] for e in events}

    all_preds = []
    for g, fp_dt in upcoming:
        away_name = g["teams"]["away"]["team"]["name"]
        home_name = g["teams"]["home"]["team"]["name"]
        home_id = g["teams"]["home"]["team"]["id"]
        away_id = g["teams"]["away"]["team"]["id"]
        stadium = get_stadium(home_id)
        away_p = g["teams"]["away"].get("probablePitcher") or {}
        home_p = g["teams"]["home"].get("probablePitcher") or {}
        away_pid = away_p.get("id"); home_pid = home_p.get("id")
        eid = event_map.get((away_name + "|" + home_name).lower())

        weather_g = wx.get_forecast(stadium["lat"], stadium["lon"], fp_dt) or {}
        home_stats = mlb_api.get_pitcher_season(home_pid, season) if home_pid else mlb_api._empty_pitcher()
        away_stats = mlb_api.get_pitcher_season(away_pid, season) if away_pid else mlb_api._empty_pitcher()
        home_p_savant = away_p_savant = None
        if home_pid and not px_df.empty:
            h = px_df[px_df["player_id"] == home_pid]
            if not h.empty: home_p_savant = h.iloc[0].to_dict()
        if away_pid and not px_df.empty:
            h = px_df[px_df["player_id"] == away_pid]
            if not h.empty: away_p_savant = h.iloc[0].to_dict()
        home_ars = away_ars = None
        if home_pid and not arsenal_df.empty:
            h = arsenal_df[arsenal_df["player_id"] == home_pid]
            if not h.empty: home_ars = h.iloc[0].to_dict()
        if away_pid and not arsenal_df.empty:
            h = arsenal_df[arsenal_df["player_id"] == away_pid]
            if not h.empty: away_ars = h.iloc[0].to_dict()
        home_splits = mlb_api.get_pitcher_splits(home_pid, season) if home_pid else None
        away_splits = mlb_api.get_pitcher_splits(away_pid, season) if away_pid else None
        home_recent = mlb_api.get_pitcher_recent_form(home_pid, season) if home_pid else None
        away_recent = mlb_api.get_pitcher_recent_form(away_pid, season) if away_pid else None

        il = mlb_api.get_team_il(home_id, season) | mlb_api.get_team_il(away_id, season)
        all_pids = []
        for tid in (home_id, away_id):
            for r in mlb_api.get_team_roster(tid, season):
                if r.get("position", {}).get("type") != "Pitcher":
                    pid = r.get("person", {}).get("id")
                    if pid and pid not in il: all_pids.append((pid, tid))
        handedness = mlb_api.get_handedness(list({p for p, _ in all_pids}))

        home_bp = mlb_api.get_team_bullpen_stats(home_id, season) or {}
        away_bp = mlb_api.get_team_bullpen_stats(away_id, season) or {}
        pid_to_spot = {}
        try:
            bx = json.loads(urllib.request.urlopen(
                f"https://statsapi.mlb.com/api/v1/game/{g.get('gamePk')}/boxscore",
                timeout=10, context=_SSL).read())
            for side in ("away", "home"):
                for _, pp in bx.get("teams", {}).get(side, {}).get("players", {}).items():
                    bo = pp.get("battingOrder")
                    if bo:
                        try:
                            spot = int(bo) // 100
                            if 1 <= spot <= 9:
                                pid_to_spot[pp["person"]["id"]] = spot
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

        sharp_odds = {}
        if eid:
            for o in odds_api.get_hr_odds(odds_key, eid, "draftkings,fanduel,betmgm,williamhill_us,bovada"):
                n = odds_api.normalize_name(o["player"])
                if n not in sharp_odds or o["american_odds"] > sharp_odds[n][0]:
                    sharp_odds[n] = (o["american_odds"], o["bookmaker"])

        for pid, tid in all_pids:
            bs_row = bs_df[bs_df["player_id"] == pid]
            if bs_row.empty: continue
            bs = bs_row.iloc[0].to_dict()
            bx_row = bx_df[bx_df["player_id"] == pid] if not bx_df.empty else pd.DataFrame()
            if not bx_row.empty:
                bs["xslg"] = bx_row.iloc[0].get("xslg")
                bs["xwoba"] = bx_row.iloc[0].get("xwoba")
            b_hand = handedness.get(pid, {}).get("bats")
            b_recent = recent_hitting.get(pid)
            b_pitch_perf = None
            if not bvp_df.empty:
                h = bvp_df[bvp_df["player_id"] == pid]
                if not h.empty: b_pitch_perf = h.iloc[0].to_dict()
            opp_stats = home_stats if tid == away_id else away_stats
            opp_pid = home_pid if tid == away_id else away_pid
            opp_savant = home_p_savant if tid == away_id else away_p_savant
            opp_ars = home_ars if tid == away_id else away_ars
            opp_splits = home_splits if tid == away_id else away_splits
            opp_recent = home_recent if tid == away_id else away_recent
            opp_name = home_p.get("fullName", "TBD") if tid == away_id else away_p.get("fullName", "TBD")
            p_hand = handedness.get(opp_pid, {}).get("throws") if opp_pid else None
            p_split = None
            if opp_pid:
                eff_bat = b_hand
                if eff_bat == "S" and p_hand: eff_bat = "R" if p_hand == "L" else "L"
                df_s = pxL_df if eff_bat == "L" else pxR_df
                if not df_s.empty:
                    h = df_s[df_s["player_id"] == opp_pid]
                    if not h.empty: p_split = h.iloc[0].to_dict()

            opp_bullpen = home_bp if tid == away_id else away_bp
            spot = pid_to_spot.get(pid)
            expected_pa = hr_model.expected_pa_for_spot(spot)

            per_pa = hr_model.predict_per_pa(
                batter_savant=bs, pitcher_stats=opp_stats, pitcher_savant=opp_savant,
                park_hr=stadium["hr_factor"], weather=weather_g,
                park_orientation_deg=stadium["orientation_deg"],
                stadium=stadium, batter_hand=b_hand, pitcher_hand=p_hand,
                pitcher_splits=opp_splits, recent_stats=b_recent,
                pitcher_arsenal=opp_ars, batter_pitch_perf=b_pitch_perf,
                pitcher_savant_split=p_split, pitcher_recent_form=opp_recent,
                bullpen_stats=opp_bullpen, lineup_spot=spot,
            )
            per_game = hr_model.predict_per_game(per_pa, expected_pa)
            p_cal = cal_model.apply_calibration(per_game["p_at_least_one"], cal_params)
            bname = bs.get("player_name", "?")
            norm = odds_api.normalize_name(bname)
            odds_data = sharp_odds.get(norm)

            rec = {
                "batter":      bname,
                "batter_id":   int(pid),
                "team":        g["teams"]["away" if tid == away_id else "home"]["team"]["name"],
                "matchup":     f"{away_name} @ {home_name}",
                "game_pk":     g.get("gamePk"),
                "vs_sp":       opp_name,
                "park":        stadium["park"],
                "model_p":     round(p_cal, 4),
                "model_p_pct": round(p_cal * 100, 2),
                "best_odds":   None,
                "best_book":   None,
                "implied_pct": None,
                "edge_pp":     None,
                # Settler metadata (HR market = "did the batter hit a HR?")
                "stat_key":    "hr",
                "side":        "Over",
                "point":       0.5,
                "player":      bname,
                "away_team":   away_name,
                "home_team":   home_name,
                "first_pitch": fp_dt.isoformat() if fp_dt else None,
            }
            if odds_data:
                rec["best_odds"] = odds_data[0]
                rec["best_book"] = odds_data[1]
                imp = (100 / (odds_data[0] + 100) if odds_data[0] > 0
                       else abs(odds_data[0]) / (abs(odds_data[0]) + 100))
                rec["implied_pct"] = round(imp * 100, 2)
                rec["edge_pp"]     = round(p_cal * 100 - imp * 100, 2)
            if hasattr(hr_model, "pick_confidence"):
                conf = hr_model.pick_confidence(rec["model_p_pct"], rec["edge_pp"])
                rec["confidence"]      = conf["score"]
                rec["confidence_tier"] = conf["tier"]
            else:
                rec["confidence"]      = None
                rec["confidence_tier"] = None
            all_preds.append(rec)

    # STRICT filter (matches page logic exactly)
    if strict:
        qualifying = [
            p for p in all_preds
            if p.get("best_odds") is not None
            and (p.get("edge_pp") is None or p["edge_pp"] >= -2)
            and (p.get("confidence") is None or p["confidence"] >= 45)
        ]
        qualifying.sort(key=lambda x: (-x.get("confidence", 0), -x["model_p"]))
        picks = qualifying[:top_n]
    else:
        all_preds.sort(key=lambda x: -x["model_p"])
        picks = all_preds[:top_n]

    return {
        "date":     datetime.now(tz=EASTERN).strftime("%Y-%m-%d"),
        "saved_at": datetime.now(tz=EASTERN).isoformat(),
        "top_n":    top_n,
        "n_games":  len(upcoming),
        "n_total":  len(all_preds),
        "picks":    picks,
    }
