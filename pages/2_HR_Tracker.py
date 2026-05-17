"""
HR Tracker — Daily Top-10 HR Picks + Performance History

Saves top-10 HR predictions to GitHub each day (permanent storage that
survives Streamlit Cloud redeploys). After games settle, cross-references
against MLB box scores and shows hit rate, P&L, calibration over time.

Workflow:
  1. Open this page in the afternoon (after lineups posted)
  2. Click "Save today's top 10" — picks commit to GitHub
  3. Anytime later, click "Refresh results" to see how past picks did
"""
import os
import sys
import json
import ssl as _ssl_compat
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd

_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()

# Ensure project root on path so we can import siblings
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from data import mlb_api, savant, weather as wx, odds as odds_api
from data import github_storage as gh
from data.stadiums import get_stadium
from models import hr_model, calibration as cal_model

EASTERN = ZoneInfo("America/New_York")
OWNER = "Theo1984-ai"
REPO = "MLB-DASHBOARD-"
TRACKER_DIR = "hr_tracker"
TOP_N = 10

st.set_page_config(page_title="HR Tracker", page_icon="📊", layout="wide")
st.title("📊 HR Tracker — Daily Top 10")
st.caption(
    "Saves the model's top 10 HR predictions each day to GitHub. "
    "Tracks hit rate + theoretical P&L over time so we can verify the model's edge."
)

# ---------- Auth ----------

def resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


GH_TOKEN = resolve_secret("GITHUB_TOKEN")
ODDS_KEY = resolve_secret("THE_ODDS_API_KEY")

if not GH_TOKEN:
    st.error(
        "Missing `GITHUB_TOKEN` in Streamlit Cloud secrets. "
        "Generate a fine-grained PAT with Contents R/W on `MLB-DASHBOARD-` and add it to secrets.toml."
    )
    st.stop()
if not ODDS_KEY:
    st.error("Missing `THE_ODDS_API_KEY`.")
    st.stop()


# ---------- Cached loaders ----------

@st.cache_data(ttl=300, show_spinner=False)
def cached_list_tracker_files():
    files = gh.list_dir(GH_TOKEN, OWNER, REPO, TRACKER_DIR)
    return [f for f in files if f["name"].endswith(".json") and not f["name"].startswith("_")]

@st.cache_data(ttl=600, show_spinner=False)
def cached_load_tracker(path):
    return gh.load_json(GH_TOKEN, OWNER, REPO, path)

@st.cache_data(ttl=600, show_spinner=False)
def cached_fetch_hrs(game_pk):
    try:
        data = json.loads(urllib.request.urlopen(
            f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore",
            timeout=10, context=_UNVERIFIED_SSL).read())
        hrs = set()
        for side in ("away", "home"):
            for _, p in data.get("teams", {}).get(side, {}).get("players", {}).items():
                if (p.get("stats", {}).get("batting", {}).get("homeRuns", 0) or 0) > 0:
                    hrs.add(p["person"]["id"])
        return list(hrs)
    except Exception:
        return None


# ---------- Top section: save today's picks ----------

today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
now_utc = datetime.now(tz=timezone.utc)
season = datetime.now(tz=EASTERN).year

st.markdown(f"### Today: **{today}**")

c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    save_btn = st.button("Save today's top 10", type="primary", use_container_width=True)
with c2:
    refresh_btn = st.button("Refresh", use_container_width=True)
    if refresh_btn:
        cached_list_tracker_files.clear()
        cached_load_tracker.clear()
        cached_fetch_hrs.clear()
        st.rerun()

if save_btn:
    with st.spinner("Running model + pulling sharp odds + saving to GitHub..."):
        try:
            # Get upcoming games
            all_games = mlb_api.get_schedule(today)
            upcoming = []
            for g in all_games:
                fp_dt, _ = mlb_api.parse_first_pitch(g.get("gameDate", ""))
                if fp_dt and fp_dt.astimezone(timezone.utc) > now_utc:
                    upcoming.append((g, fp_dt))
            if not upcoming:
                st.warning("No upcoming games — nothing to save. Try earlier in the day.")
                st.stop()

            # Load model data
            bs_df = savant.batter_statcast(season)
            bx_df = savant.batter_xstats(season)
            px_df = savant.pitcher_xstats(season)
            arsenal_df = savant.pitcher_arsenal(season)
            bvp_df = savant.batter_vs_pitch_types(season)
            pxL_df = savant.pitcher_xstats_split(season, "L")
            pxR_df = savant.pitcher_xstats_split(season, "R")
            recent_hitting = mlb_api.get_recent_hitting_leaderboard(season, 15)
            cal_params = cal_model.load_params()

            events = json.loads(urllib.request.urlopen(
                f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={ODDS_KEY}",
                timeout=15, context=_UNVERIFIED_SSL).read())
            event_map = {(e["away_team"] + "|" + e["home_team"]).lower(): e["id"] for e in events}

            all_preds = []
            progress = st.progress(0)
            for gi, (g, fp_dt) in enumerate(upcoming):
                progress.progress((gi + 1) / len(upcoming))
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

                sharp_odds = {}
                if eid:
                    for o in odds_api.get_hr_odds(ODDS_KEY, eid, "draftkings,fanduel,betmgm,caesars"):
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

                    per_pa = hr_model.predict_per_pa(
                        batter_savant=bs, pitcher_stats=opp_stats, pitcher_savant=opp_savant,
                        park_hr=stadium["hr_factor"], weather=weather_g,
                        park_orientation_deg=stadium["orientation_deg"],
                        stadium=stadium, batter_hand=b_hand, pitcher_hand=p_hand,
                        pitcher_splits=opp_splits, recent_stats=b_recent,
                        pitcher_arsenal=opp_ars, batter_pitch_perf=b_pitch_perf,
                        pitcher_savant_split=p_split, pitcher_recent_form=opp_recent,
                    )
                    per_game = hr_model.predict_per_game(per_pa, 4.3)
                    p_cal = cal_model.apply_calibration(per_game["p_at_least_one"], cal_params)
                    bname = bs.get("player_name", "?")
                    norm = odds_api.normalize_name(bname)
                    odds_data = sharp_odds.get(norm)

                    # Always include all keys (None if no odds) so DataFrame columns
                    # are stable across picks. Avoids "X not in index" errors when
                    # displaying mixed odds/no-odds picks.
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
                    }
                    if odds_data:
                        rec["best_odds"] = odds_data[0]
                        rec["best_book"] = odds_data[1]
                        imp = (100 / (odds_data[0] + 100) if odds_data[0] > 0
                               else abs(odds_data[0]) / (abs(odds_data[0]) + 100))
                        rec["implied_pct"] = round(imp * 100, 2)
                        rec["edge_pp"]     = round(p_cal * 100 - imp * 100, 2)
                    all_preds.append(rec)

            progress.empty()
            all_preds.sort(key=lambda x: -x["model_p"])
            top_n = all_preds[:TOP_N]
            payload = {
                "date":     today,
                "saved_at": datetime.now(tz=EASTERN).isoformat(),
                "top_n":    TOP_N,
                "n_games":  len(upcoming),
                "n_total":  len(all_preds),
                "picks":    top_n,
            }
            path = f"{TRACKER_DIR}/{today}.json"
            gh.save_json(GH_TOKEN, OWNER, REPO, path, payload,
                         commit_msg=f"HR tracker: top {TOP_N} for {today}")
            cached_list_tracker_files.clear()
            cached_load_tracker.clear()
            st.success(f"Saved {len(top_n)} picks to GitHub at `{path}`.")
        except Exception as e:
            st.error(f"Save failed: {e}")

    # Display saved picks (defensive — reindex so missing cols become NaN)
    if "top_n" in locals() and top_n:
        cols = ["batter", "team", "matchup", "model_p_pct", "implied_pct",
                "edge_pp", "best_odds", "best_book"]
        try:
            df = pd.DataFrame(top_n).reindex(columns=cols).rename(columns={
                "model_p_pct": "Model %", "implied_pct": "Mkt %",
                "edge_pp": "Edge", "best_odds": "Odds", "best_book": "Book",
            })
            st.dataframe(df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.caption(f"(Display preview unavailable: {e})")


# ---------- History section ----------

st.markdown("---")
st.markdown("### 📈 Tracker History & Results")

with st.spinner("Loading history from GitHub..."):
    tracker_files = cached_list_tracker_files()

if not tracker_files:
    st.info("No tracker files yet. Click **Save today's top 10** above to get started.")
    st.stop()

# Sort by date desc
tracker_files.sort(key=lambda f: f["name"], reverse=True)
st.caption(f"📦 {len(tracker_files)} days saved in GitHub")

# Aggregate results
daily_rows = []
calibration_pairs = []

for tf in tracker_files:
    date = tf["name"][:-5]
    if date >= today:
        continue   # Skip today (games may not be settled)
    slate = cached_load_tracker(tf["path"])
    if not slate: continue
    picks = slate.get("picks", [])
    if not picks: continue

    # Resolve outcomes
    game_pks = list({p["game_pk"] for p in picks if p.get("game_pk")})
    hr_map = {gpk: set(cached_fetch_hrs(gpk) or []) for gpk in game_pks}

    top5 = picks[:5]
    top10 = picks[:10]
    t5_hits = sum(1 for p in top5 if p["batter_id"] in hr_map.get(p["game_pk"], set()))
    t10_hits = sum(1 for p in top10 if p["batter_id"] in hr_map.get(p["game_pk"], set()))

    # P&L on top 5 at $10 flat at sharp odds
    pnl5 = 0; bet5 = 0
    for p in top5:
        if p.get("best_odds") is None: continue
        bet5 += 1
        won = p["batter_id"] in hr_map.get(p["game_pk"], set())
        if won:
            am = p["best_odds"]
            dec = 1 + am/100 if am > 0 else 1 + 100/abs(am)
            pnl5 += 10 * dec - 10
        else:
            pnl5 -= 10
    daily_rows.append({
        "Date":        date,
        "Top 5":       f"{t5_hits}/5",
        "Top 10":      f"{t10_hits}/10",
        "$ Bet":       bet5 * 10,
        "PnL@$10":     round(pnl5, 2),
        "AvgModel%":   round(sum(p["model_p_pct"] for p in top5) / max(1, len(top5)), 1),
    })

    for p in top10:
        if p.get("game_pk") is None: continue
        actual = 1 if p["batter_id"] in hr_map.get(p["game_pk"], set()) else 0
        calibration_pairs.append((p["model_p_pct"], actual))

# Aggregate metrics
if daily_rows:
    n_days = len(daily_rows)
    total_t5 = sum(int(r["Top 5"].split("/")[0]) for r in daily_rows)
    total_t10 = sum(int(r["Top 10"].split("/")[0]) for r in daily_rows)
    total_pnl = sum(r["PnL@$10"] for r in daily_rows)
    total_bet = sum(r["$ Bet"] for r in daily_rows)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Days tracked", n_days)
    m2.metric("Top 5 hit rate", f"{total_t5}/{n_days*5}",
              delta=f"{total_t5/(n_days*5)*100:.1f}% vs ~10% baseline")
    m3.metric("Top 10 hit rate", f"{total_t10}/{n_days*10}",
              delta=f"{total_t10/(n_days*10)*100:.1f}%")
    if total_bet:
        roi = total_pnl / total_bet * 100
        m4.metric("Top 5 P&L (flat $10)", f"${total_pnl:+.2f}",
                  delta=f"{roi:+.1f}% ROI")

    st.markdown("#### Daily breakdown")
    df = pd.DataFrame(daily_rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

# Calibration view
if calibration_pairs:
    st.markdown("#### Calibration: predicted vs actual hit rate")
    bins = [(0, 10), (10, 15), (15, 20), (20, 30), (30, 100)]
    cal_rows = []
    for lo, hi in bins:
        chunk = [(p, a) for p, a in calibration_pairs if lo <= p < hi]
        if not chunk: continue
        n = len(chunk)
        avg_pred = sum(p for p, _ in chunk) / n
        actual_rate = sum(a for _, a in chunk) / n * 100
        cal_rows.append({
            "Bin":          f"{lo}-{hi}%",
            "N":            n,
            "Pred Avg %":   round(avg_pred, 2),
            "Actual %":     round(actual_rate, 2),
            "Diff pp":      round(avg_pred - actual_rate, 2),
        })
    st.dataframe(pd.DataFrame(cal_rows), use_container_width=True, hide_index=True)

# ---------- Per-day picks viewer ----------
st.markdown("---")
st.markdown("#### 🔎 View picks for a specific day")

date_options = [tf["name"][:-5] for tf in tracker_files]
default_idx = 0   # most recent (already sorted desc above)
selected_date = st.selectbox(
    "Select a saved date",
    options=date_options,
    index=default_idx,
    help="Pick any day from your saved history to see the full top-10 with hit/miss results.",
)

if selected_date:
    sel_file = next((tf for tf in tracker_files if tf["name"] == f"{selected_date}.json"), None)
    if sel_file:
        slate = cached_load_tracker(sel_file["path"])
        if slate and slate.get("picks"):
            picks = slate["picks"]
            is_today = (selected_date >= today)

            # Fetch HR outcomes for that day's games (if past)
            hr_map = {}
            if not is_today:
                game_pks = list({p["game_pk"] for p in picks if p.get("game_pk")})
                hr_map = {gpk: set(cached_fetch_hrs(gpk) or []) for gpk in game_pks}

            # Build display rows
            display_rows = []
            for i, p in enumerate(picks, 1):
                won = None
                if not is_today and p.get("game_pk") in hr_map:
                    won = p["batter_id"] in hr_map[p["game_pk"]]
                result = "🟢 HR" if won is True else ("⚪ —" if won is False else "⏳ pending")
                display_rows.append({
                    "#":        i,
                    "Result":   result,
                    "Batter":   p.get("batter", "?"),
                    "Team":     p.get("team", "?"),
                    "Matchup":  p.get("matchup", "?"),
                    "vs SP":    p.get("vs_sp", "?"),
                    "Park":     p.get("park", "?"),
                    "Model %":  p.get("model_p_pct"),
                    "Mkt %":    p.get("implied_pct"),
                    "Edge pp":  p.get("edge_pp"),
                    "Odds":     p.get("best_odds"),
                    "Book":     p.get("best_book"),
                })

            # Summary line
            if not is_today and hr_map:
                t5_hits = sum(1 for p in picks[:5] if p["batter_id"] in hr_map.get(p["game_pk"], set()))
                t10_hits = sum(1 for p in picks[:10] if p["batter_id"] in hr_map.get(p["game_pk"], set()))
                # Top-5 P&L if odds attached
                pnl, bet = 0.0, 0
                for p in picks[:5]:
                    if p.get("best_odds") is None: continue
                    bet += 1
                    won = p["batter_id"] in hr_map.get(p["game_pk"], set())
                    if won:
                        am = p["best_odds"]
                        dec = 1 + am/100 if am > 0 else 1 + 100/abs(am)
                        pnl += 10 * dec - 10
                    else:
                        pnl -= 10
                summary = f"**Top 5: {t5_hits}/5** • **Top 10: {t10_hits}/10**"
                if bet:
                    summary += f" • **P&L @ $10 flat: ${pnl:+.2f}** ({bet} bet{'s' if bet != 1 else ''})"
                st.markdown(summary)
            else:
                n_odds = sum(1 for p in picks if p.get("best_odds") is not None)
                st.markdown(f"⏳ Games haven't settled yet • {n_odds}/{len(picks)} picks have sharp odds attached")

            # Build DataFrame, then coerce numeric cols so NumberColumn doesn't
            # crash on object-dtype columns (happens when every pick has None odds).
            df_view = pd.DataFrame(display_rows)
            for col in ("Model %", "Mkt %", "Edge pp", "Odds"):
                if col in df_view.columns:
                    df_view[col] = pd.to_numeric(df_view[col], errors="coerce")
            st.dataframe(
                df_view,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Model %":  st.column_config.NumberColumn(format="%.2f%%"),
                    "Mkt %":    st.column_config.NumberColumn(format="%.2f%%"),
                    "Edge pp":  st.column_config.NumberColumn(format="%+.2f"),
                    "Odds":     st.column_config.NumberColumn(format="%+d"),
                },
            )
            st.caption(
                f"Saved at {slate.get('saved_at', '?')[:19]}  •  "
                f"{slate.get('n_games', '?')} games, {slate.get('n_total', '?')} total predictions"
            )
        else:
            st.info(f"No picks found in {selected_date}.json.")
