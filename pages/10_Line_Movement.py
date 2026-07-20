"""
📈 Line Movement — DraftKings team totals tracked hourly.

Reads team_totals_history/YYYY-MM-DD.json (populated by
scripts/team_totals_snapshot.py running hourly 11 AM - 10 PM ET).

Shows for each team:
  - Opening line + price (11 AM snapshot)
  - Current line + price (latest snapshot)
  - Δ line (movement in runs)
  - Δ price (movement in cents)
Highlights meaningful moves (≥0.5 runs or ≥20 cents).
"""
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")
HISTORY_DIR = os.path.join(ROOT, "team_totals_history")

st.set_page_config(page_title="Line Movement", page_icon="📈", layout="wide")
st.title("📈 Team Totals — Line Movement")
st.caption(
    "Hourly DraftKings team-total snapshots from **11 AM to 10 PM ET**. "
    "Track how lines move through the day to spot steam moves and reverse "
    "line movement. Opening = first snapshot of the day; Current = most "
    "recent."
)


# ---------- Load today's file ----------

def _load_day(date_str):
    path = os.path.join(HISTORY_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# Date selector — default today
today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
available_dates = []
if os.path.isdir(HISTORY_DIR):
    for fn in sorted(os.listdir(HISTORY_DIR), reverse=True):
        if fn.endswith(".json"):
            available_dates.append(fn[:-5])

if not available_dates:
    st.info(
        "No team-totals snapshots yet. The hourly cron starts at 11 AM ET. "
        "If it's before then today, check back after 11:00. If it's after "
        "and you still see this, the cron may not be scheduled yet."
    )
    st.stop()

sel_date = st.selectbox("📅 Date", options=available_dates, index=0)
payload = _load_day(sel_date)
if not payload:
    st.error(f"Could not read {sel_date}.json")
    st.stop()

snapshots = payload.get("snapshots", []) or []
if not snapshots:
    st.info(f"No snapshots saved for {sel_date} yet.")
    st.stop()

# Summary strip
c1, c2, c3, c4 = st.columns(4)
c1.metric("Snapshots today", len(snapshots))
try:
    first_t = datetime.fromisoformat(snapshots[0]["captured_at"]).strftime("%I:%M %p")
except Exception:
    first_t = "?"
try:
    last_t = datetime.fromisoformat(snapshots[-1]["captured_at"]).strftime("%I:%M %p")
except Exception:
    last_t = "?"
c2.metric("First snapshot", first_t)
c3.metric("Latest snapshot", last_t)
c4.metric("Games tracked", snapshots[-1].get("n_games", 0))

st.divider()


# ---------- Build the delta table ----------

opening = snapshots[0].get("games", [])
current = snapshots[-1].get("games", [])
opening_map = {g["game"]: g for g in opening}
current_map = {g["game"]: g for g in current}

all_games = sorted(set(opening_map) | set(current_map))

def _delta_price(open_p, cur_p):
    """American odds delta (in 'cents' change). Both same sign expected."""
    if open_p is None or cur_p is None:
        return None
    return cur_p - open_p


def _emoji(dl, dp):
    """Flag meaningful movement."""
    if dl is None: dl = 0
    if dp is None: dp = 0
    if abs(dl) >= 0.5 or abs(dp) >= 20:
        return "🔥"
    if abs(dl) >= 0.25 or abs(dp) >= 10:
        return "📈" if (dl > 0 or dp > 0) else "📉"
    return ""


rows = []
for game in all_games:
    o = opening_map.get(game, {})
    c = current_map.get(game, {})
    o_away = o.get("away", {}) if o else {}
    o_home = o.get("home", {}) if o else {}
    c_away = c.get("away", {}) if c else {}
    c_home = c.get("home", {}) if c else {}
    # Away team row
    dl_a = None
    if c_away.get("line") is not None and o_away.get("line") is not None:
        dl_a = c_away["line"] - o_away["line"]
    dp_a_over = _delta_price(o_away.get("over_price"), c_away.get("over_price"))
    dp_a_under = _delta_price(o_away.get("under_price"), c_away.get("under_price"))
    rows.append({
        "Game":    game,
        "Team":    (c.get("away_team") or o.get("away_team") or "?"),
        "Open":    o_away.get("line"),
        "Current": c_away.get("line"),
        "Δ Line":  dl_a,
        "Over Open":     o_away.get("over_price"),
        "Over Current":  c_away.get("over_price"),
        "Δ Over":        dp_a_over,
        "Under Open":    o_away.get("under_price"),
        "Under Current": c_away.get("under_price"),
        "Δ Under":       dp_a_under,
        "Move":    _emoji(dl_a, max(abs(dp_a_over or 0), abs(dp_a_under or 0)) * (1 if (dp_a_over or 0) + (dp_a_under or 0) >= 0 else -1)),
    })
    # Home team row
    dl_h = None
    if c_home.get("line") is not None and o_home.get("line") is not None:
        dl_h = c_home["line"] - o_home["line"]
    dp_h_over = _delta_price(o_home.get("over_price"), c_home.get("over_price"))
    dp_h_under = _delta_price(o_home.get("under_price"), c_home.get("under_price"))
    rows.append({
        "Game":    game,
        "Team":    (c.get("home_team") or o.get("home_team") or "?"),
        "Open":    o_home.get("line"),
        "Current": c_home.get("line"),
        "Δ Line":  dl_h,
        "Over Open":     o_home.get("over_price"),
        "Over Current":  c_home.get("over_price"),
        "Δ Over":        dp_h_over,
        "Under Open":    o_home.get("under_price"),
        "Under Current": c_home.get("under_price"),
        "Δ Under":       dp_h_under,
        "Move":    _emoji(dl_h, max(abs(dp_h_over or 0), abs(dp_h_under or 0)) * (1 if (dp_h_over or 0) + (dp_h_under or 0) >= 0 else -1)),
    })

df = pd.DataFrame(rows)

# Filter controls
fc1, fc2 = st.columns([1, 4])
with fc1:
    only_moves = st.toggle("🔥 Only show movement",
                           help="Hide teams where line & price haven't moved")

if only_moves and not df.empty:
    df = df[(df["Δ Line"].fillna(0) != 0) |
            (df["Δ Over"].fillna(0) != 0) |
            (df["Δ Under"].fillna(0) != 0)]

st.dataframe(
    df, use_container_width=True, hide_index=True,
    column_config={
        "Open":          st.column_config.NumberColumn(format="%.1f"),
        "Current":       st.column_config.NumberColumn(format="%.1f"),
        "Δ Line":        st.column_config.NumberColumn(format="%+.1f"),
        "Over Open":     st.column_config.NumberColumn(format="%+d"),
        "Over Current":  st.column_config.NumberColumn(format="%+d"),
        "Δ Over":        st.column_config.NumberColumn(format="%+d"),
        "Under Open":    st.column_config.NumberColumn(format="%+d"),
        "Under Current": st.column_config.NumberColumn(format="%+d"),
        "Δ Under":       st.column_config.NumberColumn(format="%+d"),
    },
)

# ---------- Detailed movement chart per team ----------
st.divider()
st.subheader("📊 Line trajectory")

team_options = sorted({g["game"] for g in current} | {g["game"] for g in opening})
if team_options:
    sel_game = st.selectbox("Pick a game to see hour-by-hour", team_options)
    if sel_game:
        # Build time series: for each snapshot, get the game's away/home lines
        rows_ts = []
        for snap in snapshots:
            t = snap.get("captured_at")
            for g in snap.get("games", []):
                if g["game"] != sel_game: continue
                rows_ts.append({
                    "Time":       t,
                    f"{g['away_team']} line": g.get("away", {}).get("line"),
                    f"{g['home_team']} line": g.get("home", {}).get("line"),
                })
        if rows_ts:
            ts_df = pd.DataFrame(rows_ts)
            ts_df["Time"] = pd.to_datetime(ts_df["Time"], errors="coerce")
            ts_df = ts_df.set_index("Time")
            st.line_chart(ts_df)
        else:
            st.caption("No time-series data for this game yet.")


st.divider()
st.caption(
    f"Data source: DraftKings via The Odds API · "
    f"Book: {payload.get('book','?')} · "
    f"Market: {payload.get('market','?')} · "
    f"Snapshots merged: {payload.get('n_snapshots', len(snapshots))}"
)
