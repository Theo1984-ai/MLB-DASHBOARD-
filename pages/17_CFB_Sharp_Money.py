"""
🏈💰 CFB Sharp Money — Polymarket order-book depth scanner.
Mirrors NFL Sharp Money but for college football.
"""
import json
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="CFB Sharp Money", page_icon="🏈", layout="wide")
st.title("🏈💰 CFB Sharp Money — Polymarket")
st.caption(
    "College football Polymarket order-book depth scanner. "
    "Heavy bid skew on one side = where limit-order books want to bet = sharp signal.  \n"
    "**Skew strength** = % of near-mid liquidity on the sharp side. "
    "**Whale share** > 50% means one large order is driving the skew (less reliable)."
)


@st.cache_data(ttl=180, show_spinner=False)
def _run_scan(min_vol, min_liq, top_n):
    from scripts.cfb_polymarket_sharp import scan
    return scan(min_volume=min_vol, min_liquidity=min_liq, top_n=top_n)


# Controls
cc1, cc2, cc3, cc4 = st.columns([1, 1, 1, 2])
with cc1:
    refresh = st.button("🔄 Refresh", type="primary", use_container_width=True)
with cc2:
    min_vol = st.number_input("Min volume ($)", value=200, step=100)
with cc3:
    min_liq = st.number_input("Min liquidity ($)", value=10000, step=1000)
with cc4:
    top_n = st.slider("Max markets to scan", 10, 50, 30)

if refresh:
    st.cache_data.clear()
    st.rerun()

with st.spinner("Scanning CFB Polymarket order books..."):
    try:
        rows, debug = _run_scan(min_vol, min_liq, top_n)
    except Exception as e:
        import traceback
        st.error(f"Scan failed: {e}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc()[:2000])
        st.stop()

with st.expander("📊 Scan stats", expanded=False):
    st.json(debug)

if not rows:
    st.warning(
        "No CFB markets found with sufficient liquidity. "
        "Polymarket CFB coverage is lighter than NFL — check back closer to game day, "
        "or lower the Min volume / Min liquidity thresholds."
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


def _score(r):
    depth_ratio = r["yes_bid_depth"] / max(r["no_bid_depth"], 1)
    skew_pts = (r["skew_strength"] - 50) * 0.4
    vol_pts  = min(r["volume"] / 5000 * 20, 20)
    whale_pen = -25 if r["sharp_whale_share"] > 0.6 else (-10 if r["sharp_whale_share"] > 0.4 else 0)
    depth_pts = min((depth_ratio - 1) * 15, 30) if depth_ratio > 1 else max((depth_ratio - 1) * 15, -30)
    return round(depth_pts + skew_pts + vol_pts + whale_pen, 1)


df = pd.DataFrame(rows)
df["Kickoff"]      = df["game_start"].apply(_kickoff)
df["Sharp Score"]  = df.apply(_score, axis=1)
df["Sharp Pick"]   = df["sharp_pick"]
df["Skew %"]       = df["skew_strength"]
df["Volume"]       = df["volume"].apply(lambda x: f"${x:,.0f}")
df["Liquidity"]    = df["liquidity"].apply(lambda x: f"${x:,.0f}")
df["Whale Share"]  = df["sharp_whale_share"].apply(lambda x: f"{x:.0%}")
df["Category"]     = df["category"]
df["YES depth"]    = df["yes_bid_depth"].apply(lambda x: f"${x:,}")
df["NO depth"]     = df["no_bid_depth"].apply(lambda x: f"${x:,}")
df["Mid"]          = df["mid"].apply(lambda x: f"{x:.2f}")

df = df.sort_values("Sharp Score", ascending=False)

DISPLAY = ["Sharp Score", "Kickoff", "Sharp Pick", "Category",
           "Skew %", "Volume", "Liquidity", "Whale Share", "YES depth", "NO depth", "Mid"]

t_all, t_yes, t_no = st.tabs([
    f"📋 All ({len(df)})",
    f"✅ YES heavy ({(df['skew_side']=='YES').sum()})",
    f"❌ NO heavy ({(df['skew_side']=='NO').sum()})",
])
with t_all:
    st.dataframe(df[DISPLAY], use_container_width=True, hide_index=True)
with t_yes:
    st.dataframe(df[df["skew_side"] == "YES"][DISPLAY], use_container_width=True, hide_index=True)
with t_no:
    st.dataframe(df[df["skew_side"] == "NO"][DISPLAY], use_container_width=True, hide_index=True)

st.divider()
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
    f"{len(rows)} markets with order-book data  •  Cache TTL: 3 min"
)
