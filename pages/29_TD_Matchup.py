"""
TD Matchup Scores — NFL & CFB

Ranks anytime TD scorer candidates by composite score:
  Odds API true probability + team implied total + Sleeper RZ opportunities
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="TD Matchup Scores", page_icon="🏈", layout="wide")
st.title("🏈 TD Matchup Scores")
st.caption("Composite TD probability — anytime TD scorer true prob · team implied total · red zone opportunities")


# ─── API key ─────────────────────────────────────────────────────────────────

def _api_key():
    try:
        if "THE_ODDS_API_KEY" in st.secrets:
            return st.secrets["THE_ODDS_API_KEY"]
    except Exception:
        pass
    return os.environ.get("THE_ODDS_API_KEY")


# ─── Scanner (no ESPN inside — ESPN loads separately below) ──────────────────

@st.cache_data(ttl=600, show_spinner=False)
def run_scan(leagues, fetch_wx):
    from scripts.td_matchup_scanner import scan
    rows = []
    if "NFL" in leagues:
        try:
            rows += scan(_api_key(), sport="nfl", fetch_weather_data=fetch_wx)
        except Exception as e:
            rows  # scan failed silently; error surfaced via st.warning below
    if "CFB" in leagues:
        try:
            rows += scan(_api_key(), sport="ncaaf", fetch_weather_data=fetch_wx)
        except Exception as e:
            pass
    return rows


# ─── ESPN DEF rankings — separate daily cache, never blocks the main scan ────

@st.cache_data(ttl=86400, show_spinner=False)
def load_def_rankings():
    """Returns {abbrev: {…ranks…}} or {} on any failure. Cached 24 h."""
    try:
        from scripts.espn_def_rankings import fetch_nfl_def_rankings
        return fetch_nfl_def_rankings()
    except Exception:
        return {}


# ─── Controls ────────────────────────────────────────────────────────────────

col_a, col_b, col_c, col_d, col_e, col_f = st.columns([1, 1, 1, 1, 1, 2])

with col_a:
    leagues = st.multiselect(
        "League",
        ["NFL", "CFB"],
        default=["NFL", "CFB"],
        label_visibility="collapsed",
    )
    if not leagues:
        leagues = ["NFL"]

with col_b:
    pos_filter = st.multiselect(
        "Position",
        ["QB", "RB", "WR", "TE"],
        default=[],
        placeholder="All positions",
        label_visibility="collapsed",
    )

with col_c:
    min_score = st.selectbox(
        "Min Score",
        [0, 60, 70, 80],
        format_func=lambda x: "All scores" if x == 0 else f"{x}+ only",
        label_visibility="collapsed",
    )

with col_d:
    fetch_wx = st.toggle("Weather", value=True)

with col_e:
    fetch_espn_def = st.toggle("DEF Rank", value=True,
                                help="Load ESPN defensive rankings by position (NFL only, ~10s first load, cached daily)")

with col_f:
    refresh = st.button("🔄 Refresh", use_container_width=False)
    if refresh:
        run_scan.clear()
        load_def_rankings.clear()
        try:
            from scripts.espn_def_rankings import clear_cache as _espn_clear
            _espn_clear()
        except Exception:
            pass

key = _api_key()
if not key:
    st.error("THE_ODDS_API_KEY not configured in secrets.toml")
    st.stop()

# ─── Load data ───────────────────────────────────────────────────────────────

with st.spinner("Fetching TD props and team totals…"):
    rows = run_scan(tuple(sorted(leagues)), fetch_wx)

# ESPN DEF rankings load independently — never blocks rows from showing
def_rankings = {}
if fetch_espn_def and "NFL" in leagues:
    with st.spinner("Loading ESPN DEF rankings (first load ~10s, then cached daily)…"):
        def_rankings = load_def_rankings()

# Inject def_rank into each row (display-only, does not change TD Score)
if def_rankings:
    try:
        from scripts.espn_def_rankings import get_def_rank as _get_dr
        for row in rows:
            opp = row.get("opponent_abbrev")
            pos = row.get("pos", "")
            if opp:
                rank, label = _get_dr(def_rankings, opp, pos)
                row["def_rank"]       = rank
                row["def_rank_label"] = label
    except Exception:
        pass

if not rows:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    day = now_et.strftime("%A")
    st.info(
        f"**No TD scorer props posted yet** ({day}). "
        "Sportsbooks typically release anytime TD scorer markets Wednesday–Thursday "
        "for the upcoming NFL weekend, and Thursday–Friday for CFB. "
        "Check back later in the week."
    )
    st.stop()

# ─── Filter ──────────────────────────────────────────────────────────────────

df = pd.DataFrame(rows)

if pos_filter:
    df = df[df["pos"].isin(pos_filter)]

if min_score > 0:
    df = df[df["score"] >= min_score]

df = df.sort_values("score", ascending=False).reset_index(drop=True)

if df.empty:
    st.info("No players match the current filters.")
    st.stop()

# ─── Summary strip ───────────────────────────────────────────────────────────

total = len(df)
hot   = (df["score"] >= 80).sum()
good  = ((df["score"] >= 60) & (df["score"] < 80)).sum()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Players", total)
m2.metric("🔥 Hot (80+)", hot)
m3.metric("✅ Good (60-79)", good)
games_today = df["game"].nunique()
m4.metric("Games", games_today)

st.divider()

# ─── Score bar helper ─────────────────────────────────────────────────────────

def score_bar(score):
    if score >= 80:
        color = "#3fb950"
    elif score >= 60:
        color = "#d29922"
    else:
        color = "#6e7681"
    pct = min(score, 100)
    return (
        f'<div style="display:flex;align-items:center;gap:6px;justify-content:flex-end">'
        f'<div style="background:#21262d;border-radius:2px;height:6px;width:56px;overflow:hidden">'
        f'<div style="background:{color};height:100%;width:{pct}%;border-radius:2px"></div></div>'
        f'<span style="color:{color};font-weight:700;font-size:16px;min-width:28px;text-align:right">'
        f'{score}</span></div>'
    )


def form_dots(form_list):
    dots = []
    for f in (form_list or [0, 0, 0]):
        c = "#3fb950" if f else "#30363d"
        dots.append(f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{c}"></span>')
    return " ".join(dots)


_BOOK_ABBREVS = {
    "draftkings":  "DK",
    "fanduel":     "FD",
    "bovada":      "BOV",
    "betrivers":   "BRV",
    "pinnacle":    "PIN",
    "espnbet":     "ESPN",
    "betonlineag": "BOL",
}

def _book_label(key):
    return _BOOK_ABBREVS.get(key or "", (key or "").upper()[:4])


def price_str(p):
    if p is None:
        return ""
    return f"+{int(p)}" if p > 0 else str(int(p))


def def_rank_str(rank):
    """Format defensive rank with color emoji: 1-10 🔴 (weak), 11-21 🟡, 22-32 🟢 (tough)."""
    if rank is None:
        return "—"
    if rank <= 10:
        icon = "🔴"
    elif rank <= 21:
        icon = "🟡"
    else:
        icon = "🟢"
    return f"{icon} #{rank}"


def pos_badge(pos):
    colors = {
        "QB": ("bg:#152540", "color:#79b8ff"),
        "RB": ("bg:#152510", "color:#56d364"),
        "WR": ("bg:#2a1535", "color:#d2a8ff"),
        "TE": ("bg:#351f0a", "color:#ffa657"),
    }
    c = colors.get(pos, ("bg:#21262d", "color:#8b949e"))
    bg = c[0].split(":")[1]
    fg = c[1].split(":")[1]
    if not pos:
        return ""
    return (
        f'<span style="background:{bg};color:{fg};border-radius:3px;'
        f'font-size:9px;font-weight:700;padding:2px 5px;'
        f'letter-spacing:0.4px;text-transform:uppercase">{pos}</span>'
    )


# ─── Build display dataframe ─────────────────────────────────────────────────

display_rows = []
for i, r in df.iterrows():
    display_rows.append({
        "#":           len(display_rows) + 1,
        "Player":      r["player"],
        "Pos":         r.get("pos", ""),
        "Game":        r["game"],
        "Time":        r.get("game_time", ""),
        "TD Score":    r["score"],
        "True Prob":   r["true_prob"],
        "Best Odds":   price_str(r.get("best_price")),
        "Book":        _book_label(r.get("best_book")),
        "# Books":     r.get("n_books", 0),
        "Team Total":  r.get("team_total", 0),
        "DEF Rank":    def_rank_str(r.get("def_rank")),
        "RZ Opp/G":    r.get("rz_opp"),
        "Snaps/G":     r.get("snap_avg"),
        "Weather":     r.get("weather", ""),
        "Form":        r.get("form", [0, 0, 0]),
    })

display_df = pd.DataFrame(display_rows)

# ─── Tabs: table view + game grouping ────────────────────────────────────────

tab_all, tab_game, tab_pos = st.tabs(["📋 All Players", "🏟 By Game", "🎯 By Position"])

with tab_all:
    _show_cols = [
        "#", "Player", "Pos", "Game", "Time",
        "TD Score", "True Prob", "Best Odds", "Book",
        "# Books", "Team Total", "DEF Rank", "RZ Opp/G", "Snaps/G", "Weather",
    ]
    _df_show = display_df[_show_cols].copy()
    _df_show["True Prob"] = _df_show["True Prob"].map(lambda x: f"{x:.1f}%")
    _df_show["Team Total"] = _df_show["Team Total"].map(lambda x: f"{x:.1f}" if x else "—")
    _df_show["RZ Opp/G"] = _df_show["RZ Opp/G"].map(
        lambda x: f"{x:.1f}" if x is not None and x == x else "—"
    )
    _df_show["Snaps/G"] = _df_show["Snaps/G"].map(
        lambda x: str(int(x)) if x is not None and x == x else "—"
    )

    def _row_style(row):
        score = display_rows[row.name]["TD Score"]
        if score >= 80:
            bg = "background-color: rgba(63,185,80,0.08)"
        elif score >= 60:
            bg = "background-color: rgba(210,153,34,0.06)"
        else:
            bg = ""
        return [bg] * len(row)

    styled = _df_show.style.apply(_row_style, axis=1)
    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config={
            "#":          st.column_config.NumberColumn(width="small"),
            "TD Score":   st.column_config.ProgressColumn(
                "TD Score", min_value=0, max_value=100, format="%d"
            ),
            "True Prob":  st.column_config.TextColumn("True Prob %", width="small"),
            "Team Total": st.column_config.TextColumn("Impl Total", width="small"),
        },
        height=min(60 + len(_df_show) * 35, 750),
    )

    st.caption(
        "**TD Score formula** — "
        "Full (RZ + DEF): 35% true prob + 20% team total + 20% RZ opps/G + 15% RZ share + 10% DEF rank  |  "
        "No DEF: 40/25/20/15  |  "
        "Odds only: 65% true prob + 35% team total  |  "
        "DEF Rank: 🔴 #1–10 = weakest (best matchup) · 🟡 #11–21 · 🟢 #22–32 = toughest  |  "
        "Books: DraftKings · FanDuel · Bovada · BetRivers · Pinnacle"
    )

with tab_game:
    for game_name in df["game"].unique():
        game_df = df[df["game"] == game_name].copy()
        gtime   = game_df.iloc[0]["game_time"] if len(game_df) else ""
        ghome   = game_df.iloc[0]["home"]
        ht      = game_df.iloc[0]["home_total"]
        at      = game_df.iloc[0]["away_total"]
        wx      = game_df.iloc[0].get("weather", "")
        wind    = game_df.iloc[0].get("wind_mph", 0)

        wind_warn = f" ⚠️ {wind}mph wind" if (wind or 0) >= 20 else ""
        with st.expander(
            f"**{game_name}**  ·  {gtime}  ·  "
            f"Totals: H {ht:.1f} / A {at:.1f}  ·  {wx}{wind_warn}",
            expanded=True,
        ):
            grp_rows = []
            for _, r in game_df.iterrows():
                grp_rows.append({
                    "Player":     r["player"],
                    "Pos":        r.get("pos", ""),
                    "TD Score":   r["score"],
                    "True Prob":  f"{r['true_prob']:.1f}%",
                    "Best Odds":  price_str(r.get("best_price")),
                    "Book":       _book_label(r.get("best_book")),
                    "# Books":    r.get("n_books", 0),
                    "RZ Opp/G":   f"{r['rz_opp']:.1f}" if r.get("rz_opp") is not None else "—",
                    "Snaps/G":    str(int(r["snap_avg"])) if r.get("snap_avg") is not None else "—",
                })
            gdf = pd.DataFrame(grp_rows)
            st.dataframe(
                gdf,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "TD Score": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%d"
                    ),
                },
            )

with tab_pos:
    pos_order = ["QB", "RB", "WR", "TE", ""]
    for pos in pos_order:
        pos_df = df[df["pos"] == pos] if pos else df[~df["pos"].isin(["QB","RB","WR","TE"])]
        if pos_df.empty:
            continue
        label = pos if pos else "Other/Unknown"
        with st.expander(f"**{label}** — {len(pos_df)} players", expanded=(pos in ("RB","WR"))):
            pr = []
            for _, r in pos_df.iterrows():
                pr.append({
                    "Player":    r["player"],
                    "Game":      r["game"],
                    "Time":      r.get("game_time", ""),
                    "TD Score":  r["score"],
                    "True Prob": f"{r['true_prob']:.1f}%",
                    "Best Odds": price_str(r.get("best_price")),
                    "Book":      _book_label(r.get("best_book")),
                    "Team Total":f"{r['team_total']:.1f}" if r.get("team_total") else "—",
                    "RZ Opp/G": f"{r['rz_opp']:.1f}" if r.get("rz_opp") is not None else "—",
                    "Weather":   r.get("weather", ""),
                })
            pdf = pd.DataFrame(pr)
            st.dataframe(
                pdf, use_container_width=True, hide_index=True,
                column_config={
                    "TD Score": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%d"
                    ),
                },
            )

st.divider()
st.caption(
    f"Refreshed {datetime.now(tz=EASTERN).strftime('%I:%M %p ET')}  ·  "
    f"Props cache: 10 min  ·  "
    f"Sleeper stats: last 3 weeks averaged  ·  "
    f"ESPN DEF rankings: cached 24 h (NFL only)"
)
