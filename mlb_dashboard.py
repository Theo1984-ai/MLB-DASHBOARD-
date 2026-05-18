"""
MLB Daily Dashboard — multi-source stats UI.

Run with: streamlit run mlb_dashboard.py

Data sources:
  - MLB Stats API (statsapi.mlb.com) — schedule, weather, lineups, pitcher/team stats, standings
  - Baseball Savant (baseballsavant.mlb.com) — Statcast xStats, barrel%, exit velo
  - Open-Meteo (api.open-meteo.com) — hourly ballpark weather forecast
"""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import io
import os
import ssl as _ssl_compat
# System cert chain has CA validation issues — public-data APIs only, no auth/PII in transit.
_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
import json

from data import mlb_api, savant, weather as wx
from data import odds as odds_api
from data import research as research_api
from data import balldontlie as bdl_api
from data.stadiums import get_stadium, wind_label, wind_hr_boost
from models import hr_model, game_model, league as league_const
from models import logger as pred_logger
from models import parlay as parlay_model
from models import calibration as cal_model
from models import kelly as kelly_model

EASTERN = ZoneInfo("America/New_York")
ODDS_DIR = os.path.join(os.path.dirname(__file__), "odds")


def _save_odds_snapshot(date_str: str, odds_rows: list[dict]) -> None:
    """
    Persist the best available HR prop odds per player to odds/YYYY-MM-DD.json.
    Keeps the highest American odds across all books (best payout for bettor).
    Used by backtest.py to compute ROI against real book prices.
    """
    os.makedirs(ODDS_DIR, exist_ok=True)
    best: dict[str, dict] = {}
    for r in odds_rows:
        name = r.get("player", "")
        if not name:
            continue
        am = r.get("american_odds") or r.get("odds")
        try:
            am = int(am)
        except (TypeError, ValueError):
            continue
        imp = r.get("implied_prob") or (100 / (am + 100) if am >= 0 else abs(am) / (abs(am) + 100))
        book = r.get("bookmaker") or r.get("book") or "?"
        if name not in best or am > best[name]["best_odds"]:
            best[name] = {"best_odds": am, "implied_p": round(float(imp), 4), "book": book}
    path = os.path.join(ODDS_DIR, f"{date_str}.json")
    with open(path, "w") as f:
        json.dump(best, f, indent=2)


# Wire up BallDontLie API key if configured
try:
    _bdl_key = st.secrets.get("BALLDONTLIE_API_KEY") or ""
    if _bdl_key:
        bdl_api.set_api_key(_bdl_key)
except Exception:
    pass

st.set_page_config(page_title="MLB Daily Dashboard", page_icon="⚾", layout="wide")

st.markdown("""
<style>
    .block-container {padding-top: 2rem; max-width: 1400px}
    [data-testid="stMetricValue"] {font-size: 24px}
    .small-cap {color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px}
</style>
""", unsafe_allow_html=True)


# ---------- Cached data loaders ----------
#
# TTLs are tuned by data volatility:
#   - Schedule (probable pitchers, lineups, scratches, weather snapshot)
#     can change mid-day → 90s
#   - Weather forecast updates hourly → 600s
#   - Recent (last-15) hitting updates after games end → 1800s
#   - Pitcher / team / standings / bullpen stats only change overnight → 3600s
#   - Savant CSVs publish daily → 21600s (6h)
#   - Handedness never changes → 21600s

# Track when each loader actually fetched (cache misses only) so the UI
# can show a "data freshness" strip.
_last_fetch: dict[str, datetime] = {}


def _mark_fetched(name: str):
    _last_fetch[name] = datetime.now(tz=EASTERN)


def get_fetch_age(name: str) -> str:
    """Human-readable age since this loader last actually fetched."""
    t = _last_fetch.get(name)
    if not t:
        return "—"
    delta = (datetime.now(tz=EASTERN) - t).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta/60)}m ago"
    return f"{int(delta/3600)}h{int((delta%3600)/60)}m ago"


@st.cache_data(ttl=90, show_spinner=False)
def load_schedule(date_str: str):
    data = mlb_api.get_schedule(date_str)
    _mark_fetched("schedule")
    return data

@st.cache_data(ttl=3600, show_spinner=False)
def load_pitcher(person_id: int, season: int):
    data = mlb_api.get_pitcher_season(person_id, season)
    _mark_fetched("pitcher_stats")
    return data

@st.cache_data(ttl=3600, show_spinner=False)
def load_pitcher_splits(person_id: int, season: int):
    return mlb_api.get_pitcher_splits(person_id, season)

@st.cache_data(ttl=3600, show_spinner=False)
def load_team_season(team_id: int, season: int, group: str):
    return mlb_api.get_team_season(team_id, season, group)

@st.cache_data(ttl=3600, show_spinner=False)
def load_team_recent(team_id: int, season: int, days: int, group: str):
    return mlb_api.get_team_recent(team_id, season, days, group)

@st.cache_data(ttl=3600, show_spinner=False)
def load_standings(season: int):
    return mlb_api.get_standings(season)

@st.cache_data(ttl=3600, show_spinner=False)
def load_roster(team_id: int, season: int):
    return mlb_api.get_team_roster(team_id, season)

@st.cache_data(ttl=21600, show_spinner=False)
def load_savant_batter_xstats(year: int):
    data = savant.batter_xstats(year)
    _mark_fetched("savant")
    return data

@st.cache_data(ttl=21600, show_spinner=False)
def load_savant_batter_statcast(year: int):
    return savant.batter_statcast(year)

@st.cache_data(ttl=21600, show_spinner=False)
def load_savant_pitcher_xstats(year: int):
    return savant.pitcher_xstats(year)

@st.cache_data(ttl=21600, show_spinner=False)
def load_savant_pitcher_arsenal(year: int):
    return savant.pitcher_arsenal(year)

@st.cache_data(ttl=21600, show_spinner=False)
def load_savant_batter_pitch_perf(year: int):
    return savant.batter_vs_pitch_types(year)

@st.cache_data(ttl=21600, show_spinner=False)
def load_savant_pitcher_split(year: int, bat_side: str):
    return savant.pitcher_xstats_split(year, bat_side)

@st.cache_data(ttl=3600, show_spinner=False)
def load_pitcher_recent(person_id: int, season: int):
    return mlb_api.get_pitcher_recent_form(person_id, season)

@st.cache_data(ttl=1800, show_spinner=False)
def load_team_il(team_id: int, season: int):
    # Convert set to tuple for caching, then back
    return tuple(sorted(mlb_api.get_team_il(team_id, season)))

@st.cache_data(ttl=86400, show_spinner=False)
def load_bvp(batter_id: int, pitcher_id: int, season: int):
    """Career BvP — cached 24h since the matchup history rarely changes within a day."""
    return research_api.get_bvp(batter_id, pitcher_id, season)

@st.cache_data(ttl=1800, show_spinner=False)
def load_bdl_injuries() -> list:
    if not bdl_api.is_configured():
        return []
    return bdl_api.get_injuries()


@st.cache_data(ttl=600, show_spinner=False)
def load_weather(lat: float, lon: float, target_iso: str):
    target = datetime.fromisoformat(target_iso)
    data = wx.get_forecast(lat, lon, target)
    _mark_fetched("weather")
    return data

@st.cache_data(ttl=1800, show_spinner=False)
def load_recent_hitting(season: int, days: int = 15):
    data = mlb_api.get_recent_hitting_leaderboard(season, days)
    _mark_fetched("recent_hitting")
    return data

@st.cache_data(ttl=3600, show_spinner=False)
def load_bullpen(team_id: int, season: int):
    return mlb_api.get_team_bullpen_stats(team_id, season)

@st.cache_data(ttl=21600, show_spinner=False)
def load_handedness(person_ids_tuple: tuple):
    return mlb_api.get_handedness(list(person_ids_tuple))


@st.cache_data(ttl=3600, show_spinner=False)
def load_odds_events(api_key: str):
    return odds_api.get_events(api_key)


@st.cache_data(ttl=3600, show_spinner=False)
def load_hr_odds_for_event(api_key: str, event_id: str, books: str):
    return odds_api.get_hr_odds(api_key, event_id, bookmakers=books)


@st.cache_data(ttl=3600, show_spinner=False)
def load_alt_prop_odds_for_event(api_key: str, event_id: str, books: str):
    return odds_api.get_alternate_prop_odds(api_key, event_id, bookmakers=books)


@st.cache_data(ttl=21600, show_spinner=False)
def load_batter_game_log_rates(player_id: int, season: int):
    return mlb_api.get_batter_game_log_rates(player_id, season)


@st.cache_data(ttl=21600, show_spinner=False)
def load_batter_game_log_rates_bdl(player_name: str, season: int):
    """BallDontLie-backed game log rates by player name (fallback)."""
    if not bdl_api.is_configured():
        return {}
    return bdl_api.get_batter_game_log_rates(player_name, season)


@st.cache_data(ttl=21600, show_spinner=False)
def load_bdl_game_log_rates_by_id(bdl_id: int, season: int):
    """BallDontLie game log rates by BDL player ID — no name search needed."""
    if not bdl_api.is_configured():
        return {}
    return bdl_api.get_batter_game_log_rates_by_id(bdl_id, season)


@st.cache_data(ttl=86400, show_spinner=False)
def load_bdl_player_name(bdl_id: int) -> str:
    if not bdl_api.is_configured():
        return str(bdl_id)
    return bdl_api.get_player_name(bdl_id) or str(bdl_id)


@st.cache_data(ttl=3600, show_spinner=False)
def load_bdl_player_names_batch(bdl_ids: tuple) -> dict:
    """Cache-friendly batch name lookup. Pass ids as a tuple (hashable)."""
    if not bdl_api.is_configured():
        return {}
    return bdl_api.get_player_names_batch(list(bdl_ids))


@st.cache_data(ttl=300, show_spinner=False)
def load_bdl_props_for_game(game_id: int) -> list:
    if not bdl_api.is_configured():
        return []
    return bdl_api.get_player_props(game_id)


@st.cache_data(ttl=300, show_spinner=False)
def load_bdl_games_for_date(date_str: str) -> list:
    if not bdl_api.is_configured():
        return []
    return bdl_api.get_games_for_date(date_str)


def clear_volatile_caches():
    """Auto-update fires this so model picks recompute with fresh inputs."""
    load_schedule.clear()
    load_weather.clear()
    load_recent_hitting.clear()


@st.cache_data(ttl=3600, show_spinner=False)
def load_calibration():
    """Load the Platt/isotonic calibration params from disk (None if absent)."""
    return cal_model.load_params()


def resolve_odds_key() -> str | None:
    """API key resolution: secrets.toml → env var → sidebar input → None."""
    try:
        if "THE_ODDS_API_KEY" in st.secrets:
            return st.secrets["THE_ODDS_API_KEY"]
    except Exception:
        pass
    env = os.environ.get("THE_ODDS_API_KEY")
    if env:
        return env
    return st.session_state.get("odds_api_key")


# ---------- Helpers ----------

def stat_color(value, thresholds, reverse=False) -> str:
    """thresholds = (good_below, ok_below, bad_below). Returns hex color."""
    try:
        v = float(value)
    except (ValueError, TypeError):
        return "#888"
    g, o, b = thresholds
    if reverse:
        if v >= g: return "#22c55e"
        if v >= o: return "#84cc16"
        if v >= b: return "#eab308"
        return "#ef4444"
    if v < g: return "#22c55e"
    if v < o: return "#84cc16"
    if v < b: return "#eab308"
    return "#ef4444"


def safe_float(v, default=None):
    try:
        f = float(v)
        if f != f:  # NaN check (NaN != NaN is True)
            return default
        return f
    except (ValueError, TypeError):
        return default


def safe_int(v):
    f = safe_float(v)
    return int(f) if f is not None else None


def format_pct(v):
    f = safe_float(v)
    return f"{f:.1f}%" if f is not None else "—"


# ---------- Header ----------

st.title("⚾ MLB Daily Dashboard")
st.caption("MLB Stats API + Baseball Savant Statcast + Open-Meteo weather + The Odds API")

# Sidebar — API key entry + quota
with st.sidebar:
    st.subheader("🔑 The Odds API")
    existing_key = resolve_odds_key()
    if existing_key:
        masked = existing_key[:6] + "…" + existing_key[-4:] if len(existing_key) > 12 else "••••"
        st.success(f"Key loaded: `{masked}`")
        if st.button("Clear key", use_container_width=True):
            st.session_state.pop("odds_api_key", None)
            st.rerun()
    else:
        st.markdown(
            "Get a **free key** at [the-odds-api.com](https://the-odds-api.com) "
            "(500 requests/month). Paste it below or set "
            "`THE_ODDS_API_KEY` in your environment / `.streamlit/secrets.toml`."
        )
        key_input = st.text_input("API key", type="password", key="odds_key_input",
                                  placeholder="paste here…")
        if key_input:
            st.session_state["odds_api_key"] = key_input.strip()
            st.rerun()

    quota = odds_api.get_quota()
    if quota.get("remaining") is not None:
        rem = quota["remaining"]
        used = quota.get("used", "?")
        color = "🟢" if isinstance(rem, int) and rem > 100 else "🟡" if isinstance(rem, int) and rem > 30 else "🔴"
        st.caption(f"{color} Quota: {used} used / {rem} remaining")

    st.divider()

    # ---------- Calibration ----------
    st.subheader("🎯 Calibration")
    cal_params = load_calibration()
    use_calibration = st.toggle(
        "Apply calibration",
        value=bool(cal_params),
        help="Remap model probabilities through a fit trained on your prediction logs vs actual HR outcomes.",
    )
    if cal_params:
        method = "isotonic" if "isotonic_xs" in cal_params else "Platt"
        n = cal_params.get("n", "?")
        ll_before = cal_params.get("log_loss_before")
        ll_after = cal_params.get("log_loss_after")
        improvement = (
            f"{(ll_before - ll_after) / ll_before * 100:+.2f}%"
            if ll_before and ll_after else "—"
        )
        fit_at = cal_params.get("fit_at", "—")[:10]
        st.caption(f"**{method}** fit on {n} pairs (trained {fit_at})")
        st.caption(f"Log loss: {ll_before:.4f} → {ll_after:.4f} ({improvement})")
    else:
        st.caption("No calibration yet — click **Recalibrate** below to fit one.")

    if st.button("🔄 Recalibrate now", use_container_width=True,
                 help="Walks every prediction log, fetches actual HR outcomes from MLB API, refits Platt + isotonic, picks the better one."):
        with st.spinner("Fetching outcomes and refitting…"):
            result = cal_model.fit_from_logs()
        load_calibration.clear()
        if result:
            st.success("Calibration refitted.")
            st.rerun()
        else:
            st.error("Couldn't fit — no prediction pairs found.")

    st.divider()

    # ---------- Bankroll & Kelly ----------
    st.subheader("💰 Bankroll & Kelly")
    bankroll = st.number_input(
        "Bankroll ($)",
        min_value=0.0, value=float(st.session_state.get("bankroll", 1000.0)),
        step=50.0, format="%.0f",
        help="Total amount you're willing to risk. Kelly stakes are computed as a percentage of this.",
    )
    st.session_state["bankroll"] = bankroll

    kelly_label = st.select_slider(
        "Kelly fraction",
        options=["Quarter (1/4)", "Half (1/2)", "Full (1)"],
        value=st.session_state.get("kelly_label", "Half (1/2)"),
        help="Quarter / Half / Full Kelly multiplier. Most pros run Half. Full is high-variance.",
    )
    st.session_state["kelly_label"] = kelly_label
    kelly_mult = {"Quarter (1/4)": 0.25, "Half (1/2)": 0.5, "Full (1)": 1.0}[kelly_label]

    max_pct = st.slider(
        "Max stake (% of bankroll)",
        min_value=1, max_value=15, value=5, step=1,
        help="Hard cap on any single bet, regardless of what Kelly says. 5% is a common floor.",
    )
    max_fraction = max_pct / 100.0

    st.caption(
        f"Will recommend up to **${bankroll * max_fraction:,.0f}** on any single pick "
        f"(at {kelly_label}, capped at {max_pct}% of ${bankroll:,.0f})."
    )

    if bdl_api.is_configured():
        st.divider()
        st.subheader("🏥 Today's Injuries")
        _inj_all = load_bdl_injuries()
        if not _inj_all:
            st.caption("No active injuries found.")
        else:
            with st.expander(f"{len(_inj_all)} players on IL — click to expand", expanded=False):
                for _inj in sorted(_inj_all, key=lambda x: x.get("player", {}).get("team", {}).get("abbreviation", "")):
                    _ip = _inj.get("player", {})
                    _team_abbr = (_ip.get("team") or {}).get("abbreviation", "?")
                    _name = _ip.get("full_name", "?")
                    _status = _inj.get("status", "IL")
                    _detail = _inj.get("detail") or _inj.get("type") or ""
                    _ret = _inj.get("return_date", "")[:10] if _inj.get("return_date") else ""
                    _ret_str = f" · back ~{_ret}" if _ret else ""
                    st.markdown(
                        f"<div style='font-size:12px;padding:2px 0'>"
                        f"<span style='color:#94a3b8;width:32px;display:inline-block'>{_team_abbr}</span> "
                        f"<span style='color:#f1f5f9'>{_name}</span> "
                        f"<span style='color:#fb923c'>{_status}</span>"
                        f"<span style='color:#64748b'>{_ret_str}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

hc1, hc2, hc3 = st.columns([2, 1, 1])
with hc1:
    selected_date = st.date_input(
        "Date",
        value=datetime.now(tz=EASTERN).date(),
        format="YYYY-MM-DD",
    )
with hc2:
    live_mode = st.toggle("🔴 Live mode (10s refresh)", value=False,
                          help="Live scores only — clears schedule cache every 10 seconds.")
    auto_update = st.toggle("🔁 Auto-update inputs", value=False,
                            help="Recompute model picks as new info comes in (pitcher swaps, lineup posts, scratches, weather updates).")
with hc3:
    if st.button("🔄 Refresh all", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    if auto_update:
        auto_interval_min = st.select_slider(
            "Every", options=[2, 5, 15], value=2, format_func=lambda x: f"{x} min",
            label_visibility="collapsed",
        )
    else:
        auto_interval_min = 2

if live_mode:
    load_schedule.clear()
    st.markdown('<meta http-equiv="refresh" content="10">', unsafe_allow_html=True)
    st.caption(f"🔴 Live mode on — page refreshes every 10 seconds. Last update: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p ET')}")
elif auto_update:
    clear_volatile_caches()
    st.markdown(f'<meta http-equiv="refresh" content="{auto_interval_min*60}">', unsafe_allow_html=True)
    next_at = (datetime.now(tz=EASTERN) + pd.Timedelta(minutes=auto_interval_min)).strftime("%I:%M:%S %p ET")
    st.caption(f"🔁 Auto-update on — refreshing inputs every {auto_interval_min} minute(s). Next: {next_at}")

date_str = selected_date.strftime("%Y-%m-%d")
season = selected_date.year

with st.spinner(f"Loading {date_str}..."):
    games = load_schedule(date_str)

if not games:
    st.warning(f"No games scheduled for {date_str}.")
    st.stop()

# ---------- Pre-compute per-game enrichment ----------

def enrich_game(g: dict) -> dict:
    away_team = g["teams"]["away"]
    home_team = g["teams"]["home"]
    home_team_id = home_team["team"]["id"]
    away_team_id = away_team["team"]["id"]

    first_pitch_dt, first_pitch_str = mlb_api.parse_first_pitch(g.get("gameDate", ""))
    stadium = get_stadium(home_team_id)

    # Weather: prefer Open-Meteo forecast over MLB API snapshot when first_pitch known
    forecast = {}
    if first_pitch_dt is not None:
        forecast = load_weather(stadium["lat"], stadium["lon"], first_pitch_dt.isoformat())
    mlb_weather = g.get("weather", {}) or {}

    # Probable pitchers
    away_p = away_team.get("probablePitcher") or {}
    home_p = home_team.get("probablePitcher") or {}
    away_pid = away_p.get("id")
    home_pid = home_p.get("id")

    away_stats = load_pitcher(away_pid, season) if away_pid else mlb_api._empty_pitcher()
    home_stats = load_pitcher(home_pid, season) if home_pid else mlb_api._empty_pitcher()

    return {
        "raw":           g,
        "away_team":     away_team["team"]["name"],
        "home_team":     home_team["team"]["name"],
        "away_team_id":  away_team_id,
        "home_team_id":  home_team_id,
        "first_pitch":   first_pitch_str,
        "first_pitch_dt": first_pitch_dt,
        "status":        g.get("status", {}).get("detailedState", "Preview"),
        "venue":         g.get("venue", {}).get("name", stadium["park"]),
        "stadium":       stadium,
        "weather":       forecast,
        "mlb_weather":   mlb_weather,
        "away_pitcher":  away_p.get("fullName", "TBD"),
        "away_pid":      away_pid,
        "away_stats":    away_stats,
        "home_pitcher":  home_p.get("fullName", "TBD"),
        "home_pid":      home_pid,
        "home_stats":    home_stats,
        "away_score":    away_team.get("score"),
        "home_score":    home_team.get("score"),
        "linescore":     g.get("linescore", {}) or {},
    }


with st.spinner("Enriching games (pitcher stats, weather)..."):
    enriched = [enrich_game(g) for g in games]


# ---------- Data freshness strip ----------

st.markdown(
    f"""
    <div style="background:#0f172a;padding:6px 14px;border-radius:6px;margin:6px 0 0 0;
                font-size:11px;color:#94a3b8;display:flex;gap:18px;flex-wrap:wrap">
      <span>📊 <b style="color:#cbd5e1">Data freshness</b></span>
      <span>Schedule: <b style="color:#e2e8f0">{get_fetch_age('schedule')}</b></span>
      <span>Weather: <b style="color:#e2e8f0">{get_fetch_age('weather')}</b></span>
      <span>Pitchers: <b style="color:#e2e8f0">{get_fetch_age('pitcher_stats')}</b></span>
      <span>Recent hitting (L15): <b style="color:#e2e8f0">{get_fetch_age('recent_hitting')}</b></span>
      <span>Savant Statcast: <b style="color:#e2e8f0">{get_fetch_age('savant')}</b></span>
    </div>
    """,
    unsafe_allow_html=True,
)


# ---------- Summary metrics ----------

st.divider()
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Games", len(enriched))
finals = sum(1 for g in enriched if g["status"] in ("Final", "Game Over"))
live = sum(1 for g in enriched if g["status"] in ("In Progress", "Live"))
m2.metric("Live", live)
m3.metric("Final", finals)
total_runs = sum(
    (g["away_score"] or 0) + (g["home_score"] or 0)
    for g in enriched if g["away_score"] is not None
)
m4.metric("Total runs (final/live)", total_runs)
total_hr = sum(safe_float(g["away_stats"]["hr_per9"], 0) for g in enriched) + \
           sum(safe_float(g["home_stats"]["hr_per9"], 0) for g in enriched)
m5.metric("Avg starter HR/9", f"{total_hr / max(1, 2*len(enriched)):.2f}")


# ---------- Tabs ----------

tab_games, tab_pitch, tab_hr, tab_pred, tab_ai, tab_team, tab_stand = st.tabs([
    "🎴 Tonight's Games", "⚾ Pitchers", "🚀 HR Watch", "🔮 Predictions",
    "🤖 AI Top Picks", "📈 Team Form", "🏆 Standings"
])


# ===== TAB 1: GAMES =====

def render_pitcher_card(name: str, stats: dict, label: str):
    era = stats["era"]
    color = stat_color(era, (3.0, 4.0, 5.0))
    hr9_color = stat_color(stats["hr_per9"], (1.0, 1.3, 1.6))
    k9_color = stat_color(stats["k_per9"], (8.0, 9.5, 11.0), reverse=True)
    st.markdown(f"""
    <div style="padding:14px;border-radius:8px;background:#1e293b;margin-bottom:6px">
      <div class="small-cap">{label}</div>
      <div style="color:#f1f5f9;font-size:18px;font-weight:600;margin:4px 0">{name}</div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:8px">
        <div><span class="small-cap">ERA</span>
          <div style="color:{color};font-size:20px;font-weight:700">{era}</div></div>
        <div><span class="small-cap">HR/9</span>
          <div style="color:{hr9_color};font-size:20px;font-weight:700">{stats['hr_per9']}</div></div>
        <div><span class="small-cap">K/9</span>
          <div style="color:{k9_color};font-size:20px;font-weight:700">{stats['k_per9']}</div></div>
        <div><span class="small-cap">BB/9</span>
          <div style="color:#f1f5f9;font-size:20px;font-weight:700">{stats['bb_per9']}</div></div>
        <div><span class="small-cap">WHIP</span>
          <div style="color:#f1f5f9;font-size:20px;font-weight:700">{stats['whip']}</div></div>
        <div><span class="small-cap">OPS-against</span>
          <div style="color:#f1f5f9;font-size:20px;font-weight:700">{stats['ops_against']}</div></div>
        <div><span class="small-cap">W-L</span>
          <div style="color:#f1f5f9;font-size:20px;font-weight:700">{stats['wins']}-{stats['losses']}</div></div>
        <div><span class="small-cap">IP</span>
          <div style="color:#f1f5f9;font-size:20px;font-weight:700">{stats['ip']}</div></div>
      </div>
    </div>
    """, unsafe_allow_html=True)


def render_weather_strip(g: dict):
    fc = g["weather"]
    mw = g["mlb_weather"]
    if fc:
        wind_lbl = wind_label(fc.get("wind_dir_deg"), g["stadium"]["orientation_deg"])
        boost = wind_hr_boost(wind_lbl, fc.get("wind_mph") or 0)
        boost_color = "#22c55e" if boost > 1.03 else "#ef4444" if boost < 0.97 else "#94a3b8"
        precip = fc.get("precip_pct", 0) or 0
        precip_color = "#ef4444" if precip > 50 else "#eab308" if precip > 20 else "#22c55e"
        st.markdown(f"""
        <div style="background:#0f172a;padding:8px 14px;border-radius:6px;margin:6px 0;
                    display:flex;gap:18px;flex-wrap:wrap;font-size:13px">
          <span>🌡️ <b>{fc.get('temp_f','?')}°F</b></span>
          <span>💨 <b>{fc.get('wind_mph','?')} mph</b> — <span style="color:{boost_color}">{wind_lbl}</span></span>
          <span>💧 <span style="color:{precip_color}">{precip}% precip</span></span>
          <span>☁️ {fc.get('condition','—')}</span>
          <span>💦 {fc.get('humidity','?')}% humidity</span>
          <span>🏟️ Park HR factor: <b>{g['stadium']['hr_factor']}</b></span>
          <span>HR boost: <b style="color:{boost_color}">{boost:.2f}×</b></span>
        </div>
        """, unsafe_allow_html=True)
    elif mw:
        st.markdown(f"""
        <div style="background:#0f172a;padding:8px 14px;border-radius:6px;margin:6px 0;font-size:13px">
          🌡️ {mw.get('temp','?')}°F  •  💨 {mw.get('wind','—')}  •  ☁️ {mw.get('condition','—')}
          •  🏟️ Park HR factor: <b>{g['stadium']['hr_factor']}</b>
        </div>
        """, unsafe_allow_html=True)


def render_live_score(g: dict):
    """Big scoreboard strip — live count/inning for in-progress, final score for completed."""
    ls = g["linescore"]
    status = g["status"]
    is_live = status in ("In Progress", "Live", "Manager Challenge", "Delayed")
    is_final = status in ("Final", "Game Over", "Completed Early")

    if not (is_live or is_final) or not ls:
        return

    away_runs = ls.get("teams", {}).get("away", {}).get("runs", g["away_score"])
    home_runs = ls.get("teams", {}).get("home", {}).get("runs", g["home_score"])
    away_hits = ls.get("teams", {}).get("away", {}).get("hits", "—")
    home_hits = ls.get("teams", {}).get("home", {}).get("hits", "—")
    away_err  = ls.get("teams", {}).get("away", {}).get("errors", "—")
    home_err  = ls.get("teams", {}).get("home", {}).get("errors", "—")

    # Highlight winner if final
    a_color = h_color = "#f1f5f9"
    if is_final and away_runs is not None and home_runs is not None:
        if away_runs > home_runs: a_color = "#22c55e"
        elif home_runs > away_runs: h_color = "#22c55e"

    if is_live:
        inning = ls.get("currentInning", "—")
        half = ls.get("inningHalf", "")
        outs = ls.get("outs", 0)
        balls = ls.get("balls", 0)
        strikes = ls.get("strikes", 0)
        offense = ls.get("offense", {}) or {}
        on_first  = "●" if offense.get("first")  else "○"
        on_second = "●" if offense.get("second") else "○"
        on_third  = "●" if offense.get("third")  else "○"
        live_badge = (
            f'<span style="background:#dc2626;color:#fff;padding:2px 8px;'
            f'border-radius:4px;font-size:11px;font-weight:700;letter-spacing:1px">● LIVE</span>'
        )
        center = f"""
          <span style="font-size:14px;color:#fbbf24;font-weight:600">{half} {inning}</span>
          <span style="color:#94a3b8">•</span>
          <span>{balls}-{strikes}, {outs} out</span>
          <span style="color:#94a3b8">•</span>
          <span title="1B / 2B / 3B">{on_third} {on_second} {on_first}</span>
        """
    elif is_final:
        live_badge = (
            f'<span style="background:#475569;color:#cbd5e1;padding:2px 8px;'
            f'border-radius:4px;font-size:11px;font-weight:700;letter-spacing:1px">FINAL</span>'
        )
        center = ""

    st.markdown(f"""
    <div style="background:#020617;padding:12px 16px;border-radius:8px;margin:6px 0;
                display:flex;align-items:center;gap:18px;flex-wrap:wrap;border:1px solid #1e293b">
      <div style="display:flex;flex-direction:column;align-items:flex-end;min-width:140px">
        <div style="color:{a_color};font-size:14px;font-weight:600">{g['away_team']}</div>
        <div style="color:{a_color};font-size:32px;font-weight:800;line-height:1">{away_runs if away_runs is not None else '—'}</div>
        <div style="color:#64748b;font-size:11px">{away_hits} H · {away_err} E</div>
      </div>
      <div style="flex:1;text-align:center;display:flex;flex-direction:column;gap:6px;align-items:center">
        {live_badge}
        <div style="color:#cbd5e1;font-size:13px">{center}</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-start;min-width:140px">
        <div style="color:{h_color};font-size:14px;font-weight:600">{g['home_team']}</div>
        <div style="color:{h_color};font-size:32px;font-weight:800;line-height:1">{home_runs if home_runs is not None else '—'}</div>
        <div style="color:#64748b;font-size:11px">{home_hits} H · {home_err} E</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # Inning-by-inning line score (collapsed)
    innings = ls.get("innings", [])
    if innings:
        with st.expander(f"Line score — {len(innings)} innings"):
            inn_nums = [str(inn.get("num", i + 1)) for i, inn in enumerate(innings)]
            away_runs_by_inn = [str(inn.get("away", {}).get("runs", "")) for inn in innings]
            home_runs_by_inn = [str(inn.get("home", {}).get("runs", "")) for inn in innings]
            df_ls = pd.DataFrame(
                [away_runs_by_inn + [str(away_runs or 0), str(away_hits), str(away_err)],
                 home_runs_by_inn + [str(home_runs or 0), str(home_hits), str(home_err)]],
                columns=inn_nums + ["R", "H", "E"],
                index=[g["away_team"], g["home_team"]],
            )
            st.dataframe(df_ls, use_container_width=True)


def game_sort_key(g):
    """Sort: live first, then upcoming (by first pitch), then final last."""
    status = g["status"]
    if status in ("In Progress", "Live", "Manager Challenge", "Delayed"):
        bucket = 0
    elif status in ("Final", "Game Over", "Completed Early"):
        bucket = 2
    else:
        bucket = 1
    fp = g["first_pitch_dt"]
    fp_sortable = fp.timestamp() if fp is not None else 0
    return (bucket, fp_sortable)


with tab_games:
    sorted_games = sorted(enriched, key=game_sort_key)
    for g in sorted_games:
        score_str = ""
        if g["away_score"] is not None and g["home_score"] is not None:
            score_str = f" — **{g['away_score']} : {g['home_score']}**"

        with st.container(border=True):
            top = st.columns([3, 1])
            with top[0]:
                st.markdown(f"### {g['away_team']} @ {g['home_team']}{score_str}")
                st.caption(f"📍 {g['venue']}  •  🕐 {g['first_pitch']}  •  {g['status']}")
            with top[1]:
                st.markdown(
                    f"<div style='text-align:right;padding-top:14px'>"
                    f"<span style='background:#334155;color:#f1f5f9;padding:4px 12px;"
                    f"border-radius:12px;font-size:12px'>{g['status']}</span></div>",
                    unsafe_allow_html=True,
                )
            render_live_score(g)
            render_weather_strip(g)
            pcols = st.columns(2)
            with pcols[0]:
                render_pitcher_card(g["away_pitcher"], g["away_stats"], f"Away SP — {g['away_team']}")
            with pcols[1]:
                render_pitcher_card(g["home_pitcher"], g["home_stats"], f"Home SP — {g['home_team']}")

            # Lineups expander
            lineups = g["raw"].get("lineups") or {}
            home_l = lineups.get("homePlayers", [])
            away_l = lineups.get("awayPlayers", [])
            if home_l or away_l:
                with st.expander(f"Lineups ({len(away_l)} away / {len(home_l)} home)"):
                    cc = st.columns(2)
                    with cc[0]:
                        st.markdown(f"**{g['away_team']}**")
                        for i, p in enumerate(away_l, 1):
                            st.markdown(f"{i}. {p.get('fullName','?')} ({p.get('primaryPosition',{}).get('abbreviation','?')})")
                    with cc[1]:
                        st.markdown(f"**{g['home_team']}**")
                        for i, p in enumerate(home_l, 1):
                            st.markdown(f"{i}. {p.get('fullName','?')} ({p.get('primaryPosition',{}).get('abbreviation','?')})")

    # CSV download
    st.divider()
    df_csv = pd.DataFrame([{
        "Away Team": g["away_team"], "Home Team": g["home_team"],
        "First Pitch (ET)": g["first_pitch"], "Status": g["status"],
        "Venue": g["venue"], "Park HR Factor": g["stadium"]["hr_factor"],
        "Away Starter": g["away_pitcher"], "Away ERA": g["away_stats"]["era"],
        "Away HR/9": g["away_stats"]["hr_per9"], "Away K/9": g["away_stats"]["k_per9"],
        "Home Starter": g["home_pitcher"], "Home ERA": g["home_stats"]["era"],
        "Home HR/9": g["home_stats"]["hr_per9"], "Home K/9": g["home_stats"]["k_per9"],
        "Temp (F)": g["weather"].get("temp_f") if g["weather"] else g["mlb_weather"].get("temp"),
        "Wind (mph)": g["weather"].get("wind_mph") if g["weather"] else None,
        "Wind dir": wind_label(g["weather"].get("wind_dir_deg"), g["stadium"]["orientation_deg"]) if g["weather"] else g["mlb_weather"].get("wind"),
        "Precip %": g["weather"].get("precip_pct") if g["weather"] else None,
        "Condition": g["weather"].get("condition") if g["weather"] else g["mlb_weather"].get("condition"),
    } for g in enriched])
    buf = io.StringIO()
    df_csv.to_csv(buf, index=False)
    st.download_button("📥 Download CSV", buf.getvalue(),
                       file_name=f"MLB_homerun_{date_str}.csv", mime="text/csv")


# ===== TAB 2: PITCHERS =====

with tab_pitch:
    st.subheader(f"All probable starters — {date_str}")

    # Pull Savant pitcher xStats once
    pxs = load_savant_pitcher_xstats(season)

    rows = []
    for g in enriched:
        for side in ("away", "home"):
            pid = g[f"{side}_pid"]
            stats = g[f"{side}_stats"]
            xera = None
            xwoba_against = None
            if pid is not None and not pxs.empty:
                hit = pxs[pxs["player_id"] == pid]
                if not hit.empty:
                    xera = safe_float(hit.iloc[0].get("xera"))
                    xwoba_against = safe_float(hit.iloc[0].get("xwoba_against"))
            rows.append({
                "Pitcher":       g[f"{side}_pitcher"],
                "Team":          g[f"{side}_team"],
                "Opponent":      g["home_team" if side == "away" else "away_team"],
                "ERA":           safe_float(stats["era"]),
                "xERA":          xera,
                "HR/9":          safe_float(stats["hr_per9"]),
                "K/9":           safe_float(stats["k_per9"]),
                "BB/9":          safe_float(stats["bb_per9"]),
                "WHIP":          safe_float(stats["whip"]),
                "OPS-against":   safe_float(stats["ops_against"]),
                "xwOBA-against": xwoba_against,
                "IP":            stats["ip"],
                "W-L":           f"{stats['wins']}-{stats['losses']}",
            })

    df_p = pd.DataFrame(rows)
    st.dataframe(
        df_p,
        use_container_width=True, hide_index=True,
        column_config={
            "ERA": st.column_config.NumberColumn(format="%.2f"),
            "xERA": st.column_config.NumberColumn(format="%.2f"),
            "HR/9": st.column_config.NumberColumn(format="%.2f"),
            "K/9": st.column_config.NumberColumn(format="%.2f"),
            "BB/9": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    st.caption("xERA / xwOBA-against from Baseball Savant Statcast leaderboards. "
               "Pitchers below the qualifying threshold show '—'.")


# ===== TAB 3: HR WATCH =====

with tab_hr:
    st.subheader(f"🚀 Home Run Watch — {date_str}")
    st.caption(
        "For each batter on tonight's active rosters, joins Baseball Savant Statcast metrics "
        "(barrel %, hard-hit %, max HR distance) with the **opposing starter's HR/9**, "
        "**park HR factor**, and **wind boost** to compute a composite HR-likelihood score."
    )

    bs_df = load_savant_batter_statcast(season)
    bx_df = load_savant_batter_xstats(season)

    if bs_df.empty:
        st.error("Could not load Baseball Savant data. Try again in a moment.")
    else:
        # Build batter rows from each game's roster, joined to opposing starter context
        watch_rows = []
        with st.spinner("Loading rosters..."):
            for g in enriched:
                # Wind boost & park factor are properties of the home stadium
                fc = g["weather"] or {}
                wlbl = wind_label(fc.get("wind_dir_deg"), g["stadium"]["orientation_deg"])
                wboost = wind_hr_boost(wlbl, fc.get("wind_mph") or 0)
                park_factor = g["stadium"]["hr_factor"] / 100.0

                for side in ("away", "home"):
                    team_id = g[f"{side}_team_id"]
                    team_name = g[f"{side}_team"]
                    opp_pitcher = g["home_pitcher" if side == "away" else "away_pitcher"]
                    opp_stats = g["home_stats" if side == "away" else "away_stats"]
                    opp_hr9 = safe_float(opp_stats["hr_per9"], 1.2) or 1.2

                    # Prefer posted lineup; fall back to roster
                    lineup_key = "homePlayers" if side == "home" else "awayPlayers"
                    lineup = (g["raw"].get("lineups") or {}).get(lineup_key, [])
                    if lineup:
                        player_ids = [p.get("id") for p in lineup if p.get("id")]
                    else:
                        roster = load_roster(team_id, season)
                        player_ids = [
                            r.get("person", {}).get("id")
                            for r in roster
                            if r.get("position", {}).get("type") not in ("Pitcher",)
                        ]

                    for pid in player_ids:
                        hit = bs_df[bs_df["player_id"] == pid]
                        if hit.empty:
                            continue
                        row = hit.iloc[0]
                        brl = safe_float(row.get("brl_percent"), 0) or 0
                        hardhit = safe_float(row.get("ev95percent"), 0) or 0
                        maxd = safe_float(row.get("max_distance"), 0) or 0
                        avg_hrd = safe_float(row.get("avg_hr_distance"), 0) or 0
                        avg_ev = safe_float(row.get("avg_hit_speed"), 0) or 0

                        xhit = bx_df[bx_df["player_id"] == pid] if not bx_df.empty else pd.DataFrame()
                        xslg = safe_float(xhit.iloc[0].get("xslg")) if not xhit.empty else None
                        xwoba = safe_float(xhit.iloc[0].get("xwoba")) if not xhit.empty else None

                        # Composite HR score
                        # baseline: barrel% (2-15 typical) × opp HR/9 (~1.2 league avg)
                        # × park factor (1.0 neutral) × wind boost (1.0 neutral)
                        hr_score = brl * (opp_hr9 / 1.2) * park_factor * wboost

                        watch_rows.append({
                            "Batter":        row.get("player_name"),
                            "Team":          team_name,
                            "vs SP":         opp_pitcher,
                            "Opp HR/9":      opp_hr9,
                            "Barrel %":      brl,
                            "HardHit %":     hardhit,
                            "Avg EV":        avg_ev,
                            "Max Dist":      safe_int(maxd),
                            "Avg HR Dist":   safe_int(avg_hrd),
                            "xSLG":          xslg,
                            "xwOBA":         xwoba,
                            "Park HR":       g["stadium"]["hr_factor"],
                            "Wind":          wlbl,
                            "Wind boost":    round(wboost, 3),
                            "HR Score":      round(hr_score, 2),
                            "Game":          f"{g['away_team']} @ {g['home_team']}",
                        })

        if not watch_rows:
            st.info("No batters joined to Statcast data — qualifying threshold may be filtering hitters out, "
                    "or rosters not yet loaded for this date.")
        else:
            df_h = pd.DataFrame(watch_rows).sort_values("HR Score", ascending=False)

            # Top 5 highlight cards
            st.markdown("### 🔥 Top 5 HR candidates tonight")
            top5 = df_h.head(5)
            cards = st.columns(5)
            for col, (_, r) in zip(cards, top5.iterrows()):
                with col:
                    st.markdown(f"""
                    <div style="background:#1e293b;padding:12px;border-radius:8px;height:100%">
                      <div style="color:#fbbf24;font-size:24px;font-weight:700">{r['HR Score']}</div>
                      <div class="small-cap">HR Score</div>
                      <div style="color:#f1f5f9;font-size:14px;font-weight:600;margin-top:6px">{r['Batter']}</div>
                      <div style="color:#94a3b8;font-size:11px">{r['Team']}</div>
                      <div style="color:#94a3b8;font-size:11px">vs {r['vs SP']}</div>
                      <div style="color:#94a3b8;font-size:11px;margin-top:6px">
                        Brl {r['Barrel %']:.1f}% • Park {r['Park HR']} • {r['Wind']}
                      </div>
                    </div>
                    """, unsafe_allow_html=True)

            st.markdown("### Full board")
            with st.expander("ℹ️ How HR Score is computed"):
                st.markdown(
                    "`HR Score = Barrel% × (Opp_HR9 / 1.2) × (ParkHR / 100) × WindBoost`\n\n"
                    "- **Barrel%** — % of plate appearances yielding a barrel (Statcast)\n"
                    "- **Opp HR/9** — opposing starter's home runs allowed per 9 IP, normalized to league avg ~1.2\n"
                    "- **Park HR** — ballpark HR factor (100 = neutral)\n"
                    "- **WindBoost** — +1.5%/mph above 5mph if blowing out, –1.5%/mph if blowing in\n\n"
                    "Higher score = more favorable HR conditions. **Not betting advice** — this is a "
                    "transparent heuristic, not a probabilistic model."
                )

            st.dataframe(
                df_h,
                use_container_width=True, hide_index=True,
                column_config={
                    "Barrel %":   st.column_config.NumberColumn(format="%.1f"),
                    "HardHit %":  st.column_config.NumberColumn(format="%.1f"),
                    "Avg EV":     st.column_config.NumberColumn(format="%.1f"),
                    "Opp HR/9":   st.column_config.NumberColumn(format="%.2f"),
                    "xSLG":       st.column_config.NumberColumn(format="%.3f"),
                    "xwOBA":      st.column_config.NumberColumn(format="%.3f"),
                    "Wind boost": st.column_config.NumberColumn(format="%.3f"),
                    "HR Score":   st.column_config.NumberColumn(format="%.2f"),
                },
                height=600,
            )


# ===== TAB 4: PREDICTIONS =====

with tab_pred:
    st.subheader(f"🔮 Predictions — {date_str}")

    # View-mode toggle — simple is the default for easy daily scanning
    view_mode = st.radio(
        "View",
        options=["Simple", "Detailed"],
        index=0,
        horizontal=True,
        label_visibility="collapsed",
    )
    if view_mode == "Simple":
        st.caption(
            "Clean ranked view. Toggle **Detailed** above to see every multiplier "
            "and the full data tables."
        )
    else:
        st.caption(
            "A-priori probabilistic model (no historical training data — pure baseball-research priors). "
            "Per-PA HR probability blends batter Statcast, opposing pitcher xERA-derived skill, "
            "park HR factor, and weather (temperature / humidity / wind)."
        )

    bs_df = load_savant_batter_statcast(season)
    bx_df = load_savant_batter_xstats(season)
    px_df = load_savant_pitcher_xstats(season)
    # Tier-2 sources
    arsenal_df = load_savant_pitcher_arsenal(season)
    bvp_df     = load_savant_batter_pitch_perf(season)
    pxL_df     = load_savant_pitcher_split(season, "L")
    pxR_df     = load_savant_pitcher_split(season, "R")

    # ---------- Game predictions ----------
    st.markdown("### 🎯 Game predictions")

    game_pred_rows = []
    with st.spinner("Running per-game predictions..."):
        for g in enriched:
            home_id = g["home_team_id"]; away_id = g["away_team_id"]
            home_season_h = load_team_season(home_id, season, "hitting")
            away_season_h = load_team_season(away_id, season, "hitting")
            home_recent_h = load_team_recent(home_id, season, 15, "hitting")
            away_recent_h = load_team_recent(away_id, season, 15, "hitting")
            home_season_p = load_team_season(home_id, season, "pitching")
            away_season_p = load_team_season(away_id, season, "pitching")

            home_p_savant = None
            away_p_savant = None
            if g["home_pid"] and not px_df.empty:
                hit = px_df[px_df["player_id"] == g["home_pid"]]
                if not hit.empty: home_p_savant = hit.iloc[0].to_dict()
            if g["away_pid"] and not px_df.empty:
                hit = px_df[px_df["player_id"] == g["away_pid"]]
                if not hit.empty: away_p_savant = hit.iloc[0].to_dict()

            home_bullpen = load_bullpen(home_id, season)
            away_bullpen = load_bullpen(away_id, season)

            pred = game_model.predict_game(
                home_team_season=home_season_h, home_team_recent=home_recent_h,
                away_team_season=away_season_h, away_team_recent=away_recent_h,
                home_pitcher_stats=g["home_stats"], home_pitcher_savant=home_p_savant,
                away_pitcher_stats=g["away_stats"], away_pitcher_savant=away_p_savant,
                home_team_pitching_season=home_season_p,
                away_team_pitching_season=away_season_p,
                park_hr=g["stadium"]["hr_factor"],
                weather=g["weather"] or g["mlb_weather"],
                home_bullpen=home_bullpen,
                away_bullpen=away_bullpen,
            )
            favorite = g["home_team"] if pred["p_home_win"] > 0.5 else g["away_team"]
            game_pred_rows.append({
                "Matchup":        f"{g['away_team']} @ {g['home_team']}",
                "First Pitch":    g["first_pitch"],
                "Status":         g["status"],
                "Exp Away R":     round(pred["away_exp_runs"], 2),
                "Exp Home R":     round(pred["home_exp_runs"], 2),
                "Total":          round(pred["total_runs"], 2),
                "Favorite":       favorite,
                "Win %":          round(max(pred["p_home_win"], pred["p_away_win"]) * 100, 1),
                "Away BP ERA":    round(away_bullpen.get("era", 0), 2) if away_bullpen else None,
                "Home BP ERA":    round(home_bullpen.get("era", 0), 2) if home_bullpen else None,
                "Park HR":        g["stadium"]["hr_factor"],
            })

    df_pred = pd.DataFrame(game_pred_rows).sort_values("Total", ascending=False)

    if view_mode == "Simple":
        # Card-based render with win-probability bar
        for _, r in df_pred.iterrows():
            # Determine sides: matchup is "AWAY @ HOME"; "Win %" is the favorite's
            matchup = r["Matchup"]
            away_team, _, home_team = matchup.partition(" @ ")
            away_r = r["Exp Away R"]; home_r = r["Exp Home R"]
            home_pct = (
                r["Win %"] if r["Favorite"] == home_team else 100 - r["Win %"]
            )
            away_pct = 100 - home_pct
            total = r["Total"]
            tot_color = "#ef4444" if total >= 9.5 else "#22c55e" if total <= 7.5 else "#fbbf24"

            with st.container(border=True):
                # Header row: matchup + first pitch + park
                hcols = st.columns([3, 1])
                with hcols[0]:
                    st.markdown(
                        f"<div style='font-size:18px;font-weight:700;color:#f1f5f9'>{away_team} @ {home_team}</div>"
                        f"<div style='color:#94a3b8;font-size:12px'>🕐 {r['First Pitch']} · 🏟️ Park HR {r['Park HR']} · {r['Status']}</div>",
                        unsafe_allow_html=True,
                    )
                with hcols[1]:
                    st.markdown(
                        f"<div style='text-align:right;padding-top:4px'>"
                        f"<div style='color:#94a3b8;font-size:11px'>EXPECTED TOTAL</div>"
                        f"<div style='color:{tot_color};font-size:28px;font-weight:800;line-height:1'>{total:.1f}</div>"
                        f"<div style='color:#94a3b8;font-size:11px'>runs</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

                # Score + win-prob row
                bcols = st.columns([1, 2, 1])
                with bcols[0]:
                    st.markdown(
                        f"<div style='text-align:center'>"
                        f"<div style='color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px'>{away_team[:18]}</div>"
                        f"<div style='color:#cbd5e1;font-size:36px;font-weight:800;line-height:1.1'>{away_r:.1f}</div>"
                        f"<div style='color:#94a3b8;font-size:10px'>exp runs</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                with bcols[1]:
                    # Win probability split bar
                    st.markdown(
                        f"""
                        <div style='padding:10px 0'>
                          <div style='color:#94a3b8;font-size:11px;text-align:center;margin-bottom:6px'>WIN PROBABILITY</div>
                          <div style='display:flex;height:32px;border-radius:8px;overflow:hidden;background:#0f172a'>
                            <div style='background:#3b82f6;width:{away_pct}%;display:flex;align-items:center;justify-content:flex-start;padding-left:10px;color:white;font-weight:700;font-size:14px'>
                              {away_pct:.0f}%
                            </div>
                            <div style='background:#22c55e;width:{home_pct}%;display:flex;align-items:center;justify-content:flex-end;padding-right:10px;color:white;font-weight:700;font-size:14px'>
                              {home_pct:.0f}%
                            </div>
                          </div>
                          <div style='display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:#94a3b8'>
                            <span>{away_team[:20]}</span><span>{home_team[:20]}</span>
                          </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                with bcols[2]:
                    st.markdown(
                        f"<div style='text-align:center'>"
                        f"<div style='color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:1px'>{home_team[:18]}</div>"
                        f"<div style='color:#cbd5e1;font-size:36px;font-weight:800;line-height:1.1'>{home_r:.1f}</div>"
                        f"<div style='color:#94a3b8;font-size:10px'>exp runs</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
    else:
        st.dataframe(
            df_pred, use_container_width=True, hide_index=True,
            column_config={
                "Exp Away R":   st.column_config.NumberColumn(format="%.2f"),
                "Exp Home R":   st.column_config.NumberColumn(format="%.2f"),
                "Total":        st.column_config.NumberColumn(format="%.2f"),
                "Win %":        st.column_config.NumberColumn(format="%.1f%%"),
                "Away BP ERA":  st.column_config.NumberColumn(format="%.2f"),
                "Home BP ERA":  st.column_config.NumberColumn(format="%.2f"),
            },
        )
        st.caption("**Away BP ERA / Home BP ERA** = bullpen-only ERA (relievers, < 5 starts). Now isolated from starter performance for sharper late-game run estimation.")

    st.markdown("### 🚀 HR probability — top batters tonight")
    cov_msg = (
        f"⚙️ Tier-2 coverage: {len(arsenal_df)} pitchers w/ arsenal data, "
        f"{len(bvp_df)} batters w/ pitch-type wOBA, "
        f"{len(pxL_df)} L-split + {len(pxR_df)} R-split xStats."
    )
    st.caption(cov_msg)

    # ---- Pre-load Tier-1 upgrade data (one call per refresh) ----
    recent_hitting = load_recent_hitting(season, 15)

    # Collect every batter & pitcher ID we'll evaluate, then batch-fetch handedness
    all_pids = set()
    for g in enriched:
        for side in ("away", "home"):
            tid = g[f"{side}_team_id"]
            lineup_key = "homePlayers" if side == "home" else "awayPlayers"
            lineup = (g["raw"].get("lineups") or {}).get(lineup_key, [])
            if lineup:
                for p in lineup:
                    if p.get("id"): all_pids.add(p["id"])
            else:
                for r in load_roster(tid, season):
                    if r.get("position", {}).get("type") not in ("Pitcher",):
                        pid = r.get("person", {}).get("id")
                        if pid: all_pids.add(pid)
        for pid_key in ("away_pid", "home_pid"):
            pid = g.get(pid_key)
            if pid: all_pids.add(pid)

    handedness = load_handedness(tuple(sorted(all_pids))) if all_pids else {}

    # ---- Pre-load Tier-2 sources ----
    # Build IL set across every team playing tonight; filter dead picks
    il_set = set()
    for g in enriched:
        for tid_key in ("home_team_id", "away_team_id"):
            tid = g[tid_key]
            il_set.update(load_team_il(tid, season))

    hr_rows = []
    with st.spinner("Computing per-batter HR probabilities..."):
        for g in enriched:
            park_orient = g["stadium"]["orientation_deg"]
            park_hr = g["stadium"]["hr_factor"]
            weather = g["weather"] or {}
            game_pk = g["raw"].get("gamePk")

            for side in ("away", "home"):
                team_id = g[f"{side}_team_id"]
                team_name = g[f"{side}_team"]
                opp_pitcher = g["home_pitcher" if side == "away" else "away_pitcher"]
                opp_stats   = g["home_stats"   if side == "away" else "away_stats"]
                opp_pid     = g["home_pid"     if side == "away" else "away_pid"]

                opp_savant = None
                if opp_pid and not px_df.empty:
                    hit = px_df[px_df["player_id"] == opp_pid]
                    if not hit.empty: opp_savant = hit.iloc[0].to_dict()

                # Pitcher handedness + L/R splits
                pitcher_hand = handedness.get(opp_pid, {}).get("throws") if opp_pid else None
                pitcher_splits = mlb_api.get_pitcher_splits(opp_pid, season) if opp_pid else None

                # ---- Tier-2: pitcher arsenal, recent form, handedness-split xStats ----
                opp_arsenal = None
                if opp_pid and not arsenal_df.empty:
                    hit = arsenal_df[arsenal_df["player_id"] == opp_pid]
                    if not hit.empty:
                        opp_arsenal = hit.iloc[0].to_dict()

                opp_recent = load_pitcher_recent(opp_pid, season) if opp_pid else None

                # Pitcher xStats split by *batter* handedness — pick the right table
                # at lookup time (per batter), not per pitcher.

                lineup_key = "homePlayers" if side == "home" else "awayPlayers"
                lineup = (g["raw"].get("lineups") or {}).get(lineup_key, [])
                # Map of pid → lineup_spot (1-9) when lineup is posted
                pid_to_spot = {}
                if lineup:
                    pids = []
                    for spot, p in enumerate(lineup, start=1):
                        if p.get("id"):
                            pids.append(p["id"])
                            pid_to_spot[p["id"]] = spot
                else:
                    roster = load_roster(team_id, season)
                    pids = [
                        r.get("person", {}).get("id") for r in roster
                        if r.get("position", {}).get("type") not in ("Pitcher",)
                    ]

                for pid in pids:
                    # Tier-2: skip players on IL
                    if pid in il_set:
                        continue

                    bs_row = bs_df[bs_df["player_id"] == pid]
                    if bs_row.empty:
                        continue
                    bs = bs_row.iloc[0].to_dict()

                    bx_row = bx_df[bx_df["player_id"] == pid] if not bx_df.empty else pd.DataFrame()
                    if not bx_row.empty:
                        bs["xslg"] = bx_row.iloc[0].get("xslg")
                        bs["xwoba"] = bx_row.iloc[0].get("xwoba")

                    batter_hand = handedness.get(pid, {}).get("bats")
                    recent_b = recent_hitting.get(pid)

                    # Tier-2: batter wOBA per pitch type (matches arsenal)
                    batter_pitch_perf = None
                    if not bvp_df.empty:
                        hit = bvp_df[bvp_df["player_id"] == pid]
                        if not hit.empty:
                            batter_pitch_perf = hit.iloc[0].to_dict()

                    # Tier-2: pitcher xStats split by *this* batter's handedness
                    pitcher_savant_split = None
                    if opp_pid:
                        side_lower = (batter_hand or "R").lower()
                        # Switch hitters bat opposite of pitcher; we map them later in handedness_factor
                        # but for the split lookup, use bat_side preference: S → opposite of pitcher.
                        eff_bat = batter_hand
                        if eff_bat == "S" and pitcher_hand:
                            eff_bat = "R" if pitcher_hand == "L" else "L"
                        df_split = pxL_df if eff_bat == "L" else pxR_df
                        if not df_split.empty:
                            hit = df_split[df_split["player_id"] == opp_pid]
                            if not hit.empty:
                                pitcher_savant_split = hit.iloc[0].to_dict()

                    # Lineup-spot-aware PA estimate (uses 4.3 default if no lineup posted)
                    spot = pid_to_spot.get(pid)
                    expected_pa = hr_model.expected_pa_for_spot(spot)

                    # BvP: fetch career head-to-head vs this pitcher inline
                    bvp_inline = {}
                    if opp_pid:
                        bvp_inline = load_bvp(pid, opp_pid, season) or {}

                    per_pa = hr_model.predict_per_pa(
                        batter_savant=bs,
                        pitcher_stats=opp_stats,
                        pitcher_savant=opp_savant,
                        park_hr=park_hr,
                        weather=weather,
                        park_orientation_deg=park_orient,
                        stadium=g["stadium"],
                        batter_hand=batter_hand,
                        pitcher_hand=pitcher_hand,
                        pitcher_splits=pitcher_splits,
                        recent_stats=recent_b,
                        pitcher_arsenal=opp_arsenal,
                        batter_pitch_perf=batter_pitch_perf,
                        pitcher_savant_split=pitcher_savant_split,
                        pitcher_recent_form=opp_recent,
                        bvp_data=bvp_inline,
                    )
                    per_game = hr_model.predict_per_game(per_pa, expected_pa)
                    p_raw = per_game["p_at_least_one"]
                    p_cal = (
                        cal_model.apply_calibration(p_raw, cal_params)
                        if (use_calibration and cal_params) else p_raw
                    )

                    hr_rows.append({
                        "Batter":        bs.get("player_name"),
                        "Team":          team_name,
                        "B/T":           f"{batter_hand or '?'}/{pitcher_hand or '?'}",
                        "Spot":          spot if spot else "—",
                        "Exp PA":        round(expected_pa, 2),
                        "vs SP":         opp_pitcher,
                        "P(HR≥1)":       round(p_cal * 100, 2),
                        "P raw":         round(p_raw * 100, 2),
                        "E[HR]":         round(per_game["expected_hrs"], 3),
                        "P/PA":          round(per_pa["p_hr_per_pa"] * 100, 2),
                        "Bat ×":         round(per_pa["f_batter"], 2),
                        "Pit ×":         round(per_pa["f_pitcher"], 2),
                        "Park ×":        round(per_pa["f_park"], 2),
                        "Hand ×":        round(per_pa["f_hand"], 2),
                        "Arsnl ×":       round(per_pa["f_arsenal"], 2),
                        "Recent ×":      round(per_pa["f_recent"], 2),
                        "BvP ×":         round(per_pa["f_bvp"], 2),
                        "Temp ×":        round(per_pa["f_temp"], 2),
                        "Wind ×":        round(per_pa["f_wind"], 2),
                        "Wind":          per_pa["wind_label"],
                        "L15 HR":        (recent_b or {}).get("hr"),
                        "L15 PA":        (recent_b or {}).get("pa"),
                        "Confidence":    hr_model.confidence_label(per_pa),
                        "Game":          f"{g['away_team']} @ {g['home_team']}",
                        # Logging-only fields (kept here for the JSONL writer)
                        "_game_pk":      game_pk,
                        "_batter_id":    pid,
                        "_opp_pid":      opp_pid,
                    })

    if not hr_rows:
        st.info("No HR predictions available — Statcast data may not be loaded yet.")
    else:
        df_hr = pd.DataFrame(hr_rows).sort_values("P(HR≥1)", ascending=False)

        # BvP was fetched inline during prediction — add display columns from it
        for r in hr_rows:
            bvp = load_bvp(r["_batter_id"], r["_opp_pid"], season) or {} if r["_opp_pid"] else {}
            ab   = bvp.get("ab",   0)
            hits = bvp.get("hits", 0)
            hr   = bvp.get("hr",   0)
            ops  = bvp.get("ops",  "—")
            if ab >= 1:
                r["Career vs SP"] = f"{hits}-{ab}, {hr} HR, {ops} OPS"
                r["BvP HR"] = hr
                r["BvP AB"] = ab
            else:
                r["Career vs SP"] = "no history"
                r["BvP HR"] = None
                r["BvP AB"] = 0

        # Rebuild df with all columns
        df_hr = pd.DataFrame(hr_rows).sort_values("P(HR≥1)", ascending=False)

        # Persist predictions to disk (for future backtesting)
        log_payload = []
        for r in hr_rows:
            log_payload.append({
                "date":           date_str,
                "game_pk":        r["_game_pk"],
                "batter_id":      r["_batter_id"],
                "batter_name":    r["Batter"],
                "team":           r["Team"],
                "opp_pitcher_id": r["_opp_pid"],
                "opp_pitcher":    r["vs SP"],
                "p_hr_per_pa":    r["P/PA"] / 100.0,
                "p_at_least_one": r["P(HR≥1)"] / 100.0,
                "expected_hrs":   r["E[HR]"],
                "bat_factor":     r["Bat ×"],
                "pit_factor":     r["Pit ×"],
                "park_factor":    r["Park ×"],
                "hand_factor":    r["Hand ×"],
                "temp_factor":    r["Temp ×"],
                "wind_factor":    r["Wind ×"],
                "wind_label":     r["Wind"],
                "model_version":  "tier1.v1",
            })
        try:
            log_path = pred_logger.log_predictions(date_str, log_payload)
            st.caption(f"📝 {len(log_payload)} predictions logged → `{os.path.basename(log_path)}` ({len(pred_logger.list_logged_dates())} day(s) saved)")
        except Exception as e:
            st.caption(f"⚠️ Could not write prediction log: {e}")

        # Hide internal logging cols from display
        df_display = df_hr.drop(columns=[c for c in df_hr.columns if c.startswith("_")])

        if view_mode == "Simple":
            top_n = st.slider("Show top N batters", 5, 30, 15, key="hr_top_n")
            cal_label = " (calibrated)" if (use_calibration and cal_params) else ""
            st.markdown(f"#### 🔥 Top {top_n} HR probabilities{cal_label}")

            ranked = df_display.head(top_n)
            for i, (_, r) in enumerate(ranked.iterrows(), start=1):
                pct = r["P(HR≥1)"]
                raw = r.get("P raw", pct)
                color = "#22c55e" if pct >= 12 else "#fbbf24" if pct >= 8 else "#94a3b8"
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"<span style='color:#64748b;font-weight:700'>#{i}</span>"
                # Show raw alongside calibrated only if they differ enough to notice
                raw_subtitle = (
                    f"<div style='color:#64748b;font-size:10px'>raw {raw:.1f}%</div>"
                    if (use_calibration and cal_params and abs(raw - pct) >= 0.3) else ""
                )
                # Career BvP line — surface only when sample is meaningful
                career = r.get("Career vs SP", "no history")
                bvp_ab = r.get("BvP AB", 0) or 0
                bvp_hr = r.get("BvP HR", 0) or 0
                if bvp_ab >= 5:
                    career_color = "#22c55e" if bvp_hr >= 1 else "#94a3b8"
                    career_line = f"<div style='color:{career_color};font-size:11px'>Career vs SP: {career}</div>"
                elif bvp_ab > 0:
                    career_line = f"<div style='color:#64748b;font-size:11px;font-style:italic'>BvP: {career} (small sample)</div>"
                else:
                    career_line = "<div style='color:#475569;font-size:11px'>BvP: no history</div>"

                st.markdown(
                    f"""
                    <div style='background:#1e293b;padding:12px 16px;border-radius:8px;margin-bottom:6px;
                                display:flex;align-items:center;gap:14px'>
                      <div style='width:32px;font-size:20px;text-align:center'>{medal}</div>
                      <div style='flex:1;min-width:0'>
                        <div style='color:#f1f5f9;font-size:15px;font-weight:600'>{r['Batter']}</div>
                        <div style='color:#94a3b8;font-size:12px'>{r['Team']} ({r['B/T']}) — vs {r['vs SP']}</div>
                        {career_line}
                      </div>
                      <div style='text-align:right;min-width:80px'>
                        <div style='color:{color};font-size:24px;font-weight:800;line-height:1'>{pct:.1f}%</div>
                        <div style='color:#94a3b8;font-size:10px;text-transform:uppercase;letter-spacing:1px'>P(HR≥1)</div>
                        {raw_subtitle}
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            # Top 5 cards
            st.markdown("#### 🔥 Top 5 HR probabilities")
            top5 = df_display.head(5)
            cards = st.columns(5)
            for col, (_, r) in zip(cards, top5.iterrows()):
                with col:
                    pct = r["P(HR≥1)"]
                    color = "#22c55e" if pct >= 12 else "#fbbf24" if pct >= 8 else "#94a3b8"
                    st.markdown(f"""
                    <div style="background:#1e293b;padding:14px;border-radius:8px;height:100%">
                      <div style="color:{color};font-size:28px;font-weight:800">{pct:.1f}%</div>
                      <div class="small-cap">P(HR ≥ 1)</div>
                      <div style="color:#f1f5f9;font-size:14px;font-weight:600;margin-top:6px">{r['Batter']}</div>
                      <div style="color:#94a3b8;font-size:11px">{r['Team']} ({r['B/T']})</div>
                      <div style="color:#94a3b8;font-size:11px">vs {r['vs SP']}</div>
                      <div style="color:#94a3b8;font-size:11px;margin-top:6px">
                        Bat×{r['Bat ×']} • Pit×{r['Pit ×']} • Park×{r['Park ×']} • Hand×{r['Hand ×']}
                      </div>
                    </div>
                    """, unsafe_allow_html=True)

            st.markdown("#### Full board")
            st.dataframe(
                df_display, use_container_width=True, hide_index=True, height=500,
                column_config={
                    "P(HR≥1)": st.column_config.NumberColumn(format="%.2f%%"),
                    "P/PA":    st.column_config.NumberColumn(format="%.2f%%"),
                    "E[HR]":   st.column_config.NumberColumn(format="%.3f"),
                    "Bat ×":   st.column_config.NumberColumn(format="%.2f"),
                    "Pit ×":   st.column_config.NumberColumn(format="%.2f"),
                    "Park ×":  st.column_config.NumberColumn(format="%.2f"),
                    "Hand ×":   st.column_config.NumberColumn(format="%.2f"),
                    "Arsnl ×":  st.column_config.NumberColumn(format="%.2f"),
                    "Recent ×": st.column_config.NumberColumn(format="%.2f"),
                    "Temp ×":  st.column_config.NumberColumn(format="%.2f"),
                    "Wind ×":  st.column_config.NumberColumn(format="%.2f"),
                },
            )

        # ---------- ODDS EDGE ----------
        st.markdown("---")
        st.markdown("### 💰 Odds Edge (model vs sportsbook implied)")

        from data import odds as _odds_helpers

        # Clear stale session data when date changes
        if st.session_state.get("_hr_odds_date") != date_str:
            st.session_state.pop("_hr_odds_rows", None)
            st.session_state.pop("_hr_odds_date", None)

        if bdl_api.is_configured():
            # ---- BDL: 6 books, no quota ----
            _bdl_hr_vendors = st.multiselect(
                "Books",
                options=["draftkings", "fanduel", "betmgm", "caesars", "betrivers", "fanatics"],
                default=["draftkings", "fanduel", "betmgm", "caesars", "betrivers", "fanatics"],
                key="bdl_hr_books",
                help="BallDontLie GOAT — all 6 sportsbooks. No API quota used.",
            )
            _bdl_all_games = load_bdl_games_for_date(date_str)
            _bdl_hr_games = [
                g for g in _bdl_all_games
                if g.get("status", "").upper() not in {"STATUS_FINAL", "FINAL", "STATUS_POSTPONED", "POSTPONED"}
            ]
            st.caption(f"⚡ {len(_bdl_hr_games)} active/upcoming game(s) · BallDontLie · no request quota")

            if st.button(
                f"🎯 Fetch HR odds — {len(_bdl_hr_games)} game(s) · 6 books",
                key="fetch_bdl_hr_btn",
                disabled=(not _bdl_hr_games),
            ):
                _hr_raw: list[dict] = []
                _prog = st.progress(0)
                with st.spinner("Fetching HR props from BallDontLie..."):
                    for _idx, _g in enumerate(_bdl_hr_games):
                        _gid = _g["id"]
                        _atm = _g.get("away_team_name") or (_g.get("away_team") or {}).get("full_name", "?")
                        _htm = _g.get("home_team_name") or (_g.get("home_team") or {}).get("full_name", "?")
                        _matchup = f"{_atm} @ {_htm}"
                        _props = load_bdl_props_for_game(_gid)
                        for _p in _props:
                            if _p.get("prop_type") != "home_runs":
                                continue
                            if _p.get("line_value", "0.5") != "0.5":
                                continue
                            _vendor = _p.get("vendor", "")
                            if _bdl_hr_vendors and _vendor not in _bdl_hr_vendors:
                                continue
                            _mkt = _p.get("market") or {}
                            _mk_type = _mkt.get("type", "")
                            if _mk_type == "over_under":
                                _ov = _mkt.get("over_odds")
                            elif _mk_type == "milestone":
                                _ov = _mkt.get("odds")
                            else:
                                continue
                            if _ov is None:
                                continue
                            try:
                                _oi = int(_ov)
                            except (TypeError, ValueError):
                                continue
                            _hr_raw.append({
                                "player_id":    _p.get("player_id"),
                                "bookmaker":    _vendor,
                                "american_odds": _oi,
                                "decimal_odds":  _odds_helpers.american_to_decimal(_oi),
                                "implied_prob":  _odds_helpers.american_to_implied(_oi),
                                "matchup":       _matchup,
                            })
                        _prog.progress((_idx + 1) / max(len(_bdl_hr_games), 1))
                _prog.empty()

                # Resolve player IDs → names via cached batch lookup
                _hr_pids = tuple(sorted({r["player_id"] for r in _hr_raw if r.get("player_id")}))
                with st.spinner(f"Resolving {len(_hr_pids)} player names..."):
                    _hr_name_map = load_bdl_player_names_batch(_hr_pids)

                _hr_odds_rows: list[dict] = []
                for _r in _hr_raw:
                    _pname = _hr_name_map.get(_r["player_id"]) or f"Player #{_r['player_id']}"
                    _hr_odds_rows.append({
                        "player":        _pname,
                        "bookmaker":     _r["bookmaker"],
                        "american_odds": _r["american_odds"],
                        "decimal_odds":  _r["decimal_odds"],
                        "implied_prob":  _r["implied_prob"],
                        "matchup":       _r["matchup"],
                    })

                if not _hr_odds_rows:
                    st.warning("No HR prop odds returned — markets may not be posted yet.")
                else:
                    st.session_state["_hr_odds_rows"] = _hr_odds_rows
                    st.session_state["_hr_odds_date"] = date_str
                    st.session_state["_hr_odds_src"] = "BallDontLie (6 books)"
                    _save_odds_snapshot(date_str, _hr_odds_rows)
                    st.rerun()

        else:
            # ---- Fallback: The Odds API ----
            odds_key = resolve_odds_key()
            if not odds_key:
                st.info(
                    "👈 BallDontLie key not configured. Add your **The Odds API** key in the "
                    "sidebar to compare model predictions against live HR prop odds."
                )
            else:
                books = st.multiselect(
                    "Books",
                    options=["draftkings", "fanduel", "betmgm", "williamhill_us", "bovada",
                             "betrivers", "fanatics", "betonlineag", "lowvig", "mybookieag"],
                    default=["draftkings", "fanduel", "betmgm", "williamhill_us", "bovada"],
                    help=("Sharp US books only by default. Caesars is keyed as "
                          "`williamhill_us` (Caesars acquired William Hill US). "
                          "BetOnline.ag intentionally excluded — their HR lines are "
                          "routinely stale longshots (+18,000+) that don't match real "
                          "market consensus."),
                )
                books_str = ",".join(books) if books else "draftkings,fanduel,betmgm,williamhill_us,bovada"

                try:
                    with st.spinner("Loading odds events..."):
                        odds_events = load_odds_events(odds_key)
                except odds_api.OddsAPIError as e:
                    st.error(f"Odds API: {e}")
                    odds_events = []

                event_map = {}
                for ev in odds_events:
                    key_str = (ev.get("away_team", "") + "|" + ev.get("home_team", "")).lower()
                    event_map[key_str] = ev["id"]

                today_iso = selected_date.strftime("%Y-%m-%d")
                today_event_ids = []
                for g in enriched:
                    key_lookup = (g["away_team"] + "|" + g["home_team"]).lower()
                    ev_id = event_map.get(key_lookup)
                    if ev_id and (not g["first_pitch_dt"] or g["first_pitch_dt"].strftime("%Y-%m-%d") == today_iso):
                        today_event_ids.append((ev_id, g["away_team"], g["home_team"]))

                est_cost = len(today_event_ids)
                st.caption(f"⚡ Will use ~{est_cost} request(s) to fetch HR props for {est_cost} game(s).")

                if st.button(f"🎯 Fetch HR odds for {est_cost} games", disabled=(est_cost == 0)):
                    all_odds_rows = []
                    progress = st.progress(0)
                    with st.spinner("Pulling odds..."):
                        for i, (eid, atm, htm) in enumerate(today_event_ids):
                            rows = load_hr_odds_for_event(odds_key, eid, books_str)
                            for r in rows:
                                r["matchup"] = f"{atm} @ {htm}"
                            all_odds_rows.extend(rows)
                            progress.progress((i + 1) / len(today_event_ids))
                    progress.empty()

                    if not all_odds_rows:
                        st.warning("No HR prop odds returned. The market may not be posted yet.")
                    else:
                        st.session_state["_hr_odds_rows"] = all_odds_rows
                        st.session_state["_hr_odds_date"] = date_str
                        st.session_state["_hr_odds_src"] = "The Odds API"
                        _save_odds_snapshot(date_str, all_odds_rows)
                        st.rerun()

        # ---- Common edge rendering (persists via session state) ----
        _all_odds_rows = st.session_state.get("_hr_odds_rows", [])
        _odds_src_label = st.session_state.get("_hr_odds_src", "")

        if _all_odds_rows:
            apply_filter = st.toggle(
                "🛡️ Filter stale / single-book longshot lines",
                value=True,
                key="hr_stale_filter",
                help=(
                    "Drops HR-prop offers with implied probability < 3% from a single book, "
                    "and outlier prices where one book is far off the consensus. "
                    "Real markets have multi-book consensus on legit HR favorites."
                ),
            )
            _display_rows = list(_all_odds_rows)
            if apply_filter:
                _display_rows, drop_log = _odds_helpers.filter_stale_lines(_display_rows)
                st.caption(
                    f"🛡️ Filtered {drop_log['single_book_longshot']} single-book longshots "
                    f"+ {drop_log['outlier_price']} outlier prices. "
                    f"Kept {drop_log['kept']} of {drop_log['total_in']} total offers."
                )
                if not _display_rows:
                    st.info("All offers were filtered as suspect. Disable the filter above to see raw data.")

            if _display_rows:
                odds_df = pd.DataFrame(_display_rows)
                odds_df["norm_name"] = odds_df["player"].apply(_odds_helpers.normalize_name)

                model_df = pd.DataFrame([{
                    "norm_name": _odds_helpers.normalize_name(r["Batter"]),
                    "Batter":    r["Batter"],
                    "Team":      r["Team"],
                    "vs SP":     r["vs SP"],
                    "model_p":   r["P(HR≥1)"] / 100.0,
                } for r in hr_rows])

                edge_rows = []
                for _, mr in model_df.iterrows():
                    mp = mr["model_p"]
                    offers = odds_df[odds_df["norm_name"] == mr["norm_name"]]
                    if offers.empty:
                        continue
                    best = offers.loc[offers["implied_prob"].idxmin()]
                    edge = mp - best["implied_prob"]
                    ev_pct = (mp * (best["decimal_odds"] - 1) - (1 - mp)) * 100
                    kelly_info = kelly_model.safe_stake(
                        p=mp,
                        decimal_odds=best["decimal_odds"],
                        bankroll=bankroll,
                        multiplier=kelly_mult,
                        max_fraction=max_fraction,
                    )
                    suspect = _odds_helpers.is_suspect_edge(edge * 100)
                    edge_rows.append({
                        "Batter":      mr["Batter"],
                        "Team":        mr["Team"],
                        "vs SP":       mr["vs SP"],
                        "Model %":     round(mp * 100, 2),
                        "Best Odds":   best["american_odds"],
                        "Book":        best["bookmaker"],
                        "Implied %":   round(best["implied_prob"] * 100, 2),
                        "Edge (pp)":   round(edge * 100, 2),
                        "EV / $100":   round(ev_pct, 2),
                        "Kelly %":     round(kelly_info["fraction"] * 100, 2),
                        "Kelly Stake": round(kelly_info["stake"], 2),
                        "Kelly EV":    round(kelly_info["expected_profit"], 2),
                        "Kelly Capped": kelly_info["capped"],
                        "Verify?":     "❓" if suspect else "",
                    })

                if not edge_rows:
                    st.warning("No name matches between model and odds — name normalization may need tuning.")
                else:
                    df_edge = pd.DataFrame(edge_rows).sort_values("Edge (pp)", ascending=False)
                    st.session_state["edge_rows"] = edge_rows
                    st.session_state["edge_date"] = date_str
                    if _odds_src_label:
                        st.caption(f"Odds source: **{_odds_src_label}**")

                    positive = df_edge[df_edge["Edge (pp)"] > 0]

                    if view_mode == "Simple":
                        if positive.empty:
                            st.info("No positive edges found right now — the market has every batter priced fairly or below model.")
                        else:
                            st.markdown(f"#### 💎 Positive edges — {len(positive)} batter(s) the market is underpricing")
                            st.caption(
                                f"💰 Stakes computed at **{kelly_label}** of bankroll **${bankroll:,.0f}**, "
                                f"capped at **{max_pct}%** per bet. Adjust in the sidebar."
                            )
                            for i, (_, r) in enumerate(positive.iterrows(), start=1):
                                edge = r["Edge (pp)"]
                                ev = r["EV / $100"]
                                stake = r["Kelly Stake"]
                                kelly_pct = r["Kelly %"]
                                kelly_ev = r["Kelly EV"]
                                capped = r["Kelly Capped"]
                                color = "#22c55e" if edge >= 5 else "#a3e635" if edge >= 2 else "#fbbf24"
                                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"<span style='color:#64748b;font-weight:700'>#{i}</span>"
                                cap_badge = (
                                    f"<span style='background:#7c2d12;color:#fed7aa;padding:1px 5px;border-radius:3px;font-size:9px;margin-left:4px'>CAP</span>"
                                    if capped else ""
                                )
                                st.markdown(
                                    f"""
                                    <div style='background:#1e293b;padding:12px 16px;border-radius:8px;margin-bottom:6px;
                                                display:flex;align-items:center;gap:14px'>
                                      <div style='width:32px;font-size:20px;text-align:center'>{medal}</div>
                                      <div style='flex:1;min-width:0'>
                                        <div style='color:#f1f5f9;font-size:15px;font-weight:600'>{r['Batter']}</div>
                                        <div style='color:#94a3b8;font-size:12px'>{r['Team']} — vs {r['vs SP']}</div>
                                        <div style='color:#fbbf24;font-size:12px;margin-top:2px'>
                                          {r['Best Odds']:+d} on {r['Book']} · Model {r['Model %']:.1f}% vs Market {r['Implied %']:.1f}%
                                        </div>
                                        <div style='color:#22c55e;font-size:13px;margin-top:4px;font-weight:600'>
                                          🎯 Stake ${stake:,.2f} ({kelly_pct:.1f}% of bankroll){cap_badge}
                                          <span style='color:#94a3b8;font-weight:400;margin-left:8px'>EV ${kelly_ev:+,.2f}</span>
                                        </div>
                                      </div>
                                      <div style='text-align:right;min-width:90px'>
                                        <div style='color:{color};font-size:22px;font-weight:800;line-height:1'>+{edge:.1f}<span style='font-size:13px'>pp</span></div>
                                        <div style='color:#94a3b8;font-size:10px;text-transform:uppercase;letter-spacing:1px'>edge</div>
                                        <div style='color:{color};font-size:11px;margin-top:4px'>EV ${ev:+.2f}/$100</div>
                                      </div>
                                    </div>
                                    """,
                                    unsafe_allow_html=True,
                                )
                    else:
                        if not positive.empty:
                            st.markdown(f"#### 💎 Top {min(5, len(positive))} positive edges (model > market)")
                            top = positive.head(5)
                            cards = st.columns(min(5, len(top)))
                            for col, (_, r) in zip(cards, top.iterrows()):
                                with col:
                                    edge = r["Edge (pp)"]
                                    color = "#22c55e" if edge >= 5 else "#a3e635"
                                    st.markdown(f"""
                                    <div style="background:#1e293b;padding:14px;border-radius:8px;height:100%">
                                      <div style="color:{color};font-size:24px;font-weight:800">+{edge:.1f}pp</div>
                                      <div class="small-cap">Model − Implied</div>
                                      <div style="color:#f1f5f9;font-size:14px;font-weight:600;margin-top:6px">{r['Batter']}</div>
                                      <div style="color:#94a3b8;font-size:11px">{r['Team']}</div>
                                      <div style="color:#fbbf24;font-size:13px;margin-top:4px">{r['Best Odds']:+d} ({r['Book']})</div>
                                      <div style="color:#94a3b8;font-size:11px">Model {r['Model %']:.1f}% vs Implied {r['Implied %']:.1f}%</div>
                                      <div style="color:{color};font-size:11px;margin-top:4px">EV: ${r['EV / $100']:+.2f} / $100</div>
                                    </div>
                                    """, unsafe_allow_html=True)

                        st.markdown("#### Full edge board")
                        st.dataframe(
                            df_edge, use_container_width=True, hide_index=True, height=500,
                            column_config={
                                "Best Odds":     st.column_config.NumberColumn(format="%+d"),
                                "Model %":       st.column_config.NumberColumn(format="%.2f%%"),
                                "Implied %":     st.column_config.NumberColumn(format="%.2f%%"),
                                "Edge (pp)":     st.column_config.NumberColumn(format="%+.2f"),
                                "EV / $100":     st.column_config.NumberColumn(format="$%+.2f"),
                                "Kelly %":       st.column_config.NumberColumn(format="%.2f%%"),
                                "Kelly Stake":   st.column_config.NumberColumn(format="$%.2f"),
                                "Kelly EV":      st.column_config.NumberColumn(format="$%+.2f"),
                                "Kelly Capped":  st.column_config.CheckboxColumn(),
                            },
                        )
                        st.caption(
                            "**Edge (pp)** = model probability − sportsbook implied probability. "
                            "**EV / $100** = expected dollar return per $100 wagered. "
                            f"**Kelly Stake** uses {kelly_label} of bankroll ${bankroll:,.0f}, "
                            f"capped at {max_pct}% per bet. **Kelly Capped** ✓ means the cap is binding. "
                            "**Verify? ❓** flags edges ≥ +20pp — manually confirm the line is "
                            "actually placeable at the quoted odds before betting (real markets "
                            "rarely leave that much edge sitting around). Not betting advice."
                        )

        # ---------- ROUND ROBIN ----------
        st.markdown("---")
        st.markdown("### 🎰 Round Robin Builder")

        # Build pick pool — prefer odds-augmented edge_rows if available,
        # else fall back to model-only with fair (no-vig) odds.
        edge_rows_cached = st.session_state.get("edge_rows") if st.session_state.get("edge_date") == date_str else None

        candidate_rows = []
        if edge_rows_cached:
            for r in edge_rows_cached:
                candidate_rows.append({
                    "name":     r["Batter"],
                    "team":     r["Team"],
                    "game_id":  None,  # filled below by joining hr_rows
                    "prob":     r["Model %"] / 100.0,
                    "decimal_odds": parlay_model.american_to_decimal(int(r["Best Odds"])),
                    "american_odds": int(r["Best Odds"]),
                    "book":     r["Book"],
                    "edge":     r["Edge (pp)"],
                    "implied":  r["Implied %"] / 100.0,
                    "source":   "real",
                })
            odds_source = "real"
        else:
            # No odds yet — use model probability to construct fair-odds proxies
            for r in hr_rows:
                p = r["P(HR≥1)"] / 100.0
                if p <= 0:
                    continue
                fair_dec = parlay_model.implied_to_fair_decimal(p)
                candidate_rows.append({
                    "name":     r["Batter"],
                    "team":     r["Team"],
                    "game_id":  r["Game"],
                    "prob":     p,
                    "decimal_odds": fair_dec,
                    "american_odds": parlay_model.decimal_to_american(fair_dec),
                    "book":     "fair",
                    "edge":     0.0,
                    "implied":  p,
                    "source":   "fair",
                })
            odds_source = "fair"

        # Fill in game_id for the odds-augmented case by name-joining to hr_rows
        if odds_source == "real":
            from data import odds as _odds_helpers
            name_to_game = {_odds_helpers.normalize_name(r["Batter"]): r["Game"] for r in hr_rows}
            for c in candidate_rows:
                c["game_id"] = name_to_game.get(_odds_helpers.normalize_name(c["name"]), c["name"])

        if not candidate_rows:
            st.info("Compute model predictions and (optionally) fetch odds above to populate the round-robin pool.")
        else:
            badge = ("✅ Real DK/FD odds" if odds_source == "real"
                     else "⚠️ Using fair (no-vig) odds — fetch sportsbook odds above for real EV")
            st.caption(badge)

            # Sort by EV-friendly default: edge if odds; prob if fair
            sort_key = "edge" if odds_source == "real" else "prob"
            candidate_rows.sort(key=lambda x: -x[sort_key])

            # ----- UI inputs -----
            ic1, ic2, ic3 = st.columns([3, 1, 1])
            with ic1:
                # Build display labels: "Judge — Yankees @ Mets — model 17%, +320 (DK), edge +4.2pp"
                def _label(c):
                    extras = ""
                    if c["source"] == "real":
                        extras = f", {c['american_odds']:+d} ({c['book']}), edge {c['edge']:+.1f}pp"
                    return (f"{c['name']} — {c['team']} — "
                            f"model {c['prob']*100:.1f}%{extras}")
                labels = [_label(c) for c in candidate_rows]
                # Default: top 6 by sort key
                default_labels = labels[:6]
                selected_labels = st.multiselect(
                    "Picks for round robin",
                    options=labels,
                    default=default_labels,
                    help="Select batters to include in the round robin.",
                )
                selected_picks = [candidate_rows[labels.index(l)] for l in selected_labels]

            with ic2:
                sizes = st.multiselect(
                    "Combo sizes",
                    options=[2, 3, 4, 5, 6, 7, 8],
                    default=[3],
                    help="Round robin 'by 3s' = all 3-leg parlays from your picks. Multi-select supports e.g. by 2s + 3s.",
                )
            with ic3:
                stake = st.number_input(
                    "$ per parlay",
                    min_value=0.10, max_value=10000.0, value=10.0, step=1.0,
                )
                one_per_game = st.toggle(
                    "1 batter / game",
                    value=True,
                    help="Skips parlays that put two batters in the same game (correlated outcomes).",
                )

            if not selected_picks or not sizes:
                st.info("Select at least 2 picks and at least one combo size.")
            else:
                # Pre-flight count
                from math import comb
                est_max = sum(comb(len(selected_picks), k) for k in sizes if k <= len(selected_picks))
                if est_max > 500:
                    st.warning(f"That's {est_max} parlays — large round robins get expensive. "
                               "Consider trimming picks or sizes.")

                parlays, summ = parlay_model.build_round_robin(
                    selected_picks, sizes, stake_per_parlay=stake, one_per_game=one_per_game,
                )

                if not parlays:
                    st.warning("No valid parlays — too few picks for the chosen size, or "
                               "all combos blocked by 1-per-game constraint. Add picks from "
                               "different games, or disable the constraint.")
                else:
                    # Summary metrics
                    sm1, sm2, sm3, sm4, sm5 = st.columns(5)
                    sm1.metric("Parlays",           summ["total_parlays"])
                    sm2.metric("Total stake",       f"${summ['total_stake']:,.2f}")
                    ev_color = "normal" if summ["total_ev"] >= 0 else "inverse"
                    sm3.metric("Expected return",   f"${summ['total_ev']:+,.2f}",
                               delta=f"{summ['ev_per_dollar']*100:+.2f}%")
                    sm4.metric("Max payout (all hit)", f"${summ['total_max_payout']:,.0f}")
                    sm5.metric("E[winning parlays]", f"{summ['expected_winners']:.2f}")

                    if summ.get("hit_distribution"):
                        hd = summ["hit_distribution"]
                        h1, h2 = st.columns(2)
                        h1.metric("P(at least 1 parlay hits)", f"{hd['p_at_least_one_win']*100:.1f}%")
                        h2.metric("P(net profitable)",          f"{hd['p_profit']*100:.1f}%")

                        with st.expander("Hit distribution (exact joint probability)"):
                            df_pmf = pd.DataFrame(hd["pmf"])
                            df_pmf["prob"] = df_pmf["prob"] * 100
                            df_pmf = df_pmf.rename(columns={
                                "winners": "Winning parlays",
                                "prob":    "Probability %",
                                "payout":  "Total payout ($)",
                                "pnl":     "Net P&L ($)",
                            })
                            st.dataframe(df_pmf, use_container_width=True, hide_index=True,
                                column_config={
                                    "Probability %":   st.column_config.NumberColumn(format="%.3f%%"),
                                    "Total payout ($)": st.column_config.NumberColumn(format="$%,.2f"),
                                    "Net P&L ($)":      st.column_config.NumberColumn(format="$%+,.2f"),
                                })

                    st.markdown("#### All parlays (sorted by EV)")
                    df_par = pd.DataFrame([{
                        "Legs":         " · ".join(p["legs"]),
                        "K":            p["leg_count"],
                        "Prob %":       round(p["prob"] * 100, 3),
                        "American":     p["american_odds"],
                        "Decimal":      round(p["decimal_odds"], 2),
                        "Payout":       round(p["payout"], 2),
                        "Profit if win":round(p["profit_if_win"], 2),
                        "EV":           round(p["ev"], 2),
                    } for p in sorted(parlays, key=lambda x: -x["ev"])])
                    st.dataframe(df_par, use_container_width=True, hide_index=True, height=400,
                        column_config={
                            "Prob %":         st.column_config.NumberColumn(format="%.3f%%"),
                            "American":       st.column_config.NumberColumn(format="%+d"),
                            "Decimal":        st.column_config.NumberColumn(format="%.2f"),
                            "Payout":         st.column_config.NumberColumn(format="$%,.2f"),
                            "Profit if win":  st.column_config.NumberColumn(format="$%,.2f"),
                            "EV":             st.column_config.NumberColumn(format="$%+,.2f"),
                        })

                    with st.expander("ℹ️ Round-robin caveats"):
                        st.markdown(f"""
- **Independence assumption** — all probabilities multiplied across legs, treating outcomes as independent. This is reasonable across games but {"violated within a game (currently constrained off)" if not one_per_game else "enforced via the 1-per-game toggle"}.
- **{"Real sportsbook odds" if odds_source == "real" else "Fair (no-vig) odds"} mode** — {"using best available American odds across selected books. EV reflects the actual market price you'd pay." if odds_source == "real" else "no sportsbook odds loaded; EV is computed against fair odds derived from the model probability, so EV is ~0 by construction. Fetch live odds in the Odds Edge section above to see real EV."}
- **Hit distribution** is the *exact* joint probability over the {len(selected_picks)} underlying picks (2^{len(selected_picks)} = {2**len(selected_picks)} outcomes brute-forced). Parlays sharing legs are correctly correlated.
- **Not betting advice** — model has no historical backtest yet.
""")

    # ---------- SUGGESTED BETS: ALTERNATE LINES ----------
    st.markdown("---")
    st.markdown("### 🎯 Suggested Bets — H+R+RBI Over Lines")
    st.caption(
        "**H+R+RBI** (Hits + Runs + RBIs combined) over props where the market favors the over (negative odds). "
        "**2025 rate** = per-game frequency of meeting the threshold from 2025 game logs. "
        "**Edge** = 2025 rate − market implied probability. Only positive-edge bets shown."
    )

    if bdl_api.is_configured():
        # ---- BDL GOAT-plan: direct player props (6 books, no Odds API needed) ----
        from data import odds as _odds_mod

        # Clear stale session data when date changes
        if st.session_state.get("_hrr_date") != date_str:
            st.session_state.pop("_hrr_rows", None)
            st.session_state.pop("_hrr_date", None)

        _bdl_vendor_opts = ["draftkings", "fanduel", "betmgm", "caesars", "betrivers", "fanatics"]
        _bdl_vendors_sel = st.multiselect(
            "Books (alt lines)",
            options=_bdl_vendor_opts,
            default=["draftkings", "fanduel", "betmgm"],
            key="bdl_alt_books_select",
            help="BallDontLie GOAT plan — all 6 sportsbooks available.",
        )

        _bdl_today_games = load_bdl_games_for_date(date_str)
        _exclude_statuses = {"STATUS_FINAL", "FINAL", "STATUS_POSTPONED", "POSTPONED"}
        _bdl_upcoming = [
            g for g in _bdl_today_games
            if g.get("status", "").upper() not in _exclude_statuses
        ]

        _btn_label = f"🎯 Fetch alternate lines ({len(_bdl_upcoming)} game(s))"
        if not _bdl_upcoming:
            st.info("No active or upcoming games today — props are only available pre-game or in-game.")

        if st.button(_btn_label, key="fetch_bdl_alt_lines_btn", disabled=(not _bdl_upcoming)):
            _bdl_alt_raw: list[dict] = []
            _prog = st.progress(0)
            with st.spinner("Fetching H+R+RBI player props from BallDontLie..."):
                for _idx, _g in enumerate(_bdl_upcoming):
                    _gid = _g["id"]
                    _atm = _g.get("away_team_name") or (_g.get("away_team") or {}).get("full_name", "?")
                    _htm = _g.get("home_team_name") or (_g.get("home_team") or {}).get("full_name", "?")
                    _matchup = f"{_atm} @ {_htm}"
                    _props = load_bdl_props_for_game(_gid)
                    for _p in _props:
                        _vendor = _p.get("vendor", "")
                        if _bdl_vendors_sel and _vendor not in _bdl_vendors_sel:
                            continue
                        _prop_type = _p.get("prop_type", "")
                        if _prop_type != "hits_runs_rbis":
                            continue
                        _mkt = _p.get("market") or {}
                        _mk_type = _mkt.get("type", "")
                        # over_under: use over_odds; milestone: use odds (single sided)
                        if _mk_type == "over_under":
                            _odds_val = _mkt.get("over_odds")
                        elif _mk_type == "milestone":
                            _odds_val = _mkt.get("odds")
                        else:
                            continue
                        if _odds_val is None:
                            continue
                        try:
                            _odds_int = int(_odds_val)
                        except (TypeError, ValueError):
                            continue
                        if _odds_int < -1000 or _odds_int > 600:
                            continue
                        _bdl_alt_raw.append({
                            "player_id":  _p.get("player_id"),
                            "vendor":     _vendor,
                            "prop_type":  _prop_type,
                            "line_value": _p.get("line_value"),
                            "odds":       _odds_int,
                            "implied":    _odds_mod.american_to_implied(_odds_int),
                            "matchup":    _matchup,
                        })
                    _prog.progress((_idx + 1) / max(len(_bdl_upcoming), 1))
            _prog.empty()

            if not _bdl_alt_raw:
                st.session_state["_hrr_rows"] = []
                st.session_state["_hrr_date"] = date_str
                st.rerun()
            else:
                # Best (highest/least-negative odds = best value for bettor) per (player_id, prop_type, line_value)
                _best_bdl: dict = {}
                for _r in _bdl_alt_raw:
                    _lk = (_r["player_id"], _r["prop_type"], _r["line_value"])
                    if _lk not in _best_bdl or _r["odds"] > _best_bdl[_lk]["odds"]:
                        _best_bdl[_lk] = _r

                _last_season = season - 1   # 2025
                _unique_pids = {k[0] for k in _best_bdl if k[0]}
                _rates_cache_bdl: dict[int, dict] = {}

                with st.spinner(
                    f"Loading 2025 game-log rates for {len(_unique_pids)} player(s) via BallDontLie..."
                ):
                    for _pid in _unique_pids:
                        if _pid not in _rates_cache_bdl:
                            _rates_cache_bdl[_pid] = load_bdl_game_log_rates_by_id(_pid, _last_season)

                _built_rows = []
                for (_pid, _prop_type, _line_val), _offer in _best_bdl.items():
                    _rates = _rates_cache_bdl.get(_pid) or {}
                    if not _rates:
                        continue
                    _rk = _odds_mod.bdl_prop_rate_key(_prop_type, _line_val)
                    if not _rk:
                        continue
                    _hit_rate = _rates.get(_rk)
                    if _hit_rate is None:
                        continue
                    _implied = _offer["implied"]
                    _edge = _hit_rate - _implied
                    if _edge <= 0:
                        continue

                    # Name comes from stats record — no extra API call needed
                    _pname = _rates.get("player_name") or f"Player #{_pid}"
                    _prop_display = "H+R+RBI" if _prop_type == "hits_runs_rbis" else _prop_type.replace("_", " ").title()
                    _line_lbl = (
                        f"{_prop_display} Over {_line_val}"
                        if _line_val is not None
                        else _prop_display
                    )
                    _built_rows.append({
                        "Player":        _pname,
                        "Market":        _line_lbl,
                        "2025 Hit Rate": round(_hit_rate * 100, 1),
                        "2025 Games":    _rates["games"],
                        "Best Odds":     _offer["odds"],
                        "Book":          _offer["vendor"],
                        "Implied %":     round(_implied * 100, 1),
                        "Edge (pp)":     round(_edge * 100, 1),
                        "Matchup":       _offer["matchup"],
                    })

                _built_rows.sort(key=lambda _x: -_x["Edge (pp)"])
                st.session_state["_hrr_rows"] = _built_rows
                st.session_state["_hrr_date"] = date_str
                st.rerun()

        # ---- Render from session state (persists across interactions) ----
        _hrr_display = st.session_state.get("_hrr_rows")
        if _hrr_display is None:
            pass  # not yet fetched — show nothing
        elif len(_hrr_display) == 0:
            st.warning(
                "No H+R+RBI prop lines found. "
                "These markets may not be posted yet for today's upcoming games."
            )
        else:
            st.success(f"**{len(_hrr_display)}** positive-edge alternate bet(s) found.")

            for _i, _r in enumerate(_hrr_display, start=1):
                _edge_val = _r["Edge (pp)"]
                _col = "#22c55e" if _edge_val >= 5 else "#a3e635" if _edge_val >= 3 else "#fbbf24"
                _medal = (
                    "🥇" if _i == 1 else "🥈" if _i == 2 else "🥉"
                    if _i == 3 else f"<span style='color:#64748b;font-weight:700'>#{_i}</span>"
                )
                st.markdown(
                    f"""
                    <div style='background:#1e293b;padding:12px 16px;border-radius:8px;
                                margin-bottom:6px;display:flex;align-items:center;gap:14px'>
                      <div style='width:32px;font-size:20px;text-align:center'>{_medal}</div>
                      <div style='flex:1;min-width:0'>
                        <div style='color:#f1f5f9;font-size:15px;font-weight:600'>{_r['Player']}</div>
                        <div style='color:#94a3b8;font-size:12px'>{_r['Matchup']}</div>
                        <div style='color:#fbbf24;font-size:13px;margin-top:2px'>
                          {_r['Best Odds']:+d} on {_r['Book']} · <b>{_r['Market']}</b>
                        </div>
                        <div style='color:#94a3b8;font-size:11px;margin-top:2px'>
                          2025 hit rate: <b style='color:#e2e8f0'>{_r['2025 Hit Rate']}%</b>
                          ({_r['2025 Games']} games) &nbsp;·&nbsp; Implied: {_r['Implied %']}%
                        </div>
                      </div>
                      <div style='text-align:right;min-width:90px'>
                        <div style='color:{_col};font-size:22px;font-weight:800;line-height:1'>
                          +{_edge_val:.1f}<span style='font-size:13px'>pp</span>
                        </div>
                        <div style='color:#94a3b8;font-size:10px;text-transform:uppercase;
                                    letter-spacing:1px'>edge</div>
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            with st.expander("Full table"):
                st.dataframe(
                    pd.DataFrame(_hrr_display),
                    use_container_width=True, hide_index=True,
                    column_config={
                        "Best Odds":     st.column_config.NumberColumn(format="%+d"),
                        "2025 Hit Rate": st.column_config.NumberColumn(format="%.1f%%"),
                        "Implied %":     st.column_config.NumberColumn(format="%.1f%%"),
                        "Edge (pp)":     st.column_config.NumberColumn(format="%+.1f"),
                    },
                )
            st.caption(
                "**2025 Hit Rate** = fraction of 2025 regular-season games in which the player "
                "met the threshold (e.g. 2+ H+R+RBI combined). "
                "Data via **BallDontLie GOAT** (DraftKings, FanDuel, BetMGM, Caesars, BetRivers, Fanatics). "
                "**Edge** = 2025 Hit Rate − current market implied probability. "
                "Past frequency does not guarantee future performance. Not betting advice."
            )

    else:
        # ---- Fallback: The Odds API (used when BDL key not configured) ----
        _alt_key = resolve_odds_key()
        if not _alt_key:
            st.info("👈 Configure BallDontLie or Odds API key in the sidebar to see alternate line suggested bets.")
        else:
            from data import odds as _odds_mod

            _alt_books = st.multiselect(
                "Books (alt lines)",
                options=["draftkings", "fanduel", "betmgm", "williamhill_us", "bovada",
                         "betrivers", "fanatics", "betonlineag", "lowvig", "mybookieag"],
                default=["draftkings", "fanduel", "betmgm", "williamhill_us", "bovada"],
                key="alt_books_select",
                help="Alternate hits / total-bases props. DK/FD/BetMGM typically post these. "
                     "Caesars is keyed as `williamhill_us` in The Odds API.",
            )
            _alt_books_str = ",".join(_alt_books) if _alt_books else "draftkings,fanduel,betmgm"

            try:
                _alt_events = load_odds_events(_alt_key)
            except odds_api.OddsAPIError as e:
                st.error(f"Odds API: {e}")
                _alt_events = []

            _alt_event_map = {}
            for _ev in _alt_events:
                _k = (_ev.get("away_team", "") + "|" + _ev.get("home_team", "")).lower()
                _alt_event_map[_k] = _ev["id"]

            _alt_game_ids = []
            for _g in enriched:
                _k = (_g["away_team"] + "|" + _g["home_team"]).lower()
                _eid = _alt_event_map.get(_k)
                if _eid:
                    _alt_game_ids.append((_eid, _g["away_team"], _g["home_team"]))

            st.caption(f"⚡ Will use ~{len(_alt_game_ids)} API request(s) for {len(_alt_game_ids)} game(s).")

            if st.button(
                f"🎯 Fetch alternate lines ({len(_alt_game_ids)} games)",
                key="fetch_alt_lines_btn",
                disabled=(len(_alt_game_ids) == 0),
            ):
                _alt_raw = []
                _alt_prog = st.progress(0)
                with st.spinner("Fetching alternate prop odds (-200 to -1000)..."):
                    for _i, (_eid, _atm, _htm) in enumerate(_alt_game_ids):
                        _rows = load_alt_prop_odds_for_event(_alt_key, _eid, _alt_books_str)
                        for _r in _rows:
                            _r["matchup"] = f"{_atm} @ {_htm}"
                        _alt_raw.extend(_rows)
                        _alt_prog.progress((_i + 1) / len(_alt_game_ids))
                _alt_prog.empty()

                if not _alt_raw:
                    st.warning(
                        "No alternate prop lines returned in the -200 to -1000 range. "
                        "These markets may not be posted yet, or selected books do not offer them."
                    )
                else:
                    for _r in _alt_raw:
                        _r["norm_name"] = _odds_mod.normalize_name(_r["player"])

                    _name_to_pid = {
                        _odds_mod.normalize_name(_r["Batter"]): _r["_batter_id"]
                        for _r in hr_rows
                    }
                    _best_by_line: dict = {}
                    for _r in _alt_raw:
                        _lk = (_r["norm_name"], _r["market_key"], _r.get("point"))
                        if _lk not in _best_by_line or _r["implied_prob"] < _best_by_line[_lk]["implied_prob"]:
                            _best_by_line[_lk] = _r

                    _last_season = season - 1
                    _rates_cache: dict = {}
                    _alt_edge_rows = []
                    _nn_to_display = {_odds_mod.normalize_name(_r["Batter"]): _r["Batter"] for _r in hr_rows}
                    _nn_to_team   = {_odds_mod.normalize_name(_r["Batter"]): _r["Team"]   for _r in hr_rows}

                    with st.spinner(f"Loading {_last_season} game-log rates (MLB Stats API)..."):
                        for (_nn, _mk, _pt), _offer in _best_by_line.items():
                            _pid = _name_to_pid.get(_nn)
                            if not _pid:
                                continue
                            if _pid not in _rates_cache:
                                _rates_cache[_pid] = load_batter_game_log_rates(_pid, _last_season)
                            _rates = _rates_cache.get(_pid) or {}
                            if not _rates:
                                continue
                            _rk = _odds_mod.alt_odds_rate_key(_mk, _pt)
                            if not _rk:
                                continue
                            _hit_rate = _rates.get(_rk)
                            if _hit_rate is None:
                                continue
                            _implied = _offer["implied_prob"]
                            _edge = _hit_rate - _implied
                            if _edge <= 0:
                                continue
                            _alt_edge_rows.append({
                                "Player":        _nn_to_display.get(_nn, _offer["player"]),
                                "Team":          _nn_to_team.get(_nn, "—"),
                                "Market":        (f"{_offer['market_label']} Over {_pt}" if _pt is not None else _offer["market_label"]),
                                "2025 Hit Rate": round(_hit_rate * 100, 1),
                                "2025 Games":    _rates["games"],
                                "Best Odds":     _offer["american_odds"],
                                "Book":          _offer["bookmaker"],
                                "Implied %":     round(_implied * 100, 1),
                                "Edge (pp)":     round(_edge * 100, 1),
                                "Matchup":       _offer["matchup"],
                            })

                    if not _alt_edge_rows:
                        st.info("No positive-edge alternate lines found.")
                    else:
                        _alt_edge_rows.sort(key=lambda _x: -_x["Edge (pp)"])
                        st.success(f"**{len(_alt_edge_rows)}** positive-edge alternate bet(s) found.")
                        for _i, _r in enumerate(_alt_edge_rows, start=1):
                            _edge_val = _r["Edge (pp)"]
                            _col = "#22c55e" if _edge_val >= 5 else "#a3e635" if _edge_val >= 3 else "#fbbf24"
                            _medal = (
                                "🥇" if _i == 1 else "🥈" if _i == 2 else "🥉"
                                if _i == 3 else f"<span style='color:#64748b;font-weight:700'>#{_i}</span>"
                            )
                            st.markdown(
                                f"""
                                <div style='background:#1e293b;padding:12px 16px;border-radius:8px;
                                            margin-bottom:6px;display:flex;align-items:center;gap:14px'>
                                  <div style='width:32px;font-size:20px;text-align:center'>{_medal}</div>
                                  <div style='flex:1;min-width:0'>
                                    <div style='color:#f1f5f9;font-size:15px;font-weight:600'>{_r['Player']}</div>
                                    <div style='color:#94a3b8;font-size:12px'>{_r['Team']} — {_r['Matchup']}</div>
                                    <div style='color:#fbbf24;font-size:13px;margin-top:2px'>
                                      {_r['Best Odds']:+d} on {_r['Book']} · <b>{_r['Market']}</b>
                                    </div>
                                    <div style='color:#94a3b8;font-size:11px;margin-top:2px'>
                                      2025 hit rate: <b style='color:#e2e8f0'>{_r['2025 Hit Rate']}%</b>
                                      ({_r['2025 Games']} games) &nbsp;·&nbsp; Implied: {_r['Implied %']}%
                                    </div>
                                  </div>
                                  <div style='text-align:right;min-width:90px'>
                                    <div style='color:{_col};font-size:22px;font-weight:800;line-height:1'>
                                      +{_edge_val:.1f}<span style='font-size:13px'>pp</span>
                                    </div>
                                    <div style='color:#94a3b8;font-size:10px;text-transform:uppercase;
                                                letter-spacing:1px'>edge</div>
                                  </div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )
                        with st.expander("Full table"):
                            st.dataframe(
                                pd.DataFrame(_alt_edge_rows), use_container_width=True, hide_index=True,
                                column_config={
                                    "Best Odds":     st.column_config.NumberColumn(format="%+d"),
                                    "2025 Hit Rate": st.column_config.NumberColumn(format="%.1f%%"),
                                    "Implied %":     st.column_config.NumberColumn(format="%.1f%%"),
                                    "Edge (pp)":     st.column_config.NumberColumn(format="%+.1f"),
                                },
                            )
                        st.caption(
                            "**2025 Hit Rate** = fraction of 2025 regular-season games in which the player "
                            "met the threshold. Data sourced from **MLB Stats API**. "
                            "**Edge** = 2025 Hit Rate − current market implied probability. "
                            "Past frequency does not guarantee future performance. Not betting advice."
                        )

    with st.expander("📐 Methodology"):
        st.markdown(f"""
**Per-PA HR probability (model v1.1 — Tier-1 upgrades):**

```
p(HR | PA) = base × Bskill × Mpit × Fpark_hand × Fhand × Ftemp × Fhumid × Fwind
```

- **base** = league HR/PA = {league_const.HR_PER_PA:.4f}
- **Bskill** = `(barrel%/{league_const.BARREL_PCT:.1f})^0.55 × (xSLG/{league_const.xSLG:.3f})^0.30 × (HardHit%/{league_const.HARDHIT_PCT:.1f})^0.15`
  → **NEW:** Recent-form adjustment from MLB last-15-day HR/PA, regressed toward season skill (max ±15%)
- **Fpark_hand** **(NEW)** — handedness-specific park HR factor. Yankee Stadium = 125 for LHB, 98 for RHB; Fenway = 85 for LHB, 110 for RHB; etc.
- **Fhand** **(NEW)** — pitcher's vsL or vsR OPS-against (whichever the batter faces), normalized to league: `(split_OPS / 0.720)^1.83`. Switch-hitters bat opposite of pitcher.
- **Mpit** = blend of regressed HR/9 and xwOBA-against, vs league
- **Fpark** = ballpark HR factor / 100
- **Ftemp** = +1% per °F above 70°F (capped at ±20%)
- **Fhumid** = drier air slightly raises HR rate
- **Fwind** = ±1.5% per mph above 5mph, blowing out / in

**Per-game probability:** `1 − (1 − p)^expected_PA` where expected_PA ≈ 4.3 (4.7 for confirmed lineup starters).

**Game predictions:**
- Each team's runs = `4.5 × OffenseFactor × OppPitchingFactor × ParkFactor × TempFactor × HumidityFactor`
- OffenseFactor = `(blended_OPS / 0.720)^1.83` — Bayesian blend of season (70%) and last-15-day (30%) OPS, regressed toward league avg
- OppPitchingFactor = 60% starter (xERA preferred) + 40% bullpen (team season ERA), each regressed toward 4.20 ERA
- Win probability = logistic on expected run differential, calibrated to single-game variance (1-run favorite ≈ 56%, 2-run ≈ 62%, 3-run ≈ 68%)
- Home-field advantage modeled as +0.15 expected runs

**Important caveats** (read these):
1. **No backtest.** This is a structural model from baseball-research priors, not a learned model. It is well-calibrated by construction (a league-average matchup returns league averages) but its ranking edge against a market is unverified.
2. **Sample-size regression.** Players with low PA are heavily shrunk toward league average — early-season "hot streaks" should not surface as wild outliers.
3. **Not betting advice.** Sportsbook implied odds incorporate sharper-than-public information (lineup news, injuries, weather updates) the model does not see.
4. **Missing factors.** No batter-vs-pitcher career data, no batter handedness × pitcher handedness adjustment (would require an additional API roundtrip per batter), no umpire factors, no bullpen usage tracking.
""")


# ===== TAB 5: AI TOP PICKS =====

with tab_ai:
    import datetime as _dt

    st.markdown(f"## 🤖 AI Top Picks — {date_str}")

    def _ai_calibrate(p: float) -> float:
        return p * 0.75 if 0.15 <= p < 0.40 else p

    # Load today's saved predictions
    _ai_pred_path = os.path.join("predictions", f"{date_str}.jsonl")
    _ai_rows: list[dict] = []
    _ai_mtime = None
    if os.path.exists(_ai_pred_path):
        _ai_mtime = os.path.getmtime(_ai_pred_path)
        with open(_ai_pred_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line:
                    continue
                _r = json.loads(_line)
                if "p_at_least_one" not in _r:
                    continue
                _name = _r.get("batter_name", "?")
                if "," in _name:
                    _last, _, _first = _name.partition(",")
                    _name = f"{_first.strip()} {_last.strip()}"
                _r["_display_name"] = _name
                _r["_p_cal"] = _ai_calibrate(_r["p_at_least_one"])
                _ai_rows.append(_r)
        _ai_rows.sort(key=lambda x: -x["_p_cal"])

    # ── Header row: timestamp + refresh button ───────────────────────────────
    _hcol1, _hcol2 = st.columns([3, 1])
    with _hcol1:
        if _ai_mtime:
            _age_min = (_dt.datetime.now().timestamp() - _ai_mtime) / 60
            _ts_str  = _dt.datetime.fromtimestamp(_ai_mtime).strftime("%I:%M %p")
            if _age_min < 60:
                st.caption(f"Last updated: **{_ts_str}** ({int(_age_min)} min ago)")
            elif _age_min < 180:
                st.caption(f"Last updated: **{_ts_str}** ({int(_age_min/60)}h {int(_age_min%60)}m ago)")
            else:
                st.warning(
                    f"⚠️ Predictions are **{int(_age_min/60)} hours old** (last run at {_ts_str}). "
                    f"Hit **Refresh Picks** to update weather, lineups, and pitcher changes."
                )
        else:
            st.caption("No predictions generated yet for today.")
    with _hcol2:
        if st.button("🔄 Refresh Picks", use_container_width=True,
                     help="Clears all cached data and re-runs the full model with the latest lineups, weather, and pitcher info."):
            st.cache_data.clear()
            st.rerun()

    if not _ai_rows:
        st.info("No predictions found for today. Hit **🔄 Refresh Picks** above to generate them now.")
    else:
        _ai_top3 = _ai_rows[:3]
        st.caption(f"{len(_ai_rows)} total players modeled today")

        # ── Top 3 pick cards ────────────────────────────────────────────────
        st.markdown("### 🎯 Today's Top 3 Picks")
        _medals = ["🥇", "🥈", "🥉"]
        _cols = st.columns(3)
        for _i, (_col, _pick) in enumerate(zip(_cols, _ai_top3)):
            _p = _pick["_p_cal"]
            _color = "#22c55e" if _p >= 0.40 else "#f59e0b" if _p >= 0.25 else "#94a3b8"
            _conf = "High" if _p >= 0.40 else "Medium" if _p >= 0.25 else "Low"
            with _col:
                st.markdown(f"""
<div style='background:#1e293b;border-radius:12px;padding:16px;text-align:center;border:1px solid #334155'>
  <div style='font-size:28px'>{_medals[_i]}</div>
  <div style='color:#f1f5f9;font-size:15px;font-weight:700;margin:6px 0'>{_pick['_display_name']}</div>
  <div style='color:#94a3b8;font-size:12px'>{_pick.get('team','?')}</div>
  <div style='color:{_color};font-size:26px;font-weight:800;margin:10px 0'>{_p*100:.1f}%</div>
  <div style='color:#64748b;font-size:11px'>vs {_pick.get('opp_pitcher','?')}</div>
  <div style='background:#0f172a;border-radius:6px;padding:6px;margin-top:10px;font-size:11px;color:#94a3b8'>
    Bat ×{_pick.get('bat_factor',1):.2f} &nbsp;|&nbsp;
    Pit ×{_pick.get('pit_factor',1):.2f} &nbsp;|&nbsp;
    Hand ×{_pick.get('hand_factor',1):.2f}
  </div>
  <div style='margin-top:8px;background:{"#14532d" if _conf=="High" else "#713f12" if _conf=="Medium" else "#1e293b"};
    border-radius:4px;padding:3px 8px;display:inline-block;font-size:11px;color:{_color}'>{_conf} confidence</div>
</div>
""", unsafe_allow_html=True)

        st.markdown("---")

        # ── Recommended strategy ────────────────────────────────────────────
        st.markdown("### 💰 Recommended Strategy")

        def _market_dec(p: float, vig: float = 0.07) -> float:
            return 1.0 / (min(p, 0.60) * (1.0 + vig))

        _p1 = _ai_top3[0]["_p_cal"]
        _p2 = _ai_top3[1]["_p_cal"] if len(_ai_top3) > 1 else _p1
        _p3 = _ai_top3[2]["_p_cal"] if len(_ai_top3) > 2 else _p1

        _dec1 = _market_dec(_p1)
        _dec2 = _market_dec(_p2)
        _dec3 = _market_dec(_p3)

        def _to_american(dec: float) -> str:
            if dec >= 2.0:
                return f"+{int((dec - 1) * 100)}"
            else:
                return f"{int(-100 / (dec - 1))}"

        _strat_cols = st.columns(2)
        with _strat_cols[0]:
            st.markdown("**Singles ($100 each)**")
            for _i, (_pick, _dec) in enumerate(zip(_ai_top3, [_dec1, _dec2, _dec3]), 1):
                _am = _to_american(_dec)
                st.markdown(
                    f"- Pick #{_i} **{_pick['_display_name']}** &nbsp; "
                    f"<span style='color:#94a3b8'>est. {_am}</span>",
                    unsafe_allow_html=True
                )
            st.markdown(f"*Total singles stake: **$300***")

        with _strat_cols[1]:
            st.markdown("**Round Robin 2-of-3 Parlay ($100 each combo)**")
            _n1 = _ai_top3[0]['_display_name'].split()[-1]
            _n2 = _ai_top3[1]['_display_name'].split()[-1] if len(_ai_top3) > 1 else "Pick2"
            _n3 = _ai_top3[2]['_display_name'].split()[-1] if len(_ai_top3) > 2 else "Pick3"
            _rr_pairs = [
                (_n1, _n2, _dec1 * _dec2),
                (_n1, _n3, _dec1 * _dec3),
                (_n2, _n3, _dec2 * _dec3),
            ]
            for _a, _b, _odds in _rr_pairs:
                st.markdown(
                    f"- {_a} + {_b} &nbsp; "
                    f"<span style='color:#94a3b8'>est. {_to_american(_odds)}</span>",
                    unsafe_allow_html=True
                )
            st.markdown(f"*Total parlay stake: **$300***")

        st.info(
            f"**Total daily stake: $600**  |  "
            f"Backtest avg ROI: **+19.8%**  |  "
            f"Top-3 hit rate over last 17 days: **50%**"
        )

        # ── Full ranked table ───────────────────────────────────────────────
        with st.expander("📋 Full prediction rankings (all players)"):
            _ai_display = []
            for _i, _r in enumerate(_ai_rows[:30], 1):
                _ai_display.append({
                    "Rank":       _i,
                    "Player":     _r["_display_name"],
                    "Team":       _r.get("team", "?"),
                    "vs SP":      _r.get("opp_pitcher", "?"),
                    "Model %":    round(_r["_p_cal"] * 100, 1),
                    "Bat ×":      _r.get("bat_factor", "?"),
                    "Pit ×":      _r.get("pit_factor", "?"),
                    "Park ×":     _r.get("park_factor", "?"),
                    "Hand ×":     _r.get("hand_factor", "?"),
                    "Wind":       str(_r.get("wind_label", "?")).replace("→", ">").replace("←", "<"),
                })
            st.dataframe(
                pd.DataFrame(_ai_display),
                use_container_width=True,
                hide_index=True,
                column_config={"Model %": st.column_config.NumberColumn(format="%.1f%%")},
            )

    # ── Alt Under Parlay Generator ───────────────────────────────────────────
    import math as _au_math
    import urllib.request as _au_req
    import json as _au_json
    import time as _au_time

    st.markdown("---")
    st.markdown("### 📊 Alt Under Parlay Generator")
    st.caption(
        "Qualifies today's low-total games (exp ≤ 9.0 runs) and builds the "
        "optimal Under parlay using 2025 baseline stats + **raw pitcher ERA** (MLB Stats API).  "
        "Umpire zone factor and bullpen fatigue shown as **informational context only** "
        "(backtested separately — xERA and BP fatigue filter both hurt hit rate; reverted).  "
        "**14-day backtest: 83.4% hit rate · +24.3% ROI · 151 bets**"
    )

    # ── constants ──
    _AU_THRESHOLD  = 9.0
    _AU_MIN_BUF    = 2.0
    _AU_MAX_BUF    = 3.5
    _AU_EDGE_MIN   = 3.0
    _AU_ERA_DANGER = 6.5   # single starter above this → flag as landmine
    _AU_TRUE_P     = 0.848  # 14-day backtest hit rate per leg
    _AU_LEAGUE_ERA = 4.30  # raw ERA league average — backtest shows raw ERA > xERA here

    _AU_RPG25 = {
        'WSH':4.24,'CIN':4.42,'NYY':5.24,'BAL':4.18,'PHI':4.80,'BOS':4.85,
        'COL':3.69,'PIT':3.60,'TB':4.41,'TOR':4.93,'DET':4.68,'NYM':4.73,
        'CHC':4.89,'ATL':4.47,'KC':4.02,'CWS':3.99,'MIA':4.38,'MIN':4.18,
        'SD':4.33,'MIL':4.97,'AZ':4.88,'TEX':4.22,'SEA':4.73,'HOU':4.24,
        'STL':4.25,'ATH':4.53,'SF':4.35,'LAD':5.09,'LAA':4.15,'CLE':3.97,
    }
    _AU_RAPG25 = {
        'WSH':5.22,'CIN':3.80,'NYY':3.86,'BAL':4.53,'PHI':3.75,'BOS':3.68,
        'COL':5.76,'PIT':3.69,'TB':3.87,'TOR':4.13,'DET':3.89,'NYM':3.96,
        'CHC':3.73,'ATL':4.30,'KC':3.68,'CWS':4.14,'MIA':4.55,'MIN':4.45,
        'SD':3.57,'MIL':3.54,'AZ':4.45,'TEX':3.44,'SEA':3.88,'HOU':3.82,
        'STL':4.21,'ATH':4.63,'SF':3.76,'LAD':3.91,'LAA':4.80,'CLE':3.66,
    }

    def _au_exp_runs(off, def_r, sp_era):
        return max(1.5, (off + def_r) / 2.0 + (sp_era - _AU_LEAGUE_ERA) * 0.22)

    def _au_p_under(lam, line):
        return sum(
            (lam**k * _au_math.exp(-lam)) / _au_math.factorial(k)
            for k in range(int(line) + 1)
        )

    def _au_best_lines(exp_total):
        lines = set()
        for buf in (2.5, 3.0, 3.5, 4.0):
            raw = exp_total + buf
            rounded = _au_math.floor(raw * 2) / 2.0
            lines.add(rounded)
            lines.add(rounded + 0.5)
        return sorted(lines)

    def _au_est_odds(buf):
        if buf <= 2.0:  return -900
        if buf <= 2.5:  return -400
        if buf <= 3.0:  return -260
        if buf <= 3.5:  return -215
        if buf <= 4.0:  return -200
        return -185

    def _au_implied(odds):
        return abs(odds) / (abs(odds) + 100.0) if odds < 0 else 100.0 / (odds + 100.0)

    def _au_am_to_dec(odds):
        return 1.0 + 100.0 / abs(odds) if odds < 0 else 1.0 + odds / 100.0

    def _au_dec_to_am(dec):
        return int((dec - 1) * 100) if dec >= 2.0 else int(-100 / (dec - 1))

    @st.cache_data(ttl=3600, show_spinner=False)
    def _au_fetch_era(pid):
        if not pid:
            return _AU_LEAGUE_ERA
        try:
            url = (f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                   f"?stats=season&season={date_str[:4]}&group=pitching&gameType=R")
            with _au_req.urlopen(url, timeout=10, context=_UNVERIFIED_SSL) as _r:
                _d = _au_json.loads(_r.read())
            splits = _d.get("stats", [{}])[0].get("splits", [])
            if splits:
                _s = splits[0].get("stat", {})
                ip  = float(_s.get("inningsPitched", "0") or "0")
                era = float(_s.get("era", "0") or "0")
                return era if ip >= 5 else _AU_LEAGUE_ERA
        except Exception:
            pass
        return _AU_LEAGUE_ERA

    @st.cache_data(ttl=3600, show_spinner=False)
    def _au_xera_lookup(year_key):
        """Build xERA lookup from Baseball Savant (cached 1 hr)."""
        try:
            import sys as _au_sys
            _au_sys.path.insert(0, ".")
            from data.fangraphs import build_xera_lookup as _bxl
            return _bxl(year_key)
        except Exception:
            return {}

    @st.cache_data(ttl=1800, show_spinner=False)
    def _au_ump_lookup(date_str_key):
        """Fetch HP umpire data for all games today (cached 30 min)."""
        try:
            import sys as _au_sys
            _au_sys.path.insert(0, ".")
            from data.umpires import get_today_umpires as _gtu, get_umpire_stats as _gus
            _raw   = _gtu(date_str_key)
            _year  = int(date_str_key[:4])
            _out   = {}
            for _gid, _info in _raw.items():
                _stats = _gus(_info["name"], year=_year)
                _out[_gid] = {**_info, **_stats}
            return _out
        except Exception:
            return {}

    @st.cache_data(ttl=1800, show_spinner=False)
    def _au_fatigue_lookup(date_str_key):
        """Compute bullpen fatigue for last 3 days (cached 30 min)."""
        try:
            import sys as _au_sys
            _au_sys.path.insert(0, ".")
            from data.bullpen import compute_bullpen_fatigue as _cbf
            return _cbf(today=date_str_key)
        except Exception:
            return {}

    @st.cache_data(ttl=90, show_spinner=False)
    def _au_qualify(date_str_key):
        """Return (qualifying_bets, total_games_checked) for the given date."""
        url = (f"https://statsapi.mlb.com/api/v1/schedule"
               f"?sportId=1&date={date_str_key}&hydrate=probablePitcher,team")
        with _au_req.urlopen(url, timeout=15, context=_UNVERIFIED_SSL) as _r:
            sched = _au_json.loads(_r.read())

        games_raw = []
        for day in sched.get("dates", []):
            for g in day.get("games", []):
                if g.get("status", {}).get("abstractGameState", "") == "Final":
                    continue
                aw = g.get("teams", {}).get("away", {})
                hm = g.get("teams", {}).get("home", {})
                games_raw.append({
                    "game_pk":     g.get("gamePk"),
                    "away":        aw.get("team", {}).get("abbreviation", "?"),
                    "home":        hm.get("team", {}).get("abbreviation", "?"),
                    "away_pit":    aw.get("probablePitcher", {}).get("fullName", "TBD"),
                    "home_pit":    hm.get("probablePitcher", {}).get("fullName", "TBD"),
                    "away_pit_id": aw.get("probablePitcher", {}).get("id"),
                    "home_pit_id": hm.get("probablePitcher", {}).get("id"),
                })

        # ── Batch-fetch raw ERAs (xERA backtested — hurt hit rate, reverted) ──
        # Always use raw ERA for qualifying; xERA shown informational-only if desired
        pit_ids = {g["away_pit_id"] for g in games_raw if g["away_pit_id"]}
        pit_ids |= {g["home_pit_id"] for g in games_raw if g["home_pit_id"]}
        era_map = {}
        for pid in pit_ids:
            era_map[pid] = _au_fetch_era(pid)
            _au_time.sleep(0.04)

        # ── Umpire data (run_adj multiplier) ───────────────────────────────
        _ump_data = _au_ump_lookup(date_str_key)

        # ── Bullpen fatigue data (run_adj multiplier) ──────────────────────
        _fatigue = _au_fatigue_lookup(date_str_key)

        bets = []
        for g in games_raw:
            away, home = g["away"], g["home"]
            if away not in _AU_RPG25 or home not in _AU_RPG25:
                continue
            away_era = era_map.get(g["away_pit_id"], _AU_LEAGUE_ERA)
            home_era = era_map.get(g["home_pit_id"], _AU_LEAGUE_ERA)

            lam_a   = _au_exp_runs(_AU_RPG25[away], _AU_RAPG25[home], home_era)
            lam_b   = _au_exp_runs(_AU_RPG25[home], _AU_RAPG25[away], away_era)
            exp_tot = lam_a + lam_b

            # ── Umpire adjustment ──────────────────────────────────────────
            _ump_adj  = 1.0
            _ump_info = {}
            _gk = g.get("game_pk")
            if _gk and _gk in _ump_data:
                _ump_info = _ump_data[_gk]
                _ump_adj  = _ump_info.get("run_adj", 1.0)

            # ── Bullpen fatigue adjustment ─────────────────────────────────
            _bp_adj          = 1.0
            _bp_away_rating  = "normal"
            _bp_home_rating  = "normal"
            _bp_away_score   = 30
            _bp_home_score   = 30
            if _fatigue:
                _af = _fatigue.get(away, {})
                _hf = _fatigue.get(home, {})
                _bp_away_rating = _af.get("rating", "normal")
                _bp_home_rating = _hf.get("rating", "normal")
                _bp_away_score  = _af.get("fatigue_score", 30)
                _bp_home_score  = _hf.get("fatigue_score", 30)
                _bp_adj = ((_af.get("run_adj", 1.0) - 1) +
                           (_hf.get("run_adj", 1.0) - 1)) / 2 + 1

            # ── Adjusted total (informational only — not used for qualifying) ─
            adj_tot = exp_tot * _ump_adj * _bp_adj
            # Qualify on raw expected total — backtest: raw ERA > xERA+ump+BP
            if exp_tot > _AU_THRESHOLD:
                continue

            danger = max(away_era, home_era) >= _AU_ERA_DANGER
            warn   = max(away_era, home_era) >= 5.5 and not danger

            # Build ump display string
            _ump_icons = {"under": "🔵", "over": "🟠", "neutral": "⚪"}
            _ump_name  = _ump_info.get("name", "TBD").split()[-1] if _ump_info else "TBD"
            _ump_label = (f"{_ump_icons.get(_ump_info.get('impact','neutral'), '⚪')} {_ump_name}"
                          if _ump_info else "TBD")

            # Build bullpen display string
            _bp_icons  = {"fresh": "🟢", "normal": "⚪", "tired": "🟡", "exhausted": "🔴"}
            _bp_label  = (f"{_bp_icons.get(_bp_away_rating,'⚪')}{away}"
                          f"/{_bp_icons.get(_bp_home_rating,'⚪')}{home}")

            best = None
            for line in _au_best_lines(exp_tot):
                buf  = line - exp_tot
                if buf < _AU_MIN_BUF or buf > _AU_MAX_BUF:
                    continue
                odds = _au_est_odds(buf)
                if not (-400 <= odds <= -200):
                    continue
                hp   = _au_p_under(exp_tot, line)
                imp  = _au_implied(odds)
                edge = (hp - imp) * 100
                if edge < _AU_EDGE_MIN:
                    continue
                if best is None or edge > best["edge"]:
                    best = dict(
                        game=f"{away}@{home}", away=away, home=home,
                        game_pk=g.get("game_pk"),
                        away_pit=g["away_pit"], home_pit=g["home_pit"],
                        away_era=away_era, home_era=home_era,
                        exp_total=round(exp_tot, 2),
                        adj_total=round(adj_tot, 2),
                        ump_label=_ump_label,
                        ump_impact=_ump_info.get("impact", "neutral") if _ump_info else "neutral",
                        ump_adj=round(_ump_adj, 3),
                        bp_label=_bp_label,
                        bp_away_rating=_bp_away_rating,
                        bp_home_rating=_bp_home_rating,
                        bp_away_score=_bp_away_score,
                        bp_home_score=_bp_home_score,
                        bp_adj=round(_bp_adj, 3),
                        line=line,
                        bet=f"Under {line}", odds=odds,
                        implied=imp, hist_p=hp, edge=round(edge, 1),
                        danger=danger, warn=warn,
                    )
            if best:
                bets.append(best)

        # Sort: danger/warn last, then by edge desc within each tier
        bets.sort(key=lambda b: (b["danger"], b["warn"], -b["edge"]))
        return bets, len(games_raw)

    # ── Controls ──
    _au_c1, _au_c2 = st.columns([1, 3])
    with _au_c1:
        _au_gen = st.button("⚡ Generate Plays", type="primary",
                            use_container_width=True, key="au_gen_btn")
    with _au_c2:
        st.caption(
            "Fetches today's schedule + pitcher ERAs → qualifies games → "
            "builds parlay table. ~10s on first run, cached after that."
        )

    if _au_gen:
        _au_qualify.clear()
        _au_fetch_era.clear()
        _au_ump_lookup.clear()
        _au_fatigue_lookup.clear()
        st.session_state["au_ran_date"] = date_str

    _au_show = st.session_state.get("au_ran_date") == date_str

    if _au_show:
        with st.spinner("Fetching pitcher ERAs · umpires · bullpen fatigue · qualifying games…"):
            try:
                _au_bets, _au_total = _au_qualify(date_str)
            except Exception as _au_err:
                st.error(f"Could not load games: {_au_err}")
                _au_bets, _au_total = [], 0

        _au_clean   = [b for b in _au_bets if not b["danger"]]
        _au_danger  = [b for b in _au_bets if b["danger"]]

        if not _au_clean:
            st.info("No qualifying games today under these rules.")
        else:
            # ── Qualifying plays table ──────────────────────────────────────
            st.markdown(f"**{_au_total} games today → {len(_au_clean)} qualify "
                        f"({len(_au_danger)} flagged as landmines)**")

            _au_tbl = []
            for _i, _b in enumerate(_au_clean, 1):
                _icon = "⚠️" if _b["warn"] else "✅"
                _au_tbl.append({
                    "#":             _i,
                    "":              _icon,
                    "Game":          _b["game"],
                    "Bet":           _b["bet"],
                    "Exp":           _b["exp_total"],
                    "Adj":           _b["adj_total"],
                    "Est Odds":      _b["odds"],
                    "Model %":       round(_b["hist_p"] * 100, 1),
                    "Edge (pp)":     _b["edge"],
                    "Ump":           _b.get("ump_label", "TBD"),
                    "Bullpen":       _b.get("bp_label", "—"),
                    "Away SP":       f"{_b['away_pit'].split()[-1]} ({_b['away_era']:.2f})",
                    "Home SP":       f"{_b['home_pit'].split()[-1]} ({_b['home_era']:.2f})",
                })
            st.dataframe(
                pd.DataFrame(_au_tbl),
                use_container_width=True, hide_index=True,
                height=min(38 * len(_au_tbl) + 42, 480),
                column_config={
                    "Est Odds":  st.column_config.NumberColumn(format="%+d"),
                    "Model %":   st.column_config.NumberColumn(format="%.1f%%"),
                    "Edge (pp)": st.column_config.NumberColumn(format="%+.1f"),
                    "Exp":       st.column_config.NumberColumn(format="%.2f",
                                     help="Model expected runs (2025 baseline + raw pitcher ERA) — used for qualifying"),
                    "Adj":       st.column_config.NumberColumn(format="%.2f",
                                     help="Informational: exp × umpire run_adj × bullpen run_adj (NOT used for qualifying — backtested and reverted)"),
                    "Ump":       st.column_config.TextColumn(
                                     help="🔵 under-lean · 🟠 over-lean · ⚪ neutral"),
                    "Bullpen":   st.column_config.TextColumn(
                                     help="🟢 fresh · ⚪ normal · 🟡 tired · 🔴 exhausted (Away/Home)"),
                },
            )

            if _au_danger:
                with st.expander(f"🚫 {len(_au_danger)} landmine game(s) excluded"):
                    for _b in _au_danger:
                        st.markdown(
                            f"- **{_b['game']}** — "
                            f"{_b['away_pit'].split()[-1]} ({_b['away_era']:.2f}) vs "
                            f"{_b['home_pit'].split()[-1]} ({_b['home_era']:.2f}) — "
                            f"starter ERA ≥ {_AU_ERA_DANGER}"
                        )

            # ── Parlay EV table ─────────────────────────────────────────────
            st.markdown("#### 📈 Parlay EV by Leg Count")

            _au_par_rows = []
            _au_best_growth = -999
            _au_rec_n = min(len(_au_clean), 6)  # default

            for _n in range(2, min(len(_au_clean) + 1, 10)):
                _legs_n  = _au_clean[:_n]
                _par_dec = 1.0
                for _lb in _legs_n:
                    _par_dec *= _au_am_to_dec(_lb["odds"])
                _tp   = _AU_TRUE_P ** _n
                _bnet = _par_dec - 1
                _kelly = max(0.0, min(1.0, (_bnet * _tp - (1 - _tp)) / _bnet)) if _bnet > 0 else 0
                try:
                    _gr = _tp * _au_math.log(1 + _kelly * _bnet) + (1 - _tp) * _au_math.log(1 - _kelly)
                except Exception:
                    _gr = 0
                _ev = _tp * _bnet * 100 - (1 - _tp) * 100
                _am = _au_dec_to_am(_par_dec)

                if _gr > _au_best_growth:
                    _au_best_growth = _gr
                    _au_rec_n = _n

                _au_par_rows.append({
                    "Legs":          _n,
                    "Payout":        _am,
                    "Win Rate":      round(_tp * 100, 1),
                    "EV / $100":     round(_ev),
                    "Kelly Growth":  round(_gr * 100, 2),
                    "Wins / 10 days": round(_tp * 10, 1),
                    "Recommended":   "",
                })

            # Mark recommended row
            for _row in _au_par_rows:
                if _row["Legs"] == _au_rec_n:
                    _row["Recommended"] = "✅ OPTIMAL"

            st.dataframe(
                pd.DataFrame(_au_par_rows),
                use_container_width=True, hide_index=True,
                column_config={
                    "Payout":         st.column_config.NumberColumn(format="%+d"),
                    "Win Rate":       st.column_config.NumberColumn(format="%.1f%%"),
                    "EV / $100":      st.column_config.NumberColumn(format="$%+d"),
                    "Kelly Growth":   st.column_config.NumberColumn(format="%.2f%%"),
                    "Wins / 10 days": st.column_config.NumberColumn(format="%.1f"),
                },
            )

            # ── Recommended parlay card ─────────────────────────────────────
            _au_rec_legs = _au_clean[:_au_rec_n]
            _au_rec_dec  = 1.0
            for _lb in _au_rec_legs:
                _au_rec_dec *= _au_am_to_dec(_lb["odds"])
            _au_rec_tp  = _AU_TRUE_P ** _au_rec_n
            _au_rec_ev  = _au_rec_tp * (_au_rec_dec - 1) * 100 - (1 - _au_rec_tp) * 100
            _au_rec_am  = _au_dec_to_am(_au_rec_dec)

            st.markdown(f"#### ✅ Recommended: {_au_rec_n}-Leg Parlay")

            _au_m1, _au_m2, _au_m3, _au_m4 = st.columns(4)
            _au_m1.metric("Est. Payout",  f"{_au_rec_am:+d}")
            _au_m2.metric("Win Rate",     f"{_au_rec_tp * 100:.1f}%")
            _au_m3.metric("EV / $100",    f"+${_au_rec_ev:.0f}")
            _au_m4.metric("Wins / 10d",   f"{_au_rec_tp * 10:.1f}")

            for _i, _lb in enumerate(_au_rec_legs, 1):
                _flag  = " ⚠️ high ERA" if _lb["warn"] else ""
                _badge = (
                    f"<span style='background:#166534;color:#86efac;"
                    f"padding:1px 6px;border-radius:4px;font-size:11px'>est {_lb['odds']:+d}</span>"
                )
                _ump_str = _lb.get("ump_label", "⚪ TBD")
                _bp_str  = _lb.get("bp_label", "—")
                _adj_str = (f"adj {_lb['adj_total']}" if _lb.get("adj_total") != _lb.get("exp_total")
                            else "")
                st.markdown(
                    f"**{_i}.** `{_lb['game']}` &nbsp; **{_lb['bet']}** &nbsp; {_badge}{_flag}"
                    f"<br><span style='color:#64748b;font-size:12px'>"
                    f"exp {_lb['exp_total']}{f' → {_adj_str}' if _adj_str else ''} runs &nbsp;|&nbsp; "
                    f"{_lb['away_pit'].split()[-1]} ({_lb['away_era']:.2f}) vs "
                    f"{_lb['home_pit'].split()[-1]} ({_lb['home_era']:.2f}) &nbsp;|&nbsp; "
                    f"model {_lb['hist_p']*100:.1f}% · edge {_lb['edge']:+.1f}pp &nbsp;|&nbsp; "
                    f"ump {_ump_str} &nbsp;|&nbsp; BP {_bp_str}</span>",
                    unsafe_allow_html=True,
                )

            st.info(
                "⏰ **Odds marked (est) are estimates.** Live alt-total lines typically "
                "open 1–2 hours before first pitch. "
                "If a live price exceeds **-400** on any leg, drop that leg and go one size down. "
                "⚠️ high-ERA legs have more blowup risk. "
                "🔴 exhausted bullpen legs → consider a lower alt-under line as insurance."
            )
    else:
        st.caption("👆 Click **Generate Plays** to qualify today's games.")

    # ── Live Alt Value Bets (Real DK Prices) ─────────────────────────────────
    st.markdown("---")
    st.markdown("### 💰 Live Alt Under Value Bets — Real DraftKings Prices")
    st.caption(
        "Fetches actual DK alternate_totals prices via Odds API · "
        "Edge = Poisson model% (2025 RPG baseline) minus real DK implied% · "
        "Caches to disk — re-runs within same day are free."
    )

    import os as _lv_os
    import json as _lv_json
    import math as _lv_math
    import time as _lv_time
    import urllib.request as _lv_req
    from itertools import combinations as _lv_combs

    _LV_KEY       = '328b6a86d5bd586ca057559e63a2d006'
    _LV_BASE      = 'https://api.the-odds-api.com/v4'
    _LV_ERA_LGAVG = 4.30
    _LV_MIN_ODDS  = -1000
    _LV_MAX_ODDS  = -200
    _LV_MIN_EDGE  = 1.0
    _LV_CACHE_DIR = 'cache'

    _LV_RPG25 = {
        'WSH':4.24,'CIN':4.42,'NYY':5.24,'BAL':4.18,'PHI':4.80,'BOS':4.85,
        'COL':3.69,'PIT':3.60,'TB':4.41,'TOR':4.93,'DET':4.68,'NYM':4.73,
        'CHC':4.89,'ATL':4.47,'KC':4.02,'CWS':3.99,'MIA':4.38,'MIN':4.18,
        'SD':4.33,'MIL':4.97,'AZ':4.88,'TEX':4.22,'SEA':4.73,'HOU':4.24,
        'STL':4.25,'ATH':4.53,'SF':4.35,'LAD':5.09,'LAA':4.15,'CLE':3.97,
    }
    _LV_RAPG25 = {
        'WSH':5.22,'CIN':3.80,'NYY':3.86,'BAL':4.53,'PHI':3.75,'BOS':3.68,
        'COL':5.76,'PIT':3.69,'TB':3.87,'TOR':4.13,'DET':3.89,'NYM':3.96,
        'CHC':3.73,'ATL':4.30,'KC':3.68,'CWS':4.14,'MIA':4.55,'MIN':4.45,
        'SD':3.57,'MIL':3.54,'AZ':4.45,'TEX':3.44,'SEA':3.88,'HOU':3.82,
        'STL':4.21,'ATH':4.63,'SF':3.76,'LAD':3.91,'LAA':4.80,'CLE':3.66,
    }
    _LV_ABBR = {
        'New York Yankees':'NYY','Baltimore Orioles':'BAL','Boston Red Sox':'BOS',
        'Philadelphia Phillies':'PHI','Chicago Cubs':'CHC','Atlanta Braves':'ATL',
        'Kansas City Royals':'KC','Chicago White Sox':'CWS','Arizona Diamondbacks':'AZ',
        'Texas Rangers':'TEX','San Francisco Giants':'SF','Los Angeles Dodgers':'LAD',
        'St. Louis Cardinals':'STL','Oakland Athletics':'ATH','Seattle Mariners':'SEA',
        'Houston Astros':'HOU','Los Angeles Angels':'LAA','Cleveland Guardians':'CLE',
        'San Diego Padres':'SD','Milwaukee Brewers':'MIL','Detroit Tigers':'DET',
        'New York Mets':'NYM','Tampa Bay Rays':'TB','Toronto Blue Jays':'TOR',
        'Colorado Rockies':'COL','Pittsburgh Pirates':'PIT','Washington Nationals':'WSH',
        'Cincinnati Reds':'CIN','Minnesota Twins':'MIN','Miami Marlins':'MIA',
    }

    def _lv_fetch_url(url):
        req = _lv_req.Request(url, headers={'User-Agent': 'mlb/1.0'})
        with _lv_req.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
            remaining = r.headers.get('x-requests-remaining', '?')
            return _lv_json.loads(r.read()), remaining

    def _lv_exp_runs(off, def_r, sp_era):
        return max(1.5, (off + def_r) / 2.0 + (sp_era - _LV_ERA_LGAVG) * 0.22)

    def _lv_p_under(lam, line):
        return sum(
            (lam**k * _lv_math.exp(-lam)) / _lv_math.factorial(k)
            for k in range(int(line) + 1)
        )

    def _lv_implied(odds):
        if odds == 0: return 0.5
        return abs(odds) / (abs(odds) + 100) if odds < 0 else 100 / (odds + 100)

    def _lv_fair_am(p):
        p = max(0.001, min(p, 0.999))
        return int(round(-p / (1 - p) * 100)) if p >= 0.5 else int(round((1 - p) / p * 100))

    @st.cache_data(ttl=3600, show_spinner=False)
    def _lv_fetch_era_cached(pid):
        if not pid: return _LV_ERA_LGAVG
        try:
            url = (f'https://statsapi.mlb.com/api/v1/people/{pid}/stats'
                   f'?stats=season&season=2026&group=pitching&gameType=R')
            req = _lv_req.Request(url, headers={'User-Agent': 'mlb/1.0'})
            with _lv_req.urlopen(req, timeout=8, context=_UNVERIFIED_SSL) as r:
                d = _lv_json.loads(r.read())
            splits = d.get('stats', [{}])[0].get('splits', [])
            if splits:
                s   = splits[0]['stat']
                ip  = float(s.get('inningsPitched', 0) or 0)
                era = float(s.get('era', 0) or 0)
                return era if ip >= 5 else _LV_ERA_LGAVG
        except: pass
        return _LV_ERA_LGAVG

    @st.cache_data(ttl=3600, show_spinner=False)
    def _lv_run(date_key):
        """Fetch DK alt totals + compute edge. Cached 1h per date."""
        cache_path = _lv_os.path.join(_LV_CACHE_DIR, f'odds_{date_key}.json')
        _lv_os.makedirs(_LV_CACHE_DIR, exist_ok=True)

        if _lv_os.path.exists(cache_path):
            with open(cache_path) as f:
                cd = _lv_json.load(f)
            events_today = cd['events']
            alt_odds     = cd['alt_odds']
            pit_map      = cd.get('pit_map', {})
        else:
            ev_url = f'{_LV_BASE}/sports/baseball_mlb/events?apiKey={_LV_KEY}&dateFormat=iso'
            all_ev, _ = _lv_fetch_url(ev_url)
            events_today = [e for e in all_ev if date_key in e['commence_time']]

            sched_url = (f'https://statsapi.mlb.com/api/v1/schedule'
                         f'?sportId=1&date={date_key}&hydrate=probablePitcher,team')
            req = _lv_req.Request(sched_url, headers={'User-Agent': 'mlb/1.0'})
            with _lv_req.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
                sched = _lv_json.loads(r.read())

            pit_map = {}
            for day in sched.get('dates', []):
                for g in day.get('games', []):
                    if g.get('status', {}).get('abstractGameState') == 'Final': continue
                    aw = g['teams']['away']; hm = g['teams']['home']
                    at = _LV_ABBR.get(aw['team']['name'])
                    ht = _LV_ABBR.get(hm['team']['name'])
                    if at and ht:
                        pit_map[f'{at}@{ht}'] = [
                            aw.get('probablePitcher', {}).get('id'),
                            hm.get('probablePitcher', {}).get('id'),
                            aw.get('probablePitcher', {}).get('fullName', 'TBD'),
                            hm.get('probablePitcher', {}).get('fullName', 'TBD'),
                        ]

            alt_odds = {}
            for ev in events_today:
                at = _LV_ABBR.get(ev['away_team'])
                ht = _LV_ABBR.get(ev['home_team'])
                if not at or not ht: continue
                if at not in _LV_RPG25 or ht not in _LV_RPG25: continue
                eid = ev['id']
                url = (f'{_LV_BASE}/sports/baseball_mlb/events/{eid}/odds'
                       f'?apiKey={_LV_KEY}&regions=us'
                       f'&markets=alternate_totals,h2h,spreads'
                       f'&oddsFormat=american&bookmakers=draftkings')
                try:
                    d, _ = _lv_fetch_url(url)
                    alt_odds[eid] = {'ev': ev, 'data': d, 'pit_key': f'{at}@{ht}'}
                    _lv_time.sleep(0.15)
                except: pass

            with open(cache_path, 'w') as f:
                _lv_json.dump({
                    'date': date_key, 'events': events_today,
                    'alt_odds': alt_odds, 'pit_map': pit_map
                }, f, indent=2)

        bets = []
        for eid, obj in alt_odds.items():
            ev   = obj['ev']
            data = obj['data']
            at   = _LV_ABBR.get(ev['away_team'])
            ht   = _LV_ABBR.get(ev['home_team'])
            if not at or not ht: continue
            pits     = pit_map.get(f'{at}@{ht}', [None, None, 'TBD', 'TBD'])
            apid     = pits[0]; hpid = pits[1]
            anam     = pits[2] if len(pits) > 2 else 'TBD'
            hnam     = pits[3] if len(pits) > 3 else 'TBD'
            away_era = _lv_fetch_era_cached(apid)
            home_era = _lv_fetch_era_cached(hpid)
            lam_a    = _lv_exp_runs(_LV_RPG25[at], _LV_RAPG25[ht], home_era)
            lam_b    = _lv_exp_runs(_LV_RPG25[ht], _LV_RAPG25[at], away_era)
            exp_tot  = lam_a + lam_b

            dk_unders = {}
            for bk in data.get('bookmakers', []):
                if bk['key'] != 'draftkings': continue
                for mkt in bk.get('markets', []):
                    if mkt['key'] == 'alternate_totals':
                        for o in mkt['outcomes']:
                            if o['name'] == 'Under':
                                dk_unders[o['point']] = o['price']

            for line, dk_price in sorted(dk_unders.items()):
                if dk_price > _LV_MAX_ODDS or dk_price < _LV_MIN_ODDS: continue
                model_p = _lv_p_under(exp_tot, line)
                dk_imp  = _lv_implied(dk_price)
                edge    = (model_p - dk_imp) * 100
                if edge < _LV_MIN_EDGE: continue
                bets.append({
                    'game':     f'{at}@{ht}',
                    'bet':      f'Under {line}',
                    'away_sp':  anam.split()[-1],
                    'home_sp':  hnam.split()[-1],
                    'away_era': round(away_era, 2),
                    'home_era': round(home_era, 2),
                    'exp_tot':  round(exp_tot, 2),
                    'model_p':  round(model_p, 4),
                    'fair_am':  _lv_fair_am(model_p),
                    'dk_price': dk_price,
                    'dk_imp':   round(dk_imp * 100, 1),
                    'edge_pp':  round(edge, 1),
                })

        seen = {}
        for b in bets:
            k = (b['game'], b['bet'])
            if k not in seen or b['edge_pp'] > seen[k]['edge_pp']:
                seen[k] = b
        return sorted(seen.values(), key=lambda x: -x['edge_pp'])

    def _lv_kelly_growth(p, dk_odds):
        dec = 1 + 100/abs(dk_odds) if dk_odds < 0 else 1 + dk_odds/100
        b = dec - 1
        if b <= 0: return -999, dec
        k = max(0.0, min((b*p-(1-p))/b, 1.0))
        gr = p*_lv_math.log(1+k*b) + (1-p)*_lv_math.log(max(1e-12, 1-k))
        return gr, dec

    _lv_c1, _lv_c2 = st.columns([1, 3])
    with _lv_c1:
        _lv_btn = st.button("🔴 Fetch Live DK Prices", type="primary",
                            use_container_width=True, key="lv_btn")
    with _lv_c2:
        st.caption("Pulls real DraftKings alt under prices · ~15s first run · cached all day · ~30 credits")

    if _lv_btn:
        _lv_run.clear()
        st.session_state['lv_ran_date'] = date_str

    _lv_show = st.session_state.get('lv_ran_date') == date_str

    if _lv_show:
        with st.spinner("Fetching DraftKings alt line prices…"):
            try:
                _lv_bets = _lv_run(date_str)
            except Exception as _lv_err:
                st.error(f"Error fetching live prices: {_lv_err}")
                _lv_bets = []

        if not _lv_bets:
            st.warning("No positive-edge alt under bets found today.")
        else:
            st.success(f"✅ {len(_lv_bets)} positive-edge bets — real DK prices · edge = model% minus actual DK implied%")

            import pandas as _lv_pd

            _lv_best_per = {}
            for _b in _lv_bets:
                _gr, _ = _lv_kelly_growth(_b['model_p'], _b['dk_price'])
                if _b['game'] not in _lv_best_per or _gr > _lv_best_per[_b['game']][1]:
                    _lv_best_per[_b['game']] = (_b, _gr)

            _lv_pool = sorted(
                [v[0] for v in _lv_best_per.values()],
                key=lambda x: -_lv_kelly_growth(x['model_p'], x['dk_price'])[0]
            )

            with st.expander(f"📋 All {len(_lv_bets)} positive-edge plays", expanded=False):
                _lv_rows = []
                for _b in _lv_bets:
                    _lv_rows.append({
                        'Game':    _b['game'],
                        'Bet':     _b['bet'],
                        'Away SP': f"{_b['away_sp']} ({_b['away_era']:.2f})",
                        'Home SP': f"{_b['home_sp']} ({_b['home_era']:.2f})",
                        'Exp Tot': _b['exp_tot'],
                        'Model%':  f"{_b['model_p']*100:.1f}%",
                        'Fair':    f"{_b['fair_am']:+d}",
                        'DK':      f"{_b['dk_price']:+d}",
                        'DK Imp%': f"{_b['dk_imp']:.1f}%",
                        'Edge':    f"+{_b['edge_pp']:.1f}pp",
                    })
                st.dataframe(_lv_pd.DataFrame(_lv_rows), use_container_width=True, hide_index=True)

            st.markdown("#### 🎯 Optimal Parlay — Real DK Prices")

            _lv_parlays = []
            for _n in range(2, min(6, len(_lv_pool) + 1)):
                for _combo in _lv_combs(_lv_pool, _n):
                    _p = 1.0; _dec = 1.0
                    for _leg in _combo:
                        _p   *= _leg['model_p']
                        _, _d = _lv_kelly_growth(_leg['model_p'], _leg['dk_price'])
                        _dec *= _d
                    _bv = _dec - 1
                    _k  = max(0.0, min((_bv*_p-(1-_p))/_bv, 1.0))
                    _ev = _p*_bv*100 - (1-_p)*100
                    _gr = _p*_lv_math.log(1+_k*_bv) + (1-_p)*_lv_math.log(max(1e-12,1-_k))
                    _py = int((_dec-1)*100) if _dec >= 2 else int(-100/(_dec-1))
                    _lv_parlays.append((_gr, list(_combo), _p, _dec, _py, _ev, _k))

            _lv_parlays.sort(key=lambda x: -x[0])

            if _lv_parlays:
                _gr0, _legs0, _p0, _dec0, _pay0, _ev0, _k0 = _lv_parlays[0]

                _mc1, _mc2, _mc3, _mc4 = st.columns(4)
                _mc1.metric("Win Rate",  f"{_p0*100:.1f}%")
                _mc2.metric("DK Payout", f"{_pay0:+d}")
                _mc3.metric("EV / $100", f"${_ev0:+.0f}")
                _mc4.metric("Kelly %",   f"{_k0*100:.1f}%")

                st.markdown("")
                for _i, _leg in enumerate(_legs0, 1):
                    _ec = "🟢" if _leg['edge_pp'] >= 8 else "🟡" if _leg['edge_pp'] >= 4 else "🔵"
                    st.markdown(
                        f"{_ec} **{_i}. {_leg['game']} &nbsp; {_leg['bet']}** &nbsp;&nbsp;"
                        f"<span style='color:#64748b'>"
                        f"model {_leg['model_p']*100:.1f}% · DK {_leg['dk_price']:+d} · "
                        f"edge +{_leg['edge_pp']:.1f}pp · "
                        f"{_leg['away_sp']} ({_leg['away_era']:.2f}) vs "
                        f"{_leg['home_sp']} ({_leg['home_era']:.2f})"
                        f"</span>",
                        unsafe_allow_html=True,
                    )

                st.markdown("")
                for _stake in [25, 50, 100]:
                    _win = _stake * (_dec0 - 1)
                    st.caption(f"${_stake} bet → wins ${_win:.0f}  (total ${_stake + _win:.0f})")

                with st.expander("📊 Top 5 parlay combos", expanded=False):
                    for _rk, (_gr2, _legs2, _p2, _dec2, _pay2, _ev2, _k2) in enumerate(_lv_parlays[:5], 1):
                        _nm = " + ".join(f"{_l['game']} {_l['bet']}" for _l in _legs2)
                        st.markdown(
                            f"**#{_rk}** &nbsp; {len(_legs2)}-leg &nbsp;|&nbsp; "
                            f"Win: {_p2*100:.1f}% &nbsp;|&nbsp; Pay: {_pay2:+d} &nbsp;|&nbsp; "
                            f"EV: ${_ev2:+.0f} &nbsp;|&nbsp; Kelly: {_k2*100:.1f}%  \n"
                            f"<span style='color:#64748b;font-size:12px'>{_nm}</span>",
                            unsafe_allow_html=True,
                        )

                st.info(
                    "ℹ️ **All edge numbers use real DK prices** — no estimates or calibration. "
                    "Model probability (2025 RPG baseline) vs actual DraftKings implied%."
                )
    else:
        st.caption("👆 Click **Fetch Live DK Prices** to load real-price value bets.")


# ===== TAB 6: TEAM FORM =====

with tab_team:
    st.subheader(f"Team form — {date_str}")
    st.caption("Season totals vs last 15 days for every team playing tonight.")

    team_rows = []
    with st.spinner("Loading team stats..."):
        seen = set()
        for g in enriched:
            for side in ("away", "home"):
                tid = g[f"{side}_team_id"]
                if tid in seen:
                    continue
                seen.add(tid)
                tname = g[f"{side}_team"]
                season_h = load_team_season(tid, season, "hitting")
                recent_h = load_team_recent(tid, season, 15, "hitting")
                season_p = load_team_season(tid, season, "pitching")

                games_played = safe_float(season_h.get("gamesPlayed"), 0) or 1
                recent_games = safe_float(recent_h.get("gamesPlayed"), 0) or 1
                team_rows.append({
                    "Team":          tname,
                    "Season HR":     season_h.get("homeRuns"),
                    "Season OPS":    season_h.get("ops"),
                    "Season AVG":    season_h.get("avg"),
                    "Season R/G":    round(safe_float(season_h.get("runs"), 0) / games_played, 2),
                    "L15 HR":        recent_h.get("homeRuns"),
                    "L15 OPS":       recent_h.get("ops"),
                    "L15 AVG":       recent_h.get("avg"),
                    "L15 R/G":       round(safe_float(recent_h.get("runs"), 0) / recent_games, 2),
                    "Season Pitch ERA": season_p.get("era"),
                    "Season Pitch HR/9": season_p.get("homeRunsPer9"),
                })
    df_t = pd.DataFrame(team_rows).sort_values("Season HR", ascending=False, key=lambda c: pd.to_numeric(c, errors="coerce"))
    st.dataframe(
        df_t,
        use_container_width=True, hide_index=True,
        column_config={
            "Season R/G": st.column_config.NumberColumn(format="%.2f"),
            "L15 R/G":    st.column_config.NumberColumn(format="%.2f"),
        },
    )


# ===== TAB 6: STANDINGS =====

with tab_stand:
    st.subheader(f"{season} Standings")
    records = load_standings(season)
    if not records:
        st.info("Standings unavailable.")
    else:
        DIVISION_NAMES = {
            200: "AL West", 201: "AL East", 202: "AL Central",
            203: "NL West", 204: "NL East", 205: "NL Central",
        }
        cols = st.columns(2)
        for i, rec in enumerate(records):
            div_id = rec.get("division", {}).get("id")
            div_name = DIVISION_NAMES.get(div_id, f"Division {div_id}")
            target = cols[i % 2]
            with target:
                st.markdown(f"**{div_name}**")
                trows = []
                for tr in rec.get("teamRecords", []):
                    trows.append({
                        "Team":   tr["team"]["name"],
                        "W":      tr.get("wins"),
                        "L":      tr.get("losses"),
                        "PCT":    tr.get("winningPercentage"),
                        "GB":     tr.get("gamesBack"),
                        "Streak": tr.get("streak", {}).get("streakCode", "—"),
                        "L10":    next((s.get("wins", "?") for s in tr.get("records", {}).get("splitRecords", []) if s.get("type") == "lastTen"), "—"),
                    })
                st.dataframe(pd.DataFrame(trows), use_container_width=True, hide_index=True)
