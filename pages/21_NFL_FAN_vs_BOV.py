"""
📊 NFL Team Totals — FanDuel vs BetOnline

Uses snapshot data so lines are frozen at their last pre-game value.
Live-adjusted lines (after kickoff) are ignored — always shows the pre-game line.
"""
import json
import os
import sys
import ssl as _ssl
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASTERN = ZoneInfo("America/New_York")
_SSL    = _ssl._create_unverified_context()
HISTORY_DIR = os.path.join(ROOT, "nfl_team_totals_history")

st.set_page_config(page_title="NFL Team Totals", page_icon="📊", layout="wide")
st.title("📊 NFL Team Totals — FanDuel vs BetOnline")
st.caption(
    "**FanDuel** (US sharp book) vs **BetOnline** (offshore) · Pre-game lines only — "
    "lines are frozen at last snapshot before kickoff so live scoring never changes the display.  \n"
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


# ---------- Snapshot trigger ----------

@st.cache_data(ttl=120, show_spinner="Saving snapshot...")
def _run_snapshot(api_key, _ts):
    sys.path.insert(0, ROOT)
    try:
        from scripts.nfl_team_totals_snapshot import main
        return main(force=True)
    except Exception as e:
        return {"status": "error", "error": str(e)}


if st.button("🔄 Refresh", type="primary"):
    st.cache_data.clear()
    _run_snapshot(ODDS_KEY, datetime.now().isoformat())
    st.rerun()


# ---------- Load snapshot ----------

def _week_monday(dt):
    from datetime import timedelta
    days = dt.weekday()
    return (dt - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)


def _load_snapshot():
    now_et   = datetime.now(tz=EASTERN)
    week_key = _week_monday(now_et).strftime("%Y-%m-%d")
    path     = os.path.join(HISTORY_DIR, f"week_{week_key}.json")
    if not os.path.exists(path):
        return None, path
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


data, snap_path = _load_snapshot()

if data is None:
    st.warning("No snapshot yet — hit **🔄 Refresh** to capture this week's lines.")
    st.stop()

snapshots = data.get("snapshots", [])
if not snapshots:
    st.warning("Snapshot file exists but has no data. Hit **🔄 Refresh**.")
    st.stop()


# ---------- current_map: last PRE-GAME line per game ----------
# Walk reversed snapshots; for each game only take the snapshot if it was
# captured BEFORE the game's kickoff — this freezes the pre-game line.

def _parse_ts(s):
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if "+" not in s and "Z" not in s \
               else datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


current_map = {}
for snap in reversed(snapshots):
    cap_ts = _parse_ts(snap.get("captured_at", ""))
    for g in snap.get("games", []):
        game_key = g.get("game", "")
        if game_key in current_map:
            continue
        kick_ts = _parse_ts(g.get("first_pitch", ""))
        # Only use this snapshot if it was taken before kickoff
        if cap_ts and kick_ts and cap_ts >= kick_ts:
            continue
        current_map[game_key] = g


# ---------- Build display rows ----------

def _fmt_time(iso):
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %b %d %#I:%M %p ET" if sys.platform == "win32" else "%a %b %d %-I:%M %p ET"
        return ct.strftime(fmt)
    except Exception:
        return iso


def _fmt_price(p):
    if p is None:
        return "—"
    return f"+{int(p)}" if p > 0 else str(int(p))


now_utc = datetime.now(tz=timezone.utc)
rows = []
for g in current_map.values():
    kick_ts = _parse_ts(g.get("first_pitch", ""))
    # Skip games finished more than 4 hours ago
    if kick_ts and (now_utc - kick_ts).total_seconds() > 4 * 3600:
        continue

    books = g.get("books", {})
    fd  = books.get("fanduel",    {})
    bol = books.get("betonlineag", {})
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
        diff     = round(abs(fd_line - bol_line), 1) if (fd_line and bol_line) else None

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

df = pd.DataFrame(rows) if rows else pd.DataFrame()
df_has = df[df["_has"]] if not df.empty else df

if df_has.empty:
    st.warning("No pre-game team total lines found in snapshot. Hit **🔄 Refresh** to capture lines.")
    st.stop()

# ---------- Summary ----------

last_snap_ts = snapshots[-1].get("captured_at", "")
try:
    ls = datetime.fromisoformat(last_snap_ts).astimezone(EASTERN).strftime("%I:%M %p ET")
except Exception:
    ls = last_snap_ts

c1, c2, c3, c4 = st.columns(4)
c1.metric("Games",           df_has["Game"].nunique())
c2.metric("FanDuel lines",   int(df_has["FD Line"].notna().sum()))
c3.metric("BetOnline lines", int(df_has["BOL Line"].notna().sum()))
c4.metric("🔴 Discrepancies", int(df["_flag"].sum()))

st.caption(f"Snapshot taken: **{ls}** · {len(snapshots)} snapshots this week")
st.divider()

# ---------- Table ----------

display = df_has.copy()
display["FD Over"]   = display["FD Over"].apply(_fmt_price)
display["FD Under"]  = display["FD Under"].apply(_fmt_price)
display["BOL Over"]  = display["BOL Over"].apply(_fmt_price)
display["BOL Under"] = display["BOL Under"].apply(_fmt_price)
display["Diff"]      = display["Diff"].apply(lambda x: f"{x:.1f}" if x is not None else "—")

display["FD Line"]  = display["FD Line"].apply(lambda x: f"{x:.1f}" if x is not None else "—")
display["BOL Line"] = display["BOL Line"].apply(lambda x: f"{x:.1f}" if x is not None else "—")

show_cols = ["Status", "Kickoff", "Game", "Team",
             "FD Line", "FD Over", "FD Under",
             "BOL Line", "BOL Over", "BOL Under",
             "Diff"]
st.dataframe(
    display[show_cols],
    use_container_width=True,
    hide_index=True,
)

# ---------- Discrepancies ----------

disc = display[display["_flag"]].sort_values("Diff", ascending=False)
if not disc.empty:
    st.divider()
    st.subheader("🔴 Line discrepancies (0.5+ difference)")
    st.caption("One book is behind the other — potential sharp signal.")
    st.dataframe(
        disc[["Status", "Kickoff", "Game", "Team",
              "FD Line", "BOL Line", "Diff",
              "FD Over", "FD Under", "BOL Over", "BOL Under"]],
        use_container_width=True,
        hide_index=True,
    )

st.divider()
st.caption(
    f"Source: The Odds API · FanDuel (US) vs BetOnline (offshore) · "
    f"Lines frozen at last pre-game snapshot · "
    f"Page loaded: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}"
)
