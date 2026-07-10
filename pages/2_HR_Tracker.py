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
HRR_TRACKER_DIR = "hrr_tracker"
TOP_N = 10
HRR_POINT = 1.5   # H+R+R threshold to target (Over 1.5 = needs any 2 of H/R/RBI)

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


@st.cache_data(ttl=600, show_spinner=False)
def cached_fetch_hrr_map(game_pk):
    """Returns {batter_id: H+R+RBI total} for a finalized game, or None on failure."""
    try:
        data = json.loads(urllib.request.urlopen(
            f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore",
            timeout=10, context=_UNVERIFIED_SSL).read())
        out = {}
        for side in ("away", "home"):
            for _, p in data.get("teams", {}).get(side, {}).get("players", {}).items():
                bs = p.get("stats", {}).get("batting", {})
                h   = bs.get("hits") or 0
                r   = bs.get("runs") or 0
                rbi = bs.get("rbi")  or 0
                # Only include actual batters (someone with a plate appearance)
                if (bs.get("plateAppearances") or 0) > 0 or h + r + rbi > 0:
                    out[p["person"]["id"]] = h + r + rbi
        return out
    except Exception:
        return None


# ---------- Top section: save today's picks ----------

today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
now_utc = datetime.now(tz=timezone.utc)
season = datetime.now(tz=EASTERN).year

st.markdown(f"### Today: **{today}**")

c1, c2, c3 = st.columns([1.3, 1.3, 1])
with c1:
    save_btn = st.button("🔍 Preview HR top 10", type="primary", use_container_width=True)
with c2:
    save_hrr_btn = st.button(f"🔍 Preview H+R+R top 10 (O{HRR_POINT})",
                             use_container_width=True)
with c3:
    refresh_btn = st.button("🔄 Refresh", use_container_width=True,
                            help="Clear cached GitHub history + boxscore results")
    if refresh_btn:
        cached_list_tracker_files.clear()
        cached_load_tracker.clear()
        cached_fetch_hrs.clear()
        cached_fetch_hrr_map.clear()
        # Also clear preview state
        for k in ("hr_preview_picks", "hr_preview_meta", "hr_preview_at",
                  "hrr_preview_picks", "hrr_preview_meta", "hrr_preview_at"):
            st.session_state.pop(k, None)
        st.rerun()

st.caption(
    "Click **Preview** to fetch picks (uses Odds API + MLB API but does NOT "
    "save anything). After reviewing, click **💾 Save to GitHub** beneath the "
    "preview table to persist the slate."
)

if save_btn:
    with st.spinner("Running Fundamentals HR strategy (Barrel% composite + sharp odds)..."):
        try:
            from scripts.hr_tracker_scanner import generate_hr_picks
            payload = generate_hr_picks(ODDS_KEY, season=season, top_n=8, strict=True)
            top_n = payload["picks"]
            n_a_plus = payload.get("n_a_plus", 0)
            n_a = payload.get("n_a", 0)

            st.session_state["hr_preview_picks"] = top_n
            st.session_state["hr_preview_meta"] = {
                "n_games": payload.get("n_games", 0),
                "n_total": payload.get("n_total", 0),
                "n_a_plus": n_a_plus,
                "n_a":      n_a,
                "filter":   "Fundamentals HR",
                "strategy": payload.get("strategy"),
            }
            st.session_state["hr_preview_at"] = datetime.now(tz=EASTERN).isoformat()
            if not top_n:
                st.info(
                    f"No qualifying HR plays tonight. "
                    f"Fundamentals produced {payload.get('n_total',0)} candidates but "
                    f"none passed the sweet-spot filter (composite 90-94 at +300-399) "
                    f"nor the broader tier (composite 85+ at +250-499). "
                    f"This is normal — the sweet spot averages ~1-2 picks/day."
                )
            else:
                st.success(
                    f"Preview ready: **{n_a_plus} A+** (sweet spot) + **{n_a} A** picks. "
                    f"Strategy: Fundamentals HR (Barrel% composite, +19.5% ROI on the "
                    f"A+ tier historically)."
                )
        except Exception as e:
            st.error(f"Preview failed: {e}")
            import traceback
            st.code(traceback.format_exc()[:2000])

# ---------- HR preview display + save-to-GitHub ----------
if st.session_state.get("hr_preview_picks"):
    preview = st.session_state["hr_preview_picks"]
    meta = st.session_state.get("hr_preview_meta", {})
    at_str = st.session_state.get("hr_preview_at", "?")[:19]

    st.markdown(f"#### 💣 HR preview — top {len(preview)}")
    st.caption(f"Generated {at_str}  •  {meta.get('n_games','?')} games, "
               f"{meta.get('n_total','?')} total predictions  •  **NOT yet saved**")

    cols = ["confidence_tier", "confidence", "batter", "team", "matchup",
            "vs_sp", "park", "model_p_pct", "implied_pct", "edge_pp",
            "best_odds", "best_book"]
    df_prev = pd.DataFrame(preview).reindex(columns=cols).rename(columns={
        "confidence_tier": "Conf", "confidence": "Score",
        "vs_sp": "vs SP", "model_p_pct": "Model %", "implied_pct": "Mkt %",
        "edge_pp": "Edge", "best_odds": "Odds", "best_book": "Book",
    })
    for c in ("Model %", "Mkt %", "Edge", "Odds", "Score"):
        if c in df_prev.columns:
            df_prev[c] = pd.to_numeric(df_prev[c], errors="coerce")
    st.dataframe(
        df_prev, use_container_width=True, hide_index=True,
        column_config={
            "Conf":    st.column_config.TextColumn(width="small"),
            "Score":   st.column_config.NumberColumn(format="%d", width="small"),
            "Model %": st.column_config.NumberColumn(format="%.2f%%"),
            "Mkt %":   st.column_config.NumberColumn(format="%.2f%%"),
            "Edge":    st.column_config.NumberColumn(format="%+.2f"),
            "Odds":    st.column_config.NumberColumn(format="%+d"),
        },
    )

    save_cols = st.columns([1, 1, 3])
    with save_cols[0]:
        confirm_hr_save = st.button("💾 Save HR picks to GitHub",
                                    type="primary", use_container_width=True,
                                    key="confirm_hr_save_btn")
    with save_cols[1]:
        if st.button("✖ Discard", use_container_width=True, key="discard_hr_btn"):
            for k in ("hr_preview_picks", "hr_preview_meta", "hr_preview_at"):
                st.session_state.pop(k, None)
            st.rerun()

    if confirm_hr_save:
        try:
            payload = {
                "date":     today,
                "saved_at": datetime.now(tz=EASTERN).isoformat(),
                "top_n":    TOP_N,
                "n_games":  meta.get("n_games"),
                "n_total":  meta.get("n_total"),
                "picks":    preview,
            }
            path = f"{TRACKER_DIR}/{today}.json"
            gh.save_json(GH_TOKEN, OWNER, REPO, path, payload,
                         commit_msg=f"HR tracker: top {TOP_N} for {today}")
            cached_list_tracker_files.clear()
            cached_load_tracker.clear()
            st.success(f"✅ Saved {len(preview)} HR picks to GitHub at `{path}`.")
            # Clear preview state — user can re-preview if needed
            for k in ("hr_preview_picks", "hr_preview_meta", "hr_preview_at"):
                st.session_state.pop(k, None)
        except Exception as e:
            st.error(f"GitHub save failed: {e}")


# ---------- H+R+R preview handler ----------
if save_hrr_btn:
    with st.spinner(f"Pulling H+R+R Over {HRR_POINT} odds (preview only, NOT saved)..."):
        try:
            all_games = mlb_api.get_schedule(today)
            upcoming = []
            for g in all_games:
                fp_dt, _ = mlb_api.parse_first_pitch(g.get("gameDate", ""))
                if fp_dt and fp_dt.astimezone(timezone.utc) > now_utc:
                    upcoming.append((g, fp_dt))
            if not upcoming:
                st.warning("No upcoming games — nothing to preview. Try earlier in the day.")
                st.stop()

            # Map games to Odds API event IDs
            events = json.loads(urllib.request.urlopen(
                f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={ODDS_KEY}",
                timeout=15, context=_UNVERIFIED_SSL).read())
            event_map = {(e["away_team"] + "|" + e["home_team"]).lower(): e["id"] for e in events}

            all_hrr = []
            progress = st.progress(0)
            for gi, (g, fp_dt) in enumerate(upcoming):
                progress.progress((gi + 1) / len(upcoming))
                away_name = g["teams"]["away"]["team"]["name"]
                home_name = g["teams"]["home"]["team"]["name"]
                eid = event_map.get((away_name + "|" + home_name).lower())
                if not eid:
                    continue

                # Pull all H+R+R Over 1.5 offers across sharp books
                hrr_offers = odds_api.get_hrr_odds(ODDS_KEY, eid, point=HRR_POINT)
                if not hrr_offers:
                    continue

                # Build {normalized_player: {"all":[(am,book,raw)], "best":(am,book,raw)}}
                # Collect ALL prices so we can compute consensus implied (median
                # across books). Backtest showed the 50-60% best-implied bucket
                # was a -20% ROI loser — long-priced books were discounting for
                # a reason. Consensus floor catches that.
                best_by_player = {}
                for o in hrr_offers:
                    n = odds_api.normalize_name(o["player"])
                    am = o["american_odds"]; bk = o["bookmaker"]; raw = o["player"]
                    entry = best_by_player.setdefault(n, {"all": [], "best": None})
                    entry["all"].append((am, bk, raw))
                    if entry["best"] is None or am > entry["best"][0]:
                        entry["best"] = (am, bk, raw)

                # Map to MLB batter IDs via team rosters
                home_id = g["teams"]["home"]["team"]["id"]
                away_id = g["teams"]["away"]["team"]["id"]
                il = mlb_api.get_team_il(home_id, season) | mlb_api.get_team_il(away_id, season)
                roster_map = {}   # {normalized_name: (pid, team_name)}
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

                for norm, entry in best_by_player.items():
                    am, book, raw_name = entry["best"]
                    all_prices = entry["all"]
                    pid_info = roster_map.get(norm)
                    if not pid_info:
                        # Player on roster mapping failed — try fuzzy via partial last-name match
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

                    _i = lambda a: (100/(a+100) if a > 0 else abs(a)/(abs(a)+100))
                    best_imp = _i(am)
                    imps = sorted([_i(a) for a, _, _ in all_prices])
                    n_b = len(imps)
                    consensus_imp = (imps[n_b//2] if n_b % 2 == 1
                                     else (imps[n_b//2-1] + imps[n_b//2]) / 2)
                    all_hrr.append({
                        "batter":      mlb_name,
                        "batter_id":   int(pid),
                        "team":        team,
                        "matchup":     f"{away_name} @ {home_name}",
                        "game_pk":     g.get("gamePk"),
                        "vs_sp":       opp_sp,
                        "park":        stadium["park"],
                        "point":       HRR_POINT,
                        "best_odds":              am,
                        "best_book":              book,
                        "best_implied_pct":       round(best_imp * 100, 2),
                        "consensus_implied_pct":  round(consensus_imp * 100, 2),
                        "n_books":                n_b,
                        "implied_pct": round(consensus_imp * 100, 2),  # alias
                        "model_p_pct": None,
                        "edge_pp":     None,
                    })

            progress.empty()
            # ===========================================================
            # TIGHTENED FILTERING (5/24) — H+R+R was losing money on heavy
            # juice. 8-day data: 53.3% hit rate at avg -150 = -11% ROI.
            # New filter:
            #   - skip picks worse than -180 juice (math doesn't work)
            #   - prefer +130 to -170 range (sweet spot for H+R+R)
            #   - cap to 6 picks max (down from 10)
            # ===========================================================
            STRICT_HRR = True
            if STRICT_HRR:
                # 7/5: tightened consensus_implied floor from 55 -> 58 after
                # the 55-60% best_implied bucket bled -31% ROI on 10 picks
                # against elite pitchers (Robbie Ray x3, Sonny Gray). Losers
                # clustered at consensus 58.3-58.5% right at the old floor.
                qualifying = [
                    p for p in all_hrr
                    if p.get("best_odds") is not None
                    and p["best_odds"] >= -180
                    and p.get("best_implied_pct") is not None
                    and p["best_implied_pct"] >= 50
                    and p.get("consensus_implied_pct") is not None
                    and p["consensus_implied_pct"] >= 58
                ]
                def score(p):
                    am = p["best_odds"]
                    imp = p["consensus_implied_pct"]  # rank by consensus, not best
                    juice_bonus = 5 if -180 <= am <= -120 else (10 if -120 < am <= 100 else 0)
                    return imp + juice_bonus
                qualifying.sort(key=lambda x: -score(x))
                top_n_hrr = qualifying[:6]
            else:
                all_hrr.sort(key=lambda x: -x["implied_pct"])
                top_n_hrr = all_hrr[:TOP_N]
            # Stash in session_state for review-then-save flow
            st.session_state["hrr_preview_picks"] = top_n_hrr
            st.session_state["hrr_preview_meta"] = {
                "n_games": len(upcoming), "n_total": len(all_hrr),
                "point":   HRR_POINT,
                "filter":  "STRICT" if STRICT_HRR else "TOP_N",
            }
            st.session_state["hrr_preview_at"] = datetime.now(tz=EASTERN).isoformat()
            if STRICT_HRR:
                st.success(
                    f"Preview ready: {len(top_n_hrr)} STRICT H+R+R picks "
                    f"(juice capped at -180, implied >= 50%). "
                    f"Filtered from {len(all_hrr)} total."
                )
            else:
                st.success(f"Preview ready: {len(top_n_hrr)} H+R+R picks loaded — review below.")
        except Exception as e:
            st.error(f"H+R+R preview failed: {e}")

# ---------- H+R+R preview display + save-to-GitHub ----------
if st.session_state.get("hrr_preview_picks"):
    preview = st.session_state["hrr_preview_picks"]
    meta = st.session_state.get("hrr_preview_meta", {})
    at_str = st.session_state.get("hrr_preview_at", "?")[:19]
    pt = meta.get("point", HRR_POINT)

    st.markdown(f"#### 🏃 H+R+R preview — top {len(preview)} (Over {pt})")
    st.caption(f"Generated {at_str}  •  {meta.get('n_games','?')} games, "
               f"{meta.get('n_total','?')} total H+R+R offers  •  **NOT yet saved**")

    cols = ["batter", "team", "matchup", "vs_sp", "park",
            "implied_pct", "best_odds", "best_book"]
    df_prev = pd.DataFrame(preview).reindex(columns=cols).rename(columns={
        "vs_sp": "vs SP", "implied_pct": "Mkt %",
        "best_odds": "Odds", "best_book": "Book",
    })
    for c in ("Mkt %", "Odds"):
        if c in df_prev.columns:
            df_prev[c] = pd.to_numeric(df_prev[c], errors="coerce")
    st.dataframe(
        df_prev, use_container_width=True, hide_index=True,
        column_config={
            "Mkt %": st.column_config.NumberColumn(format="%.2f%%"),
            "Odds":  st.column_config.NumberColumn(format="%+d"),
        },
    )

    hrr_save_cols = st.columns([1, 1, 3])
    with hrr_save_cols[0]:
        confirm_hrr_save = st.button("💾 Save H+R+R to GitHub",
                                     type="primary", use_container_width=True,
                                     key="confirm_hrr_save_btn")
    with hrr_save_cols[1]:
        if st.button("✖ Discard", use_container_width=True, key="discard_hrr_btn"):
            for k in ("hrr_preview_picks", "hrr_preview_meta", "hrr_preview_at"):
                st.session_state.pop(k, None)
            st.rerun()

    if confirm_hrr_save:
        try:
            payload = {
                "date":     today,
                "saved_at": datetime.now(tz=EASTERN).isoformat(),
                "top_n":    TOP_N,
                "point":    pt,
                "n_games":  meta.get("n_games"),
                "n_total":  meta.get("n_total"),
                "picks":    preview,
            }
            path = f"{HRR_TRACKER_DIR}/{today}.json"
            gh.save_json(GH_TOKEN, OWNER, REPO, path, payload,
                         commit_msg=f"H+R+R tracker: top {TOP_N} (O{pt}) for {today}")
            st.success(f"✅ Saved {len(preview)} H+R+R picks to GitHub at `{path}`.")
            for k in ("hrr_preview_picks", "hrr_preview_meta", "hrr_preview_at"):
                st.session_state.pop(k, None)
        except Exception as e:
            st.error(f"GitHub save failed: {e}")


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
                # Backfill confidence on older saved picks that don't have it
                conf_tier = p.get("confidence_tier")
                conf_score = p.get("confidence")
                if conf_tier is None and hasattr(hr_model, "pick_confidence"):
                    c = hr_model.pick_confidence(p.get("model_p_pct"), p.get("edge_pp"))
                    conf_tier = c["tier"]
                    conf_score = c["score"]
                display_rows.append({
                    "#":        i,
                    "Conf":     conf_tier,
                    "Score":    conf_score,
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
            for col in ("Model %", "Mkt %", "Edge pp", "Odds", "Score"):
                if col in df_view.columns:
                    df_view[col] = pd.to_numeric(df_view[col], errors="coerce")
            st.dataframe(
                df_view,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Conf":     st.column_config.TextColumn(width="small"),
                    "Score":    st.column_config.NumberColumn(format="%d", width="small"),
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


# ============================================================================
# H+R+R Tracker — separate section below HR section
# ============================================================================
st.markdown("---")
st.markdown(f"### 🏃 H+R+R Tracker (Over {HRR_POINT})")
st.caption(
    f"Tracks the top {TOP_N} Over {HRR_POINT} Hits+Runs+RBIs picks each day. "
    "Currently uses **DraftKings consensus** (most-likely-to-clear ranking) since "
    "we don't have a dedicated H+R+R model yet. FD/MGM/Caesars typically post "
    "closer to first pitch and will be picked up automatically when available."
)

@st.cache_data(ttl=300, show_spinner=False)
def cached_list_hrr_files():
    files = gh.list_dir(GH_TOKEN, OWNER, REPO, HRR_TRACKER_DIR)
    return [f for f in files if f["name"].endswith(".json") and not f["name"].startswith("_")]

@st.cache_data(ttl=600, show_spinner=False)
def cached_load_hrr(path):
    return gh.load_json(GH_TOKEN, OWNER, REPO, path)

with st.spinner("Loading H+R+R history..."):
    hrr_files = cached_list_hrr_files()

if not hrr_files:
    st.info(f"No H+R+R tracker files yet. Click **🏃 Save H+R+R top 10 (O{HRR_POINT})** above to start tracking.")
else:
    hrr_files.sort(key=lambda f: f["name"], reverse=True)
    st.caption(f"📦 {len(hrr_files)} H+R+R day(s) saved in GitHub")

    # Aggregate H+R+R stats across settled days
    hrr_daily = []
    for tf in hrr_files:
        date_str = tf["name"][:-5]
        if date_str >= today: continue
        slate = cached_load_hrr(tf["path"])
        if not slate: continue
        picks = slate.get("picks", [])
        if not picks: continue
        game_pks = list({p["game_pk"] for p in picks if p.get("game_pk")})
        hrr_map = {gpk: (cached_fetch_hrr_map(gpk) or {}) for gpk in game_pks}
        pt = slate.get("point", HRR_POINT)

        def hit(p):
            tot = hrr_map.get(p["game_pk"], {}).get(p["batter_id"])
            return tot is not None and tot > pt

        t5 = sum(1 for p in picks[:5] if hit(p))
        t10 = sum(1 for p in picks[:10] if hit(p))
        # P&L on top 5 at flat $10
        pnl5, bet5 = 0.0, 0
        for p in picks[:5]:
            if p.get("best_odds") is None: continue
            bet5 += 1
            am = p["best_odds"]
            dec = 1 + am/100 if am > 0 else 1 + 100/abs(am)
            pnl5 += (10 * dec - 10) if hit(p) else -10
        hrr_daily.append({
            "Date":    date_str,
            "Line":    f"O{pt}",
            "Top 5":   f"{t5}/5",
            "Top 10":  f"{t10}/10",
            "$ Bet":   bet5 * 10,
            "PnL@$10": round(pnl5, 2),
            "AvgImp%": round(sum(p["implied_pct"] for p in picks[:5] if p.get("implied_pct") is not None)
                              / max(1, sum(1 for p in picks[:5] if p.get("implied_pct") is not None)), 1),
        })

    if hrr_daily:
        n = len(hrr_daily)
        t5 = sum(int(r["Top 5"].split("/")[0]) for r in hrr_daily)
        t10 = sum(int(r["Top 10"].split("/")[0]) for r in hrr_daily)
        pnl = sum(r["PnL@$10"] for r in hrr_daily)
        bet = sum(r["$ Bet"] for r in hrr_daily)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Days tracked", n)
        m2.metric("Top 5 hit rate", f"{t5}/{n*5}",
                  delta=f"{t5/(n*5)*100:.1f}%")
        m3.metric("Top 10 hit rate", f"{t10}/{n*10}",
                  delta=f"{t10/(n*10)*100:.1f}%")
        if bet:
            roi = pnl / bet * 100
            m4.metric("Top 5 P&L (flat $10)", f"${pnl:+.2f}",
                      delta=f"{roi:+.1f}% ROI")
        st.markdown("#### Daily breakdown — H+R+R")
        st.dataframe(pd.DataFrame(hrr_daily), use_container_width=True, hide_index=True)

    # Per-day viewer for H+R+R
    st.markdown("#### 🔎 View H+R+R picks for a specific day")
    hrr_date_options = [tf["name"][:-5] for tf in hrr_files]
    selected_hrr_date = st.selectbox(
        "Select a saved H+R+R date",
        options=hrr_date_options,
        index=0,
        key="hrr_date_picker",
    )

    if selected_hrr_date:
        sel = next((tf for tf in hrr_files if tf["name"] == f"{selected_hrr_date}.json"), None)
        if sel:
            slate = cached_load_hrr(sel["path"])
            if slate and slate.get("picks"):
                picks = slate["picks"]
                pt = slate.get("point", HRR_POINT)
                is_today = (selected_hrr_date >= today)

                hrr_map = {}
                if not is_today:
                    game_pks = list({p["game_pk"] for p in picks if p.get("game_pk")})
                    hrr_map = {gpk: (cached_fetch_hrr_map(gpk) or {}) for gpk in game_pks}

                display_rows = []
                for i, p in enumerate(picks, 1):
                    actual = None
                    won = None
                    if not is_today and p.get("game_pk") in hrr_map:
                        actual = hrr_map[p["game_pk"]].get(p["batter_id"])
                        if actual is not None:
                            won = actual > pt
                    if is_today or actual is None:
                        result = "⏳ pending"
                    elif won:
                        result = f"🟢 {actual} H+R+R"
                    else:
                        result = f"⚪ {actual} H+R+R"
                    display_rows.append({
                        "#":        i,
                        "Result":   result,
                        "Batter":   p.get("batter", "?"),
                        "Team":     p.get("team", "?"),
                        "Matchup":  p.get("matchup", "?"),
                        "vs SP":    p.get("vs_sp", "?"),
                        "Park":     p.get("park", "?"),
                        "Line":     f"O{pt}",
                        "Mkt %":    p.get("implied_pct"),
                        "Odds":     p.get("best_odds"),
                        "Book":     p.get("best_book"),
                    })

                if not is_today and hrr_map:
                    def hit(p):
                        tot = hrr_map.get(p["game_pk"], {}).get(p["batter_id"])
                        return tot is not None and tot > pt
                    t5 = sum(1 for p in picks[:5] if hit(p))
                    t10 = sum(1 for p in picks[:10] if hit(p))
                    pnl, bet = 0.0, 0
                    for p in picks[:5]:
                        if p.get("best_odds") is None: continue
                        bet += 1
                        am = p["best_odds"]
                        dec = 1 + am/100 if am > 0 else 1 + 100/abs(am)
                        pnl += (10 * dec - 10) if hit(p) else -10
                    summary = f"**Top 5: {t5}/5** • **Top 10: {t10}/10**"
                    if bet:
                        summary += f" • **P&L @ $10 flat: ${pnl:+.2f}** ({bet} bet{'s' if bet != 1 else ''})"
                    st.markdown(summary)
                else:
                    n_odds = sum(1 for p in picks if p.get("best_odds") is not None)
                    st.markdown(f"⏳ Games haven't settled yet • {n_odds}/{len(picks)} picks have odds attached")

                df_view = pd.DataFrame(display_rows)
                for col in ("Mkt %", "Odds"):
                    if col in df_view.columns:
                        df_view[col] = pd.to_numeric(df_view[col], errors="coerce")
                st.dataframe(
                    df_view,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Mkt %":  st.column_config.NumberColumn(format="%.2f%%"),
                        "Odds":   st.column_config.NumberColumn(format="%+d"),
                    },
                )
                st.caption(
                    f"Saved at {slate.get('saved_at', '?')[:19]}  •  "
                    f"{slate.get('n_games', '?')} games, {slate.get('n_total', '?')} total H+R+R offers"
                )
            else:
                st.info(f"No picks found in {selected_hrr_date}.json.")
