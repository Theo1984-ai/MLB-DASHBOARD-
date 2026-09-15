"""
🏈💰 CFB Sharp Money — Sportsbook line comparison.

Detects sharp signals by comparing lines across DK, FD, BetMGM, Caesars, Bovada, BetOnline:
  🔴 Off-market line  = one book significantly differs from consensus (sharps already moved it)
  ⚡ Steam move       = DK+FD have moved but lag books haven't caught up
  💧 Juice imbalance  = book pricing one side cheaper = taking sharp action on that side
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

st.set_page_config(page_title="CFB Sharp Money", page_icon="🏈", layout="wide")
st.title("🏈💰 CFB Sharp Money")
st.caption(
    "Detects sharp money signals by comparing lines across **DraftKings, FanDuel, BetMGM, Caesars, Bovada, BetOnline**.  \n"
    "🔴 **Off-market** = one book differs significantly from consensus (sharps already moved it) · "
    "⚡ **Steam** = DK+FD moved, lag books haven't caught up · "
    "💧 **Juice imbalance** = one side priced cheaper = book taking sharp action on that side."
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


@st.cache_data(ttl=180, show_spinner=False)
def _run_scan(api_key):
    from scripts.cfb_sharp_scanner import scan
    return scan(api_key)


rc1, rc2 = st.columns([1, 5])
with rc1:
    refresh = st.button("🔄 Refresh", type="primary", use_container_width=True)
with rc2:
    st.caption("Live comparison across 6 sharp books · Cache TTL: 3 min")

if refresh:
    st.cache_data.clear()
    st.rerun()

with st.spinner("Comparing CFB lines across sportsbooks..."):
    try:
        plays = _run_scan(ODDS_KEY)
    except Exception as e:
        import traceback
        st.error(f"Scan failed: {e}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc()[:2000])
        st.stop()

if not plays:
    st.info(
        "No sharp signals detected right now. This is normal early in the week before "
        "books post CFB lines (typically Monday/Tuesday). Check back once lines are up."
    )
    st.stop()


def _kickoff(iso):
    if not iso:
        return "?"
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %b %d %#I:%M %p ET" if sys.platform == "win32" else "%a %b %d %-I:%M %p ET"
        return ct.strftime(fmt)
    except Exception:
        return iso


df = pd.DataFrame(plays)
df["Kickoff"]     = df["first_pitch"].apply(_kickoff)
df["Signal"]      = df["signal"]
df["Sharp Pick"]  = df["sharp_pick"]
df["Detail"]      = df["detail"]
df["Strength"]    = df["strength"]
df["Market"]      = df["market"]

DISPLAY = ["Signal", "Kickoff", "Sharp Pick", "Market", "Detail", "Strength"]

# Tabs by signal type
off_mkt  = df[df["Signal"].str.contains("Off-market")]
steam    = df[df["Signal"].str.contains("Steam")]
juice    = df[df["Signal"].str.contains("Juice")]

t1, t2, t3, t4 = st.tabs([
    f"📋 All ({len(df)})",
    f"🔴 Off-market ({len(off_mkt)})",
    f"⚡ Steam ({len(steam)})",
    f"💧 Juice ({len(juice)})",
])

COL_CFG = {
    "Strength": st.column_config.NumberColumn(format="%.1f"),
}

with t1:
    st.dataframe(df[DISPLAY], use_container_width=True, hide_index=True, column_config=COL_CFG)
with t2:
    if off_mkt.empty:
        st.info("No off-market lines detected.")
    else:
        st.dataframe(off_mkt[DISPLAY], use_container_width=True, hide_index=True, column_config=COL_CFG)
with t3:
    if steam.empty:
        st.info("No steam moves detected.")
    else:
        st.dataframe(steam[DISPLAY], use_container_width=True, hide_index=True, column_config=COL_CFG)
with t4:
    if juice.empty:
        st.info("No juice imbalances detected.")
    else:
        st.dataframe(juice[DISPLAY], use_container_width=True, hide_index=True, column_config=COL_CFG)

st.divider()
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
    f"{len(plays)} sharp signals across {df['game'].nunique()} games  •  "
    f"Books: DK · FD · BetMGM · Caesars · Bovada · BetOnline"
)
