"""
📊 Team Totals — FanDuel vs BetOnline (NFL + CFB)

Pre-game lines frozen at last snapshot before kickoff.
Live scoring never changes the displayed line.
"""
import json
import os
import sys
import ssl as _ssl
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASTERN = ZoneInfo("America/New_York")
_SSL    = _ssl._create_unverified_context()

st.set_page_config(page_title="Team Totals FD vs BOL", page_icon="📊", layout="wide")
st.title("📊 Team Totals — FanDuel vs BetOnline")
st.caption(
    "**FanDuel** (US) vs **BetOnline** (offshore) · Pre-game lines only — frozen at last snapshot before kickoff.  \n"
    "🔴 = lines differ by 0.5+ · Hit **🔄 Refresh** to capture a new snapshot."
)


def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


ODDS_KEY = _resolve_secret("THE_ODDS_API_KEY")
if not ODDS_KEY:
    st.error("Missing `THE_ODDS_API_KEY` in secrets.")
    st.stop()


# ---------- Helpers ----------

def _parse_ts(s):
    try:
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _week_monday(dt):
    days = dt.weekday()
    return (dt - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)


def _fmt_time(iso):
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %b %d %#I:%M %p ET" if sys.platform == "win32" else "%a %b %d %-I:%M %p ET"
        return ct.strftime(fmt)
    except Exception:
        return iso


def _fmt_price(p):
    try:
        if p is None or (p != p):
            return "—"
        return f"+{int(p)}" if p > 0 else str(int(p))
    except Exception:
        return "—"


def _fmt_line(x):
    try:
        if x is None or (x != x):
            return "—"
        return f"{float(x):.1f}"
    except Exception:
        return "—"


# ---------- Snapshot ----------

def _run_snapshot(sport):
    sys.path.insert(0, ROOT)
    try:
        if sport == "nfl":
            from scripts.nfl_team_totals_snapshot import main
        else:
            from scripts.cfb_team_totals_snapshot import main
        return main(force=True)
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _load_snapshot(history_dir, extra_days=0):
    now_et   = datetime.now(tz=EASTERN)
    week_key = _week_monday(now_et).strftime("%Y-%m-%d")
    path     = os.path.join(history_dir, f"week_{week_key}.json")
    if not os.path.exists(path):
        return None, path
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


# ---------- Render tab ----------

def _render(history_dir, sport_label):
    data, _ = _load_snapshot(history_dir)

    if data is None:
        st.warning(f"No {sport_label} snapshot yet — hit **🔄 Refresh**.")
        return

    snapshots = data.get("snapshots", [])
    if not snapshots:
        st.warning(f"Snapshot file has no data. Hit **🔄 Refresh**.")
        return

    # current_map: last pre-game snapshot per game
    current_map = {}
    for snap in reversed(snapshots):
        cap_ts = _parse_ts(snap.get("captured_at", ""))
        for g in snap.get("games", []):
            game_key = g.get("game", "")
            if game_key in current_map:
                continue
            kick_ts = _parse_ts(g.get("first_pitch", ""))
            if cap_ts and kick_ts and cap_ts >= kick_ts:
                continue
            current_map[game_key] = g

    now_utc = datetime.now(tz=timezone.utc)
    rows = []
    for g in current_map.values():
        kick_ts = _parse_ts(g.get("first_pitch", ""))
        books   = g.get("books", {})
        fd      = books.get("fanduel", {})
        bol     = books.get("betonlineag", {})
        kickoff = _fmt_time(g.get("first_pitch", ""))
        game    = g.get("game", "")
        away    = g.get("away_team", "")
        home    = g.get("home_team", "")
        started = kick_ts and now_utc >= kick_ts
        status  = "🔴 Live" if started else "🟢 Pre"

        for side, team in (("away", away), ("home", home)):
            fd_side  = fd.get(side,  {})
            bol_side = bol.get(side, {})
            fd_line  = fd_side.get("line")
            bol_line = bol_side.get("line")
            diff     = round(abs(fd_line - bol_line), 1) if (fd_line is not None and bol_line is not None) else None
            rows.append({
                "Status":    status,
                "Kickoff":   kickoff,
                "Game":      game,
                "Team":      team,
                "FD Line":   fd_line,
                "FD Over":   fd_side.get("over_price"),
                "FD Under":  fd_side.get("under_price"),
                "BOL Line":  bol_line,
                "BOL Over":  bol_side.get("over_price"),
                "BOL Under": bol_side.get("under_price"),
                "Diff":      diff,
                "_flag":     (diff is not None and diff >= 0.5),
                "_has":      (fd_line is not None or bol_line is not None),
            })

    df     = pd.DataFrame(rows) if rows else pd.DataFrame()
    df_has = df[df["_has"]] if not df.empty else df

    if df_has.empty:
        st.warning(f"No pre-game {sport_label} team total lines in snapshot. Hit **🔄 Refresh**.")
        return

    # Summary
    last_snap_ts = snapshots[-1].get("captured_at", "")
    try:
        ls = _parse_ts(last_snap_ts).astimezone(EASTERN).strftime("%I:%M %p ET")
    except Exception:
        ls = last_snap_ts

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Games",            df_has["Game"].nunique())
    c2.metric("FanDuel lines",    int(df_has["FD Line"].notna().sum()))
    c3.metric("BetOnline lines",  int(df_has["BOL Line"].notna().sum()))
    c4.metric("🔴 Discrepancies", int(df["_flag"].sum()))
    st.caption(f"Snapshot taken: **{ls}** · {len(snapshots)} snapshots this week")
    st.divider()

    # Format for display
    display = df_has.copy()
    for col in ["FD Over", "FD Under", "BOL Over", "BOL Under"]:
        display[col] = display[col].apply(_fmt_price)
    display["FD Line"]  = display["FD Line"].apply(_fmt_line)
    display["BOL Line"] = display["BOL Line"].apply(_fmt_line)
    display["Diff"]     = display["Diff"].apply(lambda x: f"{x:.1f}" if x is not None and x == x else "—")

    show_cols = ["Status", "Kickoff", "Game", "Team",
                 "FD Line", "FD Over", "FD Under",
                 "BOL Line", "BOL Over", "BOL Under", "Diff"]
    st.dataframe(display[show_cols], use_container_width=True, hide_index=True)

    # Discrepancies
    disc = display[display["_flag"]].sort_values("Diff", ascending=False)
    if not disc.empty:
        st.divider()
        st.subheader("🔴 Line discrepancies (0.5+ difference)")
        st.caption("One book is behind the other — potential sharp signal.")
        st.dataframe(
            disc[["Status", "Kickoff", "Game", "Team",
                  "FD Line", "BOL Line", "Diff",
                  "FD Over", "FD Under", "BOL Over", "BOL Under"]],
            use_container_width=True, hide_index=True,
        )

    st.divider()
    st.caption(
        f"Source: The Odds API · FanDuel (US) vs BetOnline (offshore) · "
        f"Lines frozen at last pre-game snapshot · "
        f"Page loaded: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}"
    )


# ---------- Refresh button ----------

if st.button("🔄 Refresh", type="primary"):
    st.cache_data.clear()
    tab_state = st.session_state.get("active_tab", "NFL")
    sport = "nfl" if tab_state == "NFL" else "cfb"
    with st.spinner("Saving snapshot..."):
        _run_snapshot(sport)
    st.rerun()

# ---------- Tabs ----------

tab_nfl, tab_cfb = st.tabs(["🏈 NFL", "🎓 CFB"])

with tab_nfl:
    st.session_state["active_tab"] = "NFL"
    _render(os.path.join(ROOT, "nfl_team_totals_history"), "NFL")

with tab_cfb:
    st.session_state["active_tab"] = "CFB"
    _render(os.path.join(ROOT, "cfb_team_totals_history"), "CFB")
