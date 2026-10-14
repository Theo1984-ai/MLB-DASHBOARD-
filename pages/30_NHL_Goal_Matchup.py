"""
NHL Goal Matchup Scores

Ranks anytime goal scorer candidates by composite score:
  Odds API true probability + team implied total + NHL DEF rank + player form
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

st.set_page_config(page_title="NHL Goal Matchup", page_icon="🏒", layout="wide")
st.title("🏒 NHL Goal Matchup Scores")
st.caption("Anytime goal scorer (YES/NO) true prob · team implied total · NHL defensive rank · player form")


# ─── API key ─────────────────────────────────────────────────────────────────

def _api_key():
    try:
        if "THE_ODDS_API_KEY" in st.secrets:
            return st.secrets["THE_ODDS_API_KEY"]
    except Exception:
        pass
    return os.environ.get("THE_ODDS_API_KEY")


# ─── Cached scan ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=600, show_spinner=False)
def run_scan(fetch_nhl_data):
    from scripts.nhl_goal_scanner import scan
    try:
        return scan(_api_key(), fetch_nhl_data=fetch_nhl_data)
    except Exception as e:
        return []


# ─── Controls ────────────────────────────────────────────────────────────────

col_a, col_b, col_c, col_d, col_e = st.columns([1, 1, 1, 1, 2])

with col_a:
    pos_filter = st.multiselect(
        "Position",
        ["F", "D"],
        default=[],
        placeholder="All positions",
        label_visibility="collapsed",
    )

with col_b:
    min_score = st.selectbox(
        "Min Score",
        [0, 50, 60, 70],
        format_func=lambda x: "All scores" if x == 0 else f"{x}+ only",
        label_visibility="collapsed",
    )

with col_c:
    fetch_nhl = st.toggle("NHL Stats", value=True,
                           help="Load NHL roster + player stats + DEF rankings (first load ~5s, then cached)")

with col_d:
    pass  # spacer

with col_e:
    refresh = st.button("🔄 Refresh", use_container_width=False)
    if refresh:
        run_scan.clear()
        try:
            from scripts.nhl_goal_scanner import _ROSTER_INDEX, _SKATER_STATS
            import scripts.nhl_goal_scanner as _mod
            _mod._ROSTER_INDEX = None
            _mod._SKATER_STATS = None
        except Exception:
            pass

key = _api_key()
if not key:
    st.error("THE_ODDS_API_KEY not configured in secrets.toml")
    st.stop()

# ─── Load data ───────────────────────────────────────────────────────────────

with st.spinner("Fetching NHL goal scorer props…"):
    rows = run_scan(fetch_nhl)

if not rows:
    now_et = datetime.now(tz=EASTERN)
    day    = now_et.strftime("%A")
    st.info(
        f"**No NHL anytime goal scorer props posted yet** ({day}). "
        "Props are usually available day-of or the day before puck drop."
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
hot   = (df["score"] >= 70).sum()
good  = ((df["score"] >= 50) & (df["score"] < 70)).sum()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Players",        total)
m2.metric("🔥 Hot (70+)",    hot)
m3.metric("✅ Good (50-69)", good)
m4.metric("Games",          df["game"].nunique())

st.divider()

# ─── Helpers ─────────────────────────────────────────────────────────────────

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
    if rank is None:
        return "—"
    if rank <= 10:
        return f"🔴 #{rank}"
    if rank <= 21:
        return f"🟡 #{rank}"
    return f"🟢 #{rank}"


def pos_badge_label(pos):
    labels = {"F": "Forward", "D": "Defense", "G": "Goalie"}
    return labels.get(pos, pos or "?")


# ─── Build display dataframe ─────────────────────────────────────────────────

display_rows = []
for _, r in df.iterrows():
    display_rows.append({
        "#":          len(display_rows) + 1,
        "Player":     r["player"],
        "Pos":        r.get("pos", ""),
        "Game":       r["game"],
        "Time":       r.get("game_time", ""),
        "Goal Score": r["score"],
        "True Prob":  r["true_prob"],
        "Best Odds":  price_str(r.get("best_price")),
        "Book":       _book_label(r.get("best_book")),
        "# Books":    r.get("n_books", 0),
        "Team Total": r.get("team_total", 0),
        "DEF Rank":   def_rank_str(r.get("def_rank")),
        "G/G":        round(r["goals_pg"], 2) if r.get("goals_pg") is not None else None,
        "S/G":        r.get("shots_pg"),
        "GP":         r.get("gp"),
    })

display_df = pd.DataFrame(display_rows)

# ─── Tabs ─────────────────────────────────────────────────────────────────────

tab_all, tab_game, tab_pos = st.tabs(["📋 All Players", "🏟 By Game", "🎯 By Position"])

with tab_all:
    _show_cols = [
        "#", "Player", "Pos", "Game", "Time",
        "Goal Score", "True Prob", "Best Odds", "Book",
        "# Books", "Team Total", "DEF Rank", "G/G", "S/G",
    ]
    _df_show = display_df[_show_cols].copy()
    _df_show["True Prob"]  = _df_show["True Prob"].map(lambda x: f"{x:.1f}%")
    _df_show["Team Total"] = _df_show["Team Total"].map(lambda x: f"{x:.1f}" if x else "—")
    _df_show["G/G"]        = _df_show["G/G"].map(lambda x: f"{x:.2f}" if x is not None else "—")
    _df_show["S/G"]        = _df_show["S/G"].map(lambda x: f"{x:.1f}" if x is not None else "—")

    def _row_style(row):
        score = display_rows[row.name]["Goal Score"]
        if score >= 70:
            bg = "background-color: rgba(63,185,80,0.08)"
        elif score >= 50:
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
            "#":           st.column_config.NumberColumn(width="small"),
            "Goal Score":  st.column_config.ProgressColumn(
                "Goal Score", min_value=0, max_value=100, format="%d"
            ),
            "True Prob":   st.column_config.TextColumn("True Prob %", width="small"),
            "Team Total":  st.column_config.TextColumn("Impl Total", width="small"),
            "DEF Rank":    st.column_config.TextColumn("DEF Rank", width="small",
                               help="🔴 #1-10 = most goals allowed (best matchup) · 🟢 #22-32 = toughest"),
        },
        height=min(60 + len(_df_show) * 35, 750),
    )

    st.caption(
        "**Goal Score formula** — "
        "Full: 40% true prob + 25% team total + 20% DEF rank + 15% G/G  |  "
        "No player stats: 50/30/20  |  "
        "Odds only: 65/35  |  "
        "DEF Rank: 🔴 #1–10 = most goals allowed · 🟢 #22–32 = fewest  |  "
        "Books: DraftKings · FanDuel · Bovada · BetRivers · Pinnacle · ESPN Bet · BetOnline"
    )

with tab_game:
    for game_name in df["game"].unique():
        gdf    = df[df["game"] == game_name]
        gtime  = gdf.iloc[0]["game_time"]
        ht     = gdf.iloc[0]["home_total"]
        at     = gdf.iloc[0]["away_total"]

        with st.expander(
            f"**{game_name}**  ·  {gtime}  ·  "
            f"Totals: H {ht:.1f} / A {at:.1f}",
            expanded=True,
        ):
            grp = []
            for _, r in gdf.iterrows():
                grp.append({
                    "Player":    r["player"],
                    "Pos":       r.get("pos", ""),
                    "Goal Score": r["score"],
                    "True Prob": f"{r['true_prob']:.1f}%",
                    "Best Odds": price_str(r.get("best_price")),
                    "Book":      _book_label(r.get("best_book")),
                    "# Books":   r.get("n_books", 0),
                    "DEF Rank":  def_rank_str(r.get("def_rank")),
                    "G/G":       f"{r['goals_pg']:.2f}" if r.get("goals_pg") is not None else "—",
                    "S/G":       f"{r['shots_pg']:.1f}" if r.get("shots_pg") is not None else "—",
                })
            st.dataframe(
                pd.DataFrame(grp),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Goal Score": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%d"
                    ),
                },
            )

with tab_pos:
    for pos in ["F", "D", ""]:
        pdf = df[df["pos"] == pos] if pos else df[~df["pos"].isin(["F", "D"])]
        if pdf.empty:
            continue
        label = pos_badge_label(pos) if pos else "Unknown"
        with st.expander(f"**{label}** — {len(pdf)} players", expanded=(pos == "F")):
            pr = []
            for _, r in pdf.iterrows():
                pr.append({
                    "Player":    r["player"],
                    "Game":      r["game"],
                    "Time":      r.get("game_time", ""),
                    "Goal Score": r["score"],
                    "True Prob": f"{r['true_prob']:.1f}%",
                    "Best Odds": price_str(r.get("best_price")),
                    "Book":      _book_label(r.get("best_book")),
                    "Team Total":f"{r['team_total']:.1f}" if r.get("team_total") else "—",
                    "DEF Rank":  def_rank_str(r.get("def_rank")),
                    "G/G":       f"{r['goals_pg']:.2f}" if r.get("goals_pg") is not None else "—",
                })
            st.dataframe(
                pd.DataFrame(pr),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Goal Score": st.column_config.ProgressColumn(
                        min_value=0, max_value=100, format="%d"
                    ),
                },
            )

st.divider()
st.caption(
    f"Refreshed {datetime.now(tz=EASTERN).strftime('%I:%M %p ET')}  ·  "
    f"Props cache: 10 min  ·  "
    f"NHL rosters cached 7 days  ·  "
    f"Player stats fetched per session  ·  "
    f"DEF rank from NHL.com standings"
)
