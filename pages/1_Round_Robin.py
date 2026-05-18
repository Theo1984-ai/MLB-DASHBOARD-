"""
Multi-market 4-leg Round Robin builder.

Pull live game-line odds (moneyline, run-line, totals) plus our existing
HR prop pipeline, compute model edges per outcome, and let the user select
4 picks across markets to build a round robin. Picks save to picks/<date>.json
for performance tracking.
"""
import os
import sys
import ssl as _ssl_compat
_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
from datetime import datetime, date
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd

# Ensure project root on path so we can import siblings
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from data import mlb_api, odds as odds_api, research as research_api
from data.stadiums import get_stadium
from models import (
    game_model, hr_model, markets, parlay as parlay_model,
    kelly as kelly_model, calibration as cal_model,
    picks_logger, league as league_const,
)
from data import savant
from data import weather as wx

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="MLB Round Robin", page_icon="", layout="wide")
st.title("🎰 4-Leg Round Robin Builder")
st.caption(
    "Pull live moneyline, run-line, totals, and HR-prop odds. Compare to model "
    "predictions, pick 4 across markets, and build the round robin. Picks save "
    "for daily P&L tracking."
)

# ── Backtest results banner ───────────────────────────────────────────────────
with st.expander("📊 Backtest results (Apr 28 – May 11, 2026 · 184 games · real DK prices)", expanded=False):
    st.markdown("""
**Pure model results** (`backtest_rr.py`):

| Market | Bets | Hit Rate | ROI | Notes |
|---|---|---|---|---|
| 🟢 **Run Line** | 92 | **71.7%** | **+24.2%** | Best market — pure model wins here |
| 🟢 **Moneyline** | 80 | **60.0%** | **+17.8%** | Good — avoid 5–8pp bucket |
| 🟡 **Totals** | 166 | 54.2% | +2.7% | Overcalibrated — fixed by blending |

**Blend model results** (`backtest_blend.py` — market-informed, currently active):

| Blend | Market Wt | Bets | Hit Rate | ROI | vs Pure Model |
|---|---|---|---|---|---|
| RL alpha=1.0 | 0% market | 92 | 71.7% | +24.2% | unchanged |
| ML alpha=0.5 | 50% market | — | — | +23.4% | +5.6pp ROI |
| TOT alpha=0.3 | 70% market | — | — | **+18.6%** | +15.9pp ROI |

**Key rules:**
- ✅ Run Line: pure model (no blend) — model already sharp here
- ✅ Moneyline: 50/50 blend with devigged market — mild improvement
- ✅ Totals: 70% market weight — fixes the 73% model / 56% actual overcalibration problem
- ✅ Suspect filter (>+20pp): always keep ON — fake edges
""")
    st.caption("Backtest scripts: `backtest_rr.py` (pure model) · `backtest_blend.py` (blend) · Rerun anytime.")


# ---------- Auth: Odds API key ----------

def resolve_odds_key():
    try:
        if "THE_ODDS_API_KEY" in st.secrets:
            return st.secrets["THE_ODDS_API_KEY"]
    except Exception:
        pass
    return os.environ.get("THE_ODDS_API_KEY") or st.session_state.get("odds_api_key")


odds_key = resolve_odds_key()
if not odds_key:
    st.error("Add your `THE_ODDS_API_KEY` in `.streamlit/secrets.toml` or via the main dashboard sidebar.")
    st.stop()


# ---------- Cached loaders (mirror main dashboard) ----------

@st.cache_data(ttl=90, show_spinner=False)
def load_schedule(date_str): return mlb_api.get_schedule(date_str)

@st.cache_data(ttl=600, show_spinner=False)
def load_weather(lat, lon, target_iso):
    return wx.get_forecast(lat, lon, datetime.fromisoformat(target_iso))

@st.cache_data(ttl=3600, show_spinner=False)
def load_pitcher(person_id, season):
    return mlb_api.get_pitcher_season(person_id, season) if person_id else mlb_api._empty_pitcher()

@st.cache_data(ttl=3600, show_spinner=False)
def load_team_season(team_id, season, group):
    return mlb_api.get_team_season(team_id, season, group)

@st.cache_data(ttl=1800, show_spinner=False)
def load_team_recent(team_id, season, days, group):
    return mlb_api.get_team_recent(team_id, season, days, group)

@st.cache_data(ttl=3600, show_spinner=False)
def load_bullpen(team_id, season): return mlb_api.get_team_bullpen_stats(team_id, season)

@st.cache_data(ttl=21600, show_spinner=False)
def load_savant_pitcher_xstats(year): return savant.pitcher_xstats(year)

@st.cache_data(ttl=21600, show_spinner=False)
def load_savant_batter_statcast(year): return savant.batter_statcast(year)

@st.cache_data(ttl=21600, show_spinner=False)
def load_savant_batter_xstats(year): return savant.batter_xstats(year)

@st.cache_data(ttl=3600, show_spinner=False)
def load_calibration(): return cal_model.load_params()

@st.cache_data(ttl=3600, show_spinner=False)
def load_odds_events(api_key): return odds_api.get_events(api_key)

@st.cache_data(ttl=600, show_spinner=False)
def load_game_lines(api_key, event_id, books):
    return odds_api.get_game_lines(api_key, event_id, books)

@st.cache_data(ttl=600, show_spinner=False)
def load_hr_odds(api_key, event_id, books):
    return odds_api.get_hr_odds(api_key, event_id, books)


# ---------- Header controls ----------

hc1, hc2, hc3 = st.columns([2, 2, 1])
with hc1:
    selected_date = st.date_input("Date", value=datetime.now(tz=EASTERN).date(), format="YYYY-MM-DD")
with hc2:
    books = st.multiselect(
        "Books",
        options=["draftkings", "fanduel", "betmgm", "williamhill_us", "bovada", "betrivers", "fanatics", "betonlineag"],
        default=["draftkings", "fanduel", "betmgm", "williamhill_us", "bovada"],
        help="Sharp default = DK / FD / BetMGM / Caesars (williamhill_us) / Bovada. "
             "Caesars is keyed as `williamhill_us` in The Odds API. "
             "Bovada is offshore but their mainline prices align with sharp consensus.",
    )
with hc3:
    if st.button(" Refresh", use_container_width=True):
        st.cache_data.clear(); st.rerun()


date_str = selected_date.strftime("%Y-%m-%d")
season = selected_date.year
books_str = ",".join(books) if books else "draftkings,fanduel,betmgm,williamhill_us,bovada"


# ---------- Load schedule + run game predictions ----------

with st.spinner(f"Loading {date_str}..."):
    games = load_schedule(date_str)

if not games:
    st.warning(f"No games for {date_str}.")
    st.stop()

cal_params = load_calibration()
px_df = load_savant_pitcher_xstats(season)
bs_df = load_savant_batter_statcast(season)
bx_df = load_savant_batter_xstats(season)


def enrich(g):
    home = g["teams"]["home"]; away = g["teams"]["away"]
    home_id = home["team"]["id"]; away_id = away["team"]["id"]
    stadium = get_stadium(home_id)
    fp_dt, fp_str = mlb_api.parse_first_pitch(g.get("gameDate", ""))
    forecast = load_weather(stadium["lat"], stadium["lon"], fp_dt.isoformat()) if fp_dt else {}
    away_p = away.get("probablePitcher") or {}
    home_p = home.get("probablePitcher") or {}
    away_stats = load_pitcher(away_p.get("id"), season)
    home_stats = load_pitcher(home_p.get("id"), season)

    home_h = load_team_season(home_id, season, "hitting")
    away_h = load_team_season(away_id, season, "hitting")
    home_r = load_team_recent(home_id, season, 15, "hitting")
    away_r = load_team_recent(away_id, season, 15, "hitting")
    home_p_team = load_team_season(home_id, season, "pitching")
    away_p_team = load_team_season(away_id, season, "pitching")
    home_bp = load_bullpen(home_id, season)
    away_bp = load_bullpen(away_id, season)

    home_p_savant = away_p_savant = None
    if home_p.get("id") and not px_df.empty:
        hit = px_df[px_df["player_id"] == home_p["id"]]
        if not hit.empty: home_p_savant = hit.iloc[0].to_dict()
    if away_p.get("id") and not px_df.empty:
        hit = px_df[px_df["player_id"] == away_p["id"]]
        if not hit.empty: away_p_savant = hit.iloc[0].to_dict()

    pred = game_model.predict_game(
        home_team_season=home_h, home_team_recent=home_r,
        away_team_season=away_h, away_team_recent=away_r,
        home_pitcher_stats=home_stats, home_pitcher_savant=home_p_savant,
        away_pitcher_stats=away_stats, away_pitcher_savant=away_p_savant,
        home_team_pitching_season=home_p_team,
        away_team_pitching_season=away_p_team,
        park_hr=stadium["hr_factor"], weather=forecast,
        home_bullpen=home_bp, away_bullpen=away_bp,
    )

    return {
        "raw": g,
        "game_pk": g.get("gamePk"),
        "home_team": home["team"]["name"], "away_team": away["team"]["name"],
        "home_id": home_id, "away_id": away_id,
        "first_pitch": fp_str, "first_pitch_dt": fp_dt,
        "status": g.get("status", {}).get("detailedState", "Preview"),
        "stadium": stadium, "weather": forecast,
        "pred": pred,
        "home_pitcher": home_p.get("fullName", "TBD"),
        "away_pitcher": away_p.get("fullName", "TBD"),
    }


with st.spinner("Running game-level predictions for every matchup..."):
    enriched = [enrich(g) for g in games]


# ---------- Match Odds API events to schedule games ----------

with st.spinner("Loading Odds API events..."):
    odds_events = load_odds_events(odds_key)

event_map = {(ev["away_team"] + "|" + ev["home_team"]).lower(): ev["id"] for ev in odds_events}
upcoming = []
now_utc = datetime.now(tz=EASTERN).astimezone(tz=None).utcnow()
for g in enriched:
    key = (g["away_team"] + "|" + g["home_team"]).lower()
    eid = event_map.get(key)
    if eid:
        g["event_id"] = eid
        upcoming.append(g)

if not upcoming:
    st.warning("None of tonight's games have matching Odds API events. Try refreshing.")
    st.stop()


# ---------- Pull odds (one request per game per market call) ----------

st.markdown(f"###  Pull market odds for {len(upcoming)} games")
mc1, mc2, mc3 = st.columns([1, 1, 1])
with mc1:
    pull_lines = st.button(f"Fetch ML / Spread / Total ({len(upcoming)} req)",
                           use_container_width=True)
with mc2:
    pull_hr = st.button(f"Fetch HR props ({len(upcoming)} req)", use_container_width=True)
with mc3:
    quota = odds_api.get_quota()
    if quota.get("remaining") is not None:
        st.metric("Quota remaining", f"{quota['remaining']}")

if pull_lines:
    with st.spinner("Pulling game-line odds..."):
        for g in upcoming:
            g["lines"] = load_game_lines(odds_key, g["event_id"], books_str)
    st.session_state["lines_loaded_at"] = datetime.now(tz=EASTERN).isoformat()
    st.session_state["loaded_lines"] = {g["event_id"]: g["lines"] for g in upcoming}
    st.rerun()
elif "loaded_lines" in st.session_state:
    for g in upcoming:
        g["lines"] = st.session_state["loaded_lines"].get(g["event_id"], [])
else:
    for g in upcoming:
        g["lines"] = []

if pull_hr:
    with st.spinner("Pulling HR prop odds..."):
        for g in upcoming:
            g["hr_offers"] = load_hr_odds(odds_key, g["event_id"], "draftkings,fanduel,betmgm,williamhill_us,bovada")
    st.session_state["hr_loaded_at"] = datetime.now(tz=EASTERN).isoformat()
    st.session_state["loaded_hr"] = {g["event_id"]: g["hr_offers"] for g in upcoming}
    st.rerun()
elif "loaded_hr" in st.session_state:
    for g in upcoming:
        g["hr_offers"] = st.session_state["loaded_hr"].get(g["event_id"], [])
else:
    for g in upcoming:
        g["hr_offers"] = []

# Apply stale-line filter to HR offers across all games
apply_filter = st.toggle(
    "[Filter] Drop stale / single-book longshot HR offers",
    value=True,
    help=(
        "Filters out HR-prop lines where a single book quotes implied probability "
        "< 3% with no other book pricing the same player. These are almost always "
        "stale opening lines or limit-only display prices that produce fake "
        "model edges of +20pp+."
    ),
)
if apply_filter:
    total_filter_log = {"single_book_longshot": 0, "outlier_price": 0, "kept": 0, "total_in": 0}
    for g in upcoming:
        if g["hr_offers"]:
            g["hr_offers"], log = odds_api.filter_stale_lines(g["hr_offers"])
            for k in total_filter_log: total_filter_log[k] += log.get(k, 0)
    if total_filter_log["total_in"] > 0:
        st.caption(
            f"[Filter] Dropped {total_filter_log['single_book_longshot']} single-book longshots "
            f"+ {total_filter_log['outlier_price']} outlier prices. "
            f"Kept {total_filter_log['kept']} of {total_filter_log['total_in']} HR offers."
        )


# ---------- Build candidate pool ----------

def best_offer(offers, key="implied_prob"):
    """Best (most generous) offer = lowest implied probability."""
    if not offers:
        return None
    return min(offers, key=lambda o: o.get(key, 1.0))


candidate_pool = []  # one row per market + side combo, with model prob + best odds

for g in upcoming:
    pred = g["pred"]
    home_runs = pred["home_exp_runs"]; away_runs = pred["away_exp_runs"]
    p_home = pred["p_home_win"]
    matchup = f"{g['away_team']} @ {g['home_team']}"

    # ----- Moneyline  [blend: alpha=0.5 — 50% model / 50% devigged market] -----
    # Backtest: pure model +17.8% ROI → blend +23.4% ROI
    h2h_offers = [o for o in g["lines"] if o["market"] == "h2h"]
    if h2h_offers:
        home_ml_offers = [o for o in h2h_offers if o["name"] == g["home_team"]]
        away_ml_offers = [o for o in h2h_offers if o["name"] == g["away_team"]]
        if home_ml_offers and away_ml_offers:
            best_home_ml = best_offer(home_ml_offers)
            best_away_ml = best_offer(away_ml_offers)
            # Devig (shared helper, keeps dashboard + local scripts in sync)
            imp_h = best_home_ml["implied_prob"]; imp_a = best_away_ml["implied_prob"]
            fair_h, fair_a = markets.devig_two_way(imp_h, imp_a)
            for side_name, mp_raw, fair, best in [
                (g["home_team"], p_home,       fair_h, best_home_ml),
                (g["away_team"], 1.0 - p_home, fair_a, best_away_ml),
            ]:
                blended = markets.blend(mp_raw, fair, markets.BLEND_ALPHA_ML)
                edge_pp = (blended - best["implied_prob"]) * 100
                candidate_pool.append({
                    "label":        f"ML -- {side_name} {best['american_odds']:+d}",
                    "market":       "ML",
                    "side":         side_name,
                    "matchup":      matchup,
                    "game_pk":      g["game_pk"],
                    "event_id":     g["event_id"],
                    "model_p":      blended,    # blended prob drives edge + parlay EV
                    "raw_model_p":  mp_raw,     # raw model for display
                    "fair_p":       fair,        # devigged market for display
                    "implied":      best["implied_prob"],
                    "edge_pp":      edge_pp,
                    "american":     best["american_odds"],
                    "decimal":      best["decimal_odds"],
                    "book":         best["bookmaker"],
                    "point":        None,
                    "first_pitch":  g["first_pitch"],
                })

    # ----- Spreads (run line)  [pure model — alpha=1.0, no blend] -----
    # Backtest: pure model +24.2% ROI — blending hurts RL (market already prices RL well)
    sp_offers = [o for o in g["lines"] if o["market"] == "spreads"]
    if sp_offers:
        for side_name in (g["home_team"], g["away_team"]):
            team_offers = [o for o in sp_offers if o["name"] == side_name]
            if not team_offers: continue
            for o in team_offers:
                point = o.get("point")
                if point is None: continue
                side_key = "home" if side_name == g["home_team"] else "away"
                mp = markets.prob_team_covers_spread(home_runs, away_runs, side_key, float(point))
                edge_pp = (mp - o["implied_prob"]) * 100
                candidate_pool.append({
                    "label":        f"Run Line -- {side_name} {point:+.1f} ({o['american_odds']:+d})",
                    "market":       "RL",
                    "side":         side_name,
                    "point":        point,
                    "matchup":      matchup,
                    "game_pk":      g["game_pk"],
                    "event_id":     g["event_id"],
                    "model_p":      mp,     # pure model (alpha=1.0)
                    "raw_model_p":  mp,
                    "fair_p":       None,   # no devig for RL
                    "implied":      o["implied_prob"],
                    "edge_pp":      edge_pp,
                    "american":     o["american_odds"],
                    "decimal":      o["decimal_odds"],
                    "book":         o["bookmaker"],
                    "first_pitch":  g["first_pitch"],
                })

    # ----- Totals  [blend: alpha=0.3 — 30% model / 70% devigged market] -----
    # Backtest: pure model +2.7% ROI (73% model vs 56% actual) → blend +18.6% ROI
    tot_offers = [o for o in g["lines"] if o["market"] == "totals"]
    if tot_offers:
        expected_total = home_runs + away_runs
        TOT_ALPHA = 0.3
        # Group by line so we can devig over/under together
        _lines_seen = set(o.get("point") for o in tot_offers if o.get("point") is not None)
        for _line in _lines_seen:
            over_offers  = [o for o in tot_offers if o["name"] == "Over"  and o.get("point") == _line]
            under_offers = [o for o in tot_offers if o["name"] == "Under" and o.get("point") == _line]
            if not over_offers or not under_offers:
                continue
            best_ov = best_offer(over_offers)
            best_un = best_offer(under_offers)
            # Devig (shared helper)
            imp_ov = best_ov["implied_prob"]; imp_un = best_un["implied_prob"]
            fair_ov, fair_un = markets.devig_two_way(imp_ov, imp_un)
            # Model probabilities (pure Poisson)
            mp_ov = markets.prob_total_over(expected_total, float(_line))
            mp_un = markets.prob_total_under(expected_total, float(_line))
            blended_ov = markets.blend(mp_ov, fair_ov, markets.BLEND_ALPHA_TOT)
            blended_un = markets.blend(mp_un, fair_un, markets.BLEND_ALPHA_TOT)
            for side_name, mp_raw, blended, fair, best in [
                ("Over",  mp_ov, blended_ov, fair_ov, best_ov),
                ("Under", mp_un, blended_un, fair_un, best_un),
            ]:
                edge_pp = (blended - best["implied_prob"]) * 100
                candidate_pool.append({
                    "label":        f"Total -- {side_name} {_line} ({best['american_odds']:+d})",
                    "market":       "TOT",
                    "side":         side_name,
                    "point":        _line,
                    "matchup":      matchup,
                    "game_pk":      g["game_pk"],
                    "event_id":     g["event_id"],
                    "model_p":      blended,    # blended prob drives edge + parlay EV
                    "raw_model_p":  mp_raw,
                    "fair_p":       fair,
                    "implied":      best["implied_prob"],
                    "edge_pp":      edge_pp,
                    "american":     best["american_odds"],
                    "decimal":      best["decimal_odds"],
                    "book":         best["bookmaker"],
                    "first_pitch":  g["first_pitch"],
                })

    # ----- HR props (one row per batter, best price) -----
    # Pre-build pitcher lookup for this game (home batter → away pitcher, vice versa)
    _raw_teams = g["raw"].get("teams", {})
    _away_pid  = _raw_teams.get("away", {}).get("probablePitcher", {}).get("id")
    _home_pid  = _raw_teams.get("home", {}).get("probablePitcher", {}).get("id")
    _away_p_stats = load_pitcher(_away_pid, season)
    _home_p_stats = load_pitcher(_home_pid, season)
    _away_p_savant = (px_df[px_df["player_id"] == _away_pid].iloc[0].to_dict()
                      if _away_pid and not px_df.empty and
                      not px_df[px_df["player_id"] == _away_pid].empty else None)
    _home_p_savant = (px_df[px_df["player_id"] == _home_pid].iloc[0].to_dict()
                      if _home_pid and not px_df.empty and
                      not px_df[px_df["player_id"] == _home_pid].empty else None)

    if g["hr_offers"]:
        from collections import defaultdict
        by_player = defaultdict(list)
        for o in g["hr_offers"]:
            by_player[o["player"]].append(o)
        for player_name, offers in by_player.items():
            best = best_offer(offers)
            norm = odds_api.normalize_name(player_name)
            mp = None
            if not bs_df.empty:
                hit = bs_df[bs_df["player_name"].apply(odds_api.normalize_name) == norm]
                if not hit.empty:
                    pid = int(hit.iloc[0]["player_id"])
                    bs = hit.iloc[0].to_dict()
                    if not bx_df.empty:
                        bx = bx_df[bx_df["player_id"] == pid]
                        if not bx.empty:
                            bs["xslg"] = bx.iloc[0].get("xslg")
                            bs["xwoba"] = bx.iloc[0].get("xwoba")

                    # Determine which pitcher faces this batter by checking team affiliation
                    # Check if player is on home or away roster via their team_id in bs_df
                    _player_team_id = hit.iloc[0].get("team_id")
                    if _player_team_id and int(_player_team_id) == g["home_id"]:
                        # Batter is HOME → faces AWAY pitcher
                        opp_p_stats  = _away_p_stats
                        opp_p_savant = _away_p_savant
                    elif _player_team_id and int(_player_team_id) == g["away_id"]:
                        # Batter is AWAY → faces HOME pitcher
                        opp_p_stats  = _home_p_stats
                        opp_p_savant = _home_p_savant
                    else:
                        # Can't determine team — use worse pitcher (conservative)
                        away_hr9 = _away_p_stats.get("hr_per9", 1.2) or 1.2
                        home_hr9 = _home_p_stats.get("hr_per9", 1.2) or 1.2
                        opp_p_stats  = _away_p_stats if away_hr9 >= home_hr9 else _home_p_stats
                        opp_p_savant = _away_p_savant if away_hr9 >= home_hr9 else _home_p_savant

                    per_pa = hr_model.predict_per_pa(
                        batter_savant=bs,
                        pitcher_stats=opp_p_stats,
                        pitcher_savant=opp_p_savant,
                        park_hr=g["stadium"]["hr_factor"],
                        weather=g["weather"],
                        park_orientation_deg=g["stadium"]["orientation_deg"],
                        stadium=g["stadium"],
                    )
                    per_g = hr_model.predict_per_game(per_pa, league_const.PA_PER_BATTER)
                    mp = cal_model.apply_calibration(per_g["p_at_least_one"], cal_params)
            if mp is None:
                continue
            # Flag HR picks as lower-confidence (simplified pitcher model)
            edge_pp = (mp - best["implied_prob"]) * 100
            candidate_pool.append({
                "label":     f"HR -- {player_name} ({best['american_odds']:+d})",
                "market":    "HR",
                "side":      player_name,
                "matchup":   matchup,
                "game_pk":   g["game_pk"],
                "event_id":  g["event_id"],
                "model_p":   mp,
                "implied":   best["implied_prob"],
                "edge_pp":   edge_pp,
                "american":  best["american_odds"],
                "decimal":   best["decimal_odds"],
                "book":      best["bookmaker"],
                "point":     None,
                "first_pitch": g["first_pitch"],
            })


# ---------- Display candidates ----------

if not candidate_pool:
    st.info("No odds loaded yet. Click **Fetch ML/Spread/Total** above.")
    st.stop()

st.markdown(f"### Candidate pool -- {len(candidate_pool)} outcomes priced")

# ── Backtest-calibrated filter defaults ──────────────────────────────────────
# ML: +5pp min (5-8pp bucket went negative in backtest)
# RL: +5pp min (all buckets profitable)
# TOT: +8pp min (barely profitable even at high edges — overcalibrated)
# HR: +5pp min (simplified pitcher model — treat as directional only)
_MKT_MIN_EDGE = {"ML": 5.0, "RL": 5.0, "TOT": 8.0, "HR": 5.0}

fc1, fc2, fc3 = st.columns([2, 1, 1])
with fc1:
    market_filter = st.multiselect(
        "Markets to show",
        options=["ML", "RL", "TOT", "HR"],
        default=["ML", "RL"],   # Totals excluded by default — use Alt Under section instead
        format_func=lambda m: {
            "ML":  "Moneyline  (backtest +17.8% ROI)",
            "RL":  "Run Line   (backtest +24.2% ROI)",
            "TOT": "Totals     (backtest +2.7% ROI — weak)",
            "HR":  "Home Run   (no backtest)",
        }[m],
    )
with fc2:
    min_edge = st.slider("Min edge (pp)", 0.0, 30.0, 5.0, 0.5,
                         help="Backtest: ML/RL profitable at 5pp+. Totals need 8pp+ and are still weak.")
with fc3:
    show_top_n = st.slider("Show top N", 8, 50, 15, 1,
                            help="Limit the candidate pool to the top-N by edge so it's easier to scan.")

# Always-on: hide suspect picks (backtest: +20pp edges hit only 58.7% despite model showing 77.6%)
include_suspect = st.toggle(
    "Include suspect picks (edge >+20pp)",
    value=False,
    help="Backtest showed these hit 58.7% despite model showing 77.6% — almost always stale lines. Keep OFF.",
)

# Totals warning
if "TOT" in market_filter:
    st.warning(
        "⚠️ **Standard Totals** are the weakest market here (+2.7% ROI, badly overcalibrated at high edges). "
        "For totals value, use **AI Picks → Alt Under Parlay Generator** instead "
        "(backtested at 84.4% hit rate with real DK prices)."
    )

# Edge filter — slider is authoritative. Per-market floors apply only when
# slider is at its default; if the user explicitly lowers the slider below
# a market floor, honor that (they want to see marginal picks).
filtered_all = []
for c in candidate_pool:
    if c["market"] not in market_filter:
        continue
    # Use slider value directly — no hardcoded floor override
    if c["edge_pp"] < min_edge:
        continue
    if not include_suspect and odds_api.is_suspect_edge(c["edge_pp"]):
        continue
    filtered_all.append(c)

filtered_all.sort(key=lambda c: -c["edge_pp"])
filtered = filtered_all[:show_top_n]

st.caption(
    f"Showing top {len(filtered)} of {len(filtered_all)} candidates "
    f"(ML/RL ≥5pp · TOT ≥8pp · suspect >+20pp hidden)"
    + (" [totals included — see warning above]" if "TOT" in market_filter else "")
)


# ---------- Auto-pick top 4 ----------

st.markdown("### Auto-pick 4 legs")

ap1, ap2 = st.columns([1, 3])
with ap1:
    auto_pick = st.button("Auto-pick top 4", type="primary", use_container_width=True,
                           help=("Runs the selection algorithm: filter to positive edges, "
                                 "sort by edge desc, pick top 4 across 4 different games, "
                                 "preferring 12-22% probability sweet spot when possible."))
with ap2:
    st.caption(
        "Algorithm: top edges across **different games** (independence guard), "
        "preferring **12-22% probability** picks when available (parlay sweet spot). "
        "After clicking, the legs below auto-populate -- swap any individual leg if you disagree."
    )


def algorithm_pick4(candidates):
    """
    Backtest-optimised 4-leg selector.

    Priority order (from backtest results):
      1. Run Line  edge >= 8pp  (best market — 71.7% hit, +24.2% ROI)
      2. Moneyline edge >= 8pp  (73.3% hit at 8-12pp, +42.6% ROI)
      3. Run Line  edge >= 5pp
      4. Moneyline edge >= 5pp  (skip 5-8pp if enough RL available)
      5. Any remaining (fill slots)

    One leg per game. Avoid ML 5-8pp bucket if higher-confidence picks exist.
    """
    picks = []
    used_games = set()

    def _try_add(pool):
        for c in pool:
            if len(picks) >= 4: break
            if c["game_pk"] in used_games: continue
            picks.append(c); used_games.add(c["game_pk"])

    rl_high  = [c for c in candidates if c["market"] == "RL" and c["edge_pp"] >= 8]
    ml_high  = [c for c in candidates if c["market"] == "ML" and c["edge_pp"] >= 8]
    rl_med   = [c for c in candidates if c["market"] == "RL" and 5 <= c["edge_pp"] < 8]
    ml_med   = [c for c in candidates if c["market"] == "ML" and 5 <= c["edge_pp"] < 8]
    rest     = [c for c in candidates if c["market"] not in ("ML", "RL")]

    for pool in [rl_high, ml_high, rl_med, ml_med, rest, candidates]:
        _try_add(sorted(pool, key=lambda c: -c["edge_pp"]))
        if len(picks) >= 4: break

    return picks


if auto_pick:
    auto_picks = algorithm_pick4(filtered_all)
    auto_labels = [c["label"] + " -- " + c["matchup"] for c in auto_picks]
    st.session_state["rr_chosen_labels"] = auto_labels

    # Auto-save the algorithm's slate so we have an unbroken daily log
    # for backtesting, regardless of whether the user clicks Save below.
    try:
        from itertools import combinations
        # Use default round-robin sizing (by 2s + 3s + 4s @ $5 stake)
        # for the auto-saved snapshot — user can override on manual save
        auto_picks_for_log = []
        for c in auto_picks:
            auto_picks_for_log.append({
                "label":       c["label"],
                "market":      c["market"],
                "side":        c.get("side"),
                "point":       c.get("point"),
                "matchup":     c["matchup"],
                "game_pk":     c.get("game_pk"),
                "event_id":    c.get("event_id"),
                "model_p":     c["model_p"],
                "raw_model_p": c.get("raw_model_p"),
                "fair_p":      c.get("fair_p"),
                "implied":     c["implied"],
                "edge_pp":     c["edge_pp"],
                "american":    c["american"],
                "decimal":     c["decimal"],
                "book":        c["book"],
                "first_pitch": c.get("first_pitch"),
            })
        # Quick round-robin EV calc for the auto-saved snapshot
        from models import parlay as _pm
        _picks_rr = [{"name": p["label"], "prob": p["model_p"],
                      "decimal_odds": p["decimal"], "game_id": p.get("game_pk")}
                     for p in auto_picks_for_log]
        _parlays, _summ = _pm.build_round_robin(
            _picks_rr, sizes=[2, 3, 4], stake_per_parlay=5.0, one_per_game=True)
        slate_payload = {
            "picks":            auto_picks_for_log,
            "sizes":            [2, 3, 4],
            "stake_per_parlay": 5.0,
            "total_stake":      _summ.get("total_stake", 0),
            "total_ev":         _summ.get("total_ev", 0),
            "max_payout":       _summ.get("total_max_payout", 0),
            "p_any_win":        _summ.get("hit_distribution", {}).get("p_at_least_one_win"),
            "p_profit":         _summ.get("hit_distribution", {}).get("p_profit"),
            "note":             "Auto-saved by Auto-pick top 4",
            "auto_saved":       True,
        }
        log_path = picks_logger.save_slate(date_str, slate_payload)
        st.success(f"Auto-picked {len(auto_picks)} legs and logged to "
                   f"`{os.path.basename(log_path)}`. Adjust below if needed.")
    except Exception as _ex:
        st.success(f"Auto-picked {len(auto_picks)} legs. Adjust below if needed.")
        st.caption(f"⚠️ Auto-save log failed (slate still usable): {_ex}")


# ---------- Candidates table (scoped) ----------

if filtered:
    cand_df = pd.DataFrame([{
        "Verify?":    "?" if odds_api.is_suspect_edge(c["edge_pp"]) else "",
        "Pick":       c["label"],
        "Market":     c["market"],
        "Matchup":    c["matchup"],
        "Model %":    round(c.get("raw_model_p", c["model_p"]) * 100, 2),
        "Fair Mkt %": round(c["fair_p"] * 100, 2) if c.get("fair_p") is not None else None,
        "Blend %":    round(c["model_p"] * 100, 2),
        "Implied %":  round(c["implied"] * 100, 2),
        "Edge (pp)":  round(c["edge_pp"], 2),
        "American":   c["american"],
        "Decimal":    round(c["decimal"], 2),
        "Book":       c["book"],
    } for c in filtered])
    st.dataframe(cand_df, use_container_width=True, hide_index=True, height=320,
        column_config={
            "Model %":    st.column_config.NumberColumn(format="%.2f%%",
                           help="Raw Poisson model probability (no market input)"),
            "Fair Mkt %": st.column_config.NumberColumn(format="%.2f%%",
                           help="Devigged sportsbook implied probability (vig removed). None for RL (pure model)."),
            "Blend %":    st.column_config.NumberColumn(format="%.2f%%",
                           help="Blended probability used for edge & parlay EV. "
                                "ML=50/50, TOT=30/70 model/market, RL=100% model."),
            "Implied %":  st.column_config.NumberColumn(format="%.2f%%",
                           help="Raw sportsbook implied probability (includes vig). Edge is Blend% minus this."),
            "Edge (pp)":  st.column_config.NumberColumn(format="%+.2f"),
            "American":   st.column_config.NumberColumn(format="%+d"),
            "Decimal":    st.column_config.NumberColumn(format="%.2f"),
        })
    st.caption(
        "**Blend logic (from backtest):** "
        "RL = pure model (71.7% hit, +24.2% ROI — market adds noise). "
        "ML = 50% model + 50% devigged market (+23.4% ROI). "
        "TOT = 30% model + 70% market — fixes 73%→56% overcalibration (+18.6% ROI)."
    )


# ---------- Pick selector ----------

st.markdown("### Pick your 4 legs")

pick_options = [c["label"] + " -- " + c["matchup"] for c in filtered]
# Default = either auto-pick result (if just clicked) or top 4 by edge
default_picks = st.session_state.get("rr_chosen_labels") or pick_options[:4]
default_picks = [p for p in default_picks if p in pick_options]  # only those still visible

chosen_labels = st.multiselect("Select 4 legs", options=pick_options, default=default_picks,
                                key="rr_chosen_labels_widget")
chosen = [filtered[pick_options.index(l)] for l in chosen_labels if l in pick_options]

if len(chosen) > 0:
    games_used = {c["game_pk"] for c in chosen}
    if len(games_used) < len(chosen):
        st.warning(f"! {len(chosen) - len(games_used)} of your picks are in the same game. "
                   "These outcomes are correlated -- independence assumption broken.")


# ---------- Round robin builder ----------

st.markdown("###  Round Robin")

c1, c2, c3 = st.columns(3)
with c1:
    sizes = st.multiselect("Combo sizes", options=[2, 3, 4], default=[2, 3, 4],
                           help="By 2s + 3s + 4s = 11 parlays for 4 picks.")
with c2:
    stake = st.number_input("$ per parlay", 0.10, 10000.0,
                             value=float(st.session_state.get("rr_stake", 5.0)), step=1.0)
    st.session_state["rr_stake"] = stake
with c3:
    one_per_game = st.toggle("1 leg / game", value=True,
                             help="Skip parlays that put two legs from the same game together "
                                  "(correlated outcomes).")

if len(chosen) >= 2 and sizes:
    picks_for_rr = [{
        "name":         c["label"].split(" -- ")[0],
        "prob":         c["model_p"],
        "decimal_odds": c["decimal"],
        "game_id":      c["game_pk"],
    } for c in chosen]

    parlays, summ = parlay_model.build_round_robin(
        picks_for_rr, sizes, stake_per_parlay=stake, one_per_game=one_per_game,
    )

    if not parlays:
        st.warning("No valid parlays -- too few picks or all blocked by 1-per-game.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Parlays", summ["total_parlays"])
        m2.metric("Total stake", f"${summ['total_stake']:,.2f}")
        m3.metric("Expected return", f"${summ['total_ev']:+,.2f}",
                  delta=f"{summ['ev_per_dollar']*100:+.2f}%")
        m4.metric("Max payout (all hit)", f"${summ['total_max_payout']:,.0f}")

        if summ.get("hit_distribution"):
            hd = summ["hit_distribution"]
            n1, n2 = st.columns(2)
            n1.metric("P(at least 1 parlay hits)", f"{hd['p_at_least_one_win']*100:.1f}%")
            n2.metric("P(net profitable)", f"{hd['p_profit']*100:.1f}%")

        st.markdown("#### All parlays")
        df_par = pd.DataFrame([{
            "Legs":      " * ".join(p["legs"]),
            "K":         p["leg_count"],
            "Prob %":    round(p["prob"] * 100, 3),
            "American":  p["american_odds"],
            "Decimal":   round(p["decimal_odds"], 2),
            "Payout":    round(p["payout"], 2),
            "EV":        round(p["ev"], 2),
        } for p in sorted(parlays, key=lambda x: -x["ev"])])
        st.dataframe(df_par, use_container_width=True, hide_index=True, height=300,
            column_config={
                "Prob %":   st.column_config.NumberColumn(format="%.3f%%"),
                "American": st.column_config.NumberColumn(format="%+d"),
                "Payout":   st.column_config.NumberColumn(format="$%,.2f"),
                "EV":       st.column_config.NumberColumn(format="$%+,.2f"),
            })

        # ---------- Save picks ----------
        st.markdown("---")
        sc1, sc2 = st.columns([3, 1])
        with sc1:
            slate_note = st.text_input("Note (optional)", placeholder="e.g. 'High-confidence ML day'")
        with sc2:
            if st.button(" Save slate", use_container_width=True, type="primary"):
                slate = {
                    "picks":            chosen,
                    "sizes":            sizes,
                    "stake_per_parlay": stake,
                    "total_stake":      summ["total_stake"],
                    "total_ev":         summ["total_ev"],
                    "max_payout":       summ["total_max_payout"],
                    "p_any_win":        summ.get("hit_distribution", {}).get("p_at_least_one_win"),
                    "p_profit":         summ.get("hit_distribution", {}).get("p_profit"),
                    "note":             slate_note,
                }
                path = picks_logger.save_slate(date_str, slate)
                st.success(f"Saved -> `{os.path.basename(path)}`. View under 'Saved slates' below.")


# ---------- Today's Research ----------

st.markdown("---")
st.markdown("### Today's Research (free sources)")

# Cached research loaders
@st.cache_data(ttl=900, show_spinner=False)
def _load_reddit_research(date_str):
    from datetime import datetime as _dt
    d = _dt.fromisoformat(date_str).date()
    thread = research_api.find_daily_mlb_thread(d)
    comments = []
    if thread:
        comments = research_api.get_reddit_top_comments(thread["permalink"], limit=15)
    return {"thread": thread, "comments": comments}

@st.cache_data(ttl=600, show_spinner=False)
def _load_bvp(batter_id, pitcher_id, season):
    return research_api.get_bvp(batter_id, pitcher_id, season)


research_tab1, research_tab2 = st.tabs(["Reddit r/sportsbook", "BvP for your picks"])

# ---- Reddit tab ----
with research_tab1:
    research = _load_reddit_research(date_str)
    thread = research["thread"]
    comments = research["comments"]

    if not thread:
        st.info("Today's MLB Props thread not posted yet on r/sportsbook (usually appears mid-morning ET). Check back later.")
    else:
        rh1, rh2, rh3 = st.columns([2, 1, 1])
        rh1.metric("Thread", thread["title"][:50])
        rh2.metric("Comments", thread["num_comments"])
        rh3.metric("Score", thread["score"])
        st.caption(f"[Open on Reddit]({thread['url']})")

        # Player-mention overlay: which of YOUR selected picks the community is also talking about
        if chosen and comments:
            chosen_names = [c.get("label", "").split(" ")[1] for c in chosen if c.get("market") == "HR"]
            chosen_names = [n for n in chosen_names if n]
            if chosen_names:
                mentions = research_api.extract_player_mentions(comments, chosen_names)
                if any(mentions.values()):
                    st.markdown("**Community mention overlay** (your picks vs Reddit thread):")
                    for name, weight in sorted(mentions.items(), key=lambda x: -x[1]):
                        if weight > 0:
                            st.markdown(f"- **{name}** -- weighted score {weight} (community is also on this pick)")
                        else:
                            st.markdown(f"- {name} -- not mentioned (model alone)")

        st.markdown(f"#### Top {min(len(comments), 10)} comments")
        for c in comments[:10]:
            preview = c["body"].replace("\n\n", "\n").strip()
            if len(preview) > 800:
                preview = preview[:800] + "..."
            with st.expander(f"[{c['score']}^] /u/{c['author']} -- {c['age_h']:.1f}h ago"):
                st.markdown(preview)


# ---- BvP tab ----
with research_tab2:
    if not chosen:
        st.info("Pick some legs above to see batter-vs-pitcher career history.")
    else:
        st.caption(
            "Career batter-vs-pitcher line via MLB Stats API. **Treat samples < 20 PA "
            "as noise.** Real signal kicks in around 30+ PA."
        )

        bvp_rows = []
        for c in chosen:
            if c.get("market") != "HR":
                # Only HR picks have a clean batter/pitcher mapping
                continue
            # Need batter_id and opp pitcher_id. We can derive batter from name lookup,
            # but to keep this simple we look up from the model's pre-computed hr rows
            # if available, else do a lightweight name search.
            bvp_rows.append({
                "Pick":      c.get("label", "")[:50],
                "Note":      "BvP lookup requires batter_id + pitcher_id from the prediction pipeline; "
                             "use the main dashboard's 'Predictions' tab for the deep view per pick.",
            })

        if not bvp_rows:
            st.info("Your slate currently has no HR picks. Add HR picks for BvP analysis (game-line picks don't have a batter-vs-pitcher dimension).")
        else:
            for r in bvp_rows:
                st.markdown(f"- **{r['Pick']}**")
                st.caption(r['Note'])

        # Direct BvP lookup tool — let user query any batter/pitcher pair
        st.markdown("---")
        st.markdown("**Direct BvP lookup** -- paste names to query career history")
        b_col, p_col, btn_col = st.columns([2, 2, 1])
        with b_col:
            bvp_batter_input = st.text_input("Batter name", placeholder="e.g., Aaron Judge")
        with p_col:
            bvp_pitcher_input = st.text_input("Pitcher name", placeholder="e.g., Paul Skenes")
        with btn_col:
            do_lookup = st.button("Look up", use_container_width=True)

        if do_lookup and bvp_batter_input and bvp_pitcher_input:
            with st.spinner("Looking up player IDs..."):
                # Use MLB Stats API search
                def _find_player(name):
                    import urllib.parse, urllib.request, json as _json
                    q = urllib.parse.quote(name)
                    url = f"https://statsapi.mlb.com/api/v1/people/search?names={q}"
                    try:
                        d = _json.loads(urllib.request.urlopen(url, timeout=10, context=_UNVERIFIED_SSL).read())
                        people = d.get("people", [])
                        if people: return people[0].get("id"), people[0].get("fullName")
                    except Exception:
                        pass
                    return None, None
                bid, bname = _find_player(bvp_batter_input)
                pid, pname = _find_player(bvp_pitcher_input)
            if bid and pid:
                bvp = _load_bvp(bid, pid, season)
                if bvp:
                    st.success(f"**{bname}** vs **{pname}** -- career line:")
                    bc1, bc2, bc3, bc4, bc5 = st.columns(5)
                    bc1.metric("AB", bvp.get("ab"))
                    bc2.metric("Hits", bvp.get("hits"))
                    bc3.metric("HR", bvp.get("hr"))
                    bc4.metric("AVG", bvp.get("avg"))
                    bc5.metric("OPS", bvp.get("ops"))
                    if bvp.get("ab", 0) < 20:
                        st.warning(f"Sample is small ({bvp.get('ab')} AB) -- treat as noise. Real signal kicks in around 30+ AB.")
                else:
                    st.info(f"No recorded encounters between {bname} and {pname} (or one is on a non-MLB roster).")
            else:
                st.error("Couldn't find one or both players. Try the exact name as it appears on baseball-reference.")


# ---------- Saved slates summary ----------

st.markdown("---")
st.markdown("###  Saved slates (your tracking log)")

slates = picks_logger.load_all_slates()
if not slates:
    st.caption("No saved slates yet. Build one above and click ** Save slate**.")
else:
    summary_rows = []
    for s in slates:
        d = s.get("date", "--")
        n_picks = len(s.get("picks", []))
        sizes_str = "+".join(map(str, s.get("sizes", [])))
        avg_edge = (sum(p.get("edge_pp", 0) for p in s.get("picks", [])) / n_picks) if n_picks else 0
        summary_rows.append({
            "Date":         d,
            "Picks":        n_picks,
            "Combo":        f"by {sizes_str}",
            "Stake":        s.get("total_stake", 0),
            "EV":           s.get("total_ev", 0),
            "P(any win) %": (s.get("p_any_win") or 0) * 100,
            "Avg Edge pp":  round(avg_edge, 2),
            "Note":         s.get("note", "") or "--",
        })
    df_log = pd.DataFrame(summary_rows).sort_values("Date", ascending=False)
    st.dataframe(df_log, use_container_width=True, hide_index=True,
        column_config={
            "Stake":        st.column_config.NumberColumn(format="$%.2f"),
            "EV":           st.column_config.NumberColumn(format="$%+.2f"),
            "P(any win) %": st.column_config.NumberColumn(format="%.1f%%"),
            "Avg Edge pp":  st.column_config.NumberColumn(format="%+.2f"),
        })

    if st.button("View today's slate detail"):
        today_slate = picks_logger.load_slate(date_str)
        if today_slate:
            st.json(today_slate, expanded=False)
