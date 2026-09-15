"""
🏈💰 CFB Sharp Money — sportsbook line comparison scanner.

Same look and scoring as MLB Sharp Money, but uses Odds API sportsbook
line comparison (off-market, steam, juice imbalance) since Polymarket
has no CFB market coverage.
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

from scripts.cfb_sharp_scanner import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="CFB Sharp Money", page_icon="🏈", layout="wide")
st.title("🏈💰 CFB Sharp Money")
st.caption(
    "Detects sharp money by comparing lines across 6 books (DK, FD, MGM, CZR, BOV, BOL).  \n"
    "**🔴 Off-market** = one book's line is already 1.5+ pts different from consensus — sharps moved it early.  \n"
    "**⚡ Steam** = DK+FD moved ahead of lag books by 1.0+ pts — sharp steam in progress.  \n"
    "**💧 Juice** = one side priced 15+ cents more expensive — book absorbing sharp action on that side.  \n"
    "Uses Odds API (small quota). Polymarket has no CFB markets."
)


def resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


ODDS_KEY = resolve_secret("THE_ODDS_API_KEY")
if not ODDS_KEY:
    st.error("Missing `THE_ODDS_API_KEY` in secrets.")
    st.stop()


@st.cache_data(ttl=300, show_spinner="Scanning CFB lines across 6 books...")
def cached_scan(api_key):
    return scan(api_key)


ctrl_cols = st.columns([1.5, 1, 1, 1, 1])
with ctrl_cols[0]:
    refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True)
with ctrl_cols[1]:
    min_strength = st.number_input("Min strength", value=1.0, step=0.5,
                                   help="Minimum signal magnitude (pts or cents)")
with ctrl_cols[2]:
    signal_filter = st.selectbox("Signal type", ["All", "Off-market", "Steam", "Juice"])
with ctrl_cols[3]:
    market_filter = st.selectbox("Market", ["All", "Spread", "Total"])
with ctrl_cols[4]:
    sort_by = st.radio("Sort by", ["score", "strength", "signal"],
                       horizontal=True, key="sort_main")

if refresh:
    cached_scan.clear()
    st.toast("Cache cleared — scanning CFB lines...", icon="🔄")

with st.spinner("Scanning CFB sportsbook lines..."):
    try:
        plays = cached_scan(ODDS_KEY)
    except Exception as e:
        st.error(f"Scan failed: {e}")
        st.stop()

if not plays:
    st.warning(
        "No CFB sharp signals right now — lines may not be posted yet "
        "(typically post Mon/Tue for the upcoming week). Check back later."
    )
    st.stop()


# ── Filtering ──
def _sig_key(s):
    if "Off-market" in s: return "Off-market"
    if "Steam"      in s: return "Steam"
    if "Juice"      in s: return "Juice"
    return s

filtered = [p for p in plays if p["strength"] >= min_strength]
if signal_filter != "All":
    filtered = [p for p in filtered if signal_filter.lower() in p["signal"].lower()]
if market_filter != "All":
    filtered = [p for p in filtered if p["market"] == market_filter]


# ── Scoring ──
SIGNAL_BASE = {"Off-market": 45, "Steam": 32, "Juice": 18}

def _score(p):
    base = SIGNAL_BASE.get(_sig_key(p["signal"]), 15)
    extra = min(30, int((p["strength"] - 1.0) / 0.5) * 8)
    return min(100, base + extra)

def _tier(score):
    if score >= 70: return "🟢🟢🟢 ELITE"
    if score >= 50: return "🟢🟢 STRONG"
    if score >= 30: return "🟢 DECENT"
    return "🟡 WEAK"

def _fp_label(iso_str):
    if not iso_str: return "?"
    try:
        ct = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %b %d %#I:%M %p ET" if sys.platform == "win32" else "%a %b %d %-I:%M %p ET"
        return ct.strftime(fmt)
    except Exception:
        return iso_str


# ── Tabs ──
off_mkt  = [p for p in filtered if "Off-market" in p["signal"]]
steam    = [p for p in filtered if "Steam"      in p["signal"]]
juice    = [p for p in filtered if "Juice"      in p["signal"]]

st.markdown(f"### 🎯 {len(filtered)} sharp signals")

tab_all, tab_off, tab_steam, tab_juice = st.tabs([
    f"📋 All ({len(filtered)})",
    f"🔴 Off-market ({len(off_mkt)})",
    f"⚡ Steam ({len(steam)})",
    f"💧 Juice ({len(juice)})",
])

show_details_key = {"📋 All": "d_all", "🔴 Off-market": "d_off",
                    "⚡ Steam": "d_steam", "💧 Juice": "d_juice"}


def render_table(subset, tab_key, show_detail=False):
    if not subset:
        st.info("No signals in this bucket.")
        return
    rows = []
    for p in subset:
        sc = _score(p)
        rows.append({
            "Score":       sc,
            "Tier":        _tier(sc),
            "Signal":      p["signal"],
            "Sharp pick":  p["sharp_pick"],
            "Market":      p["market"],
            "Strength":    p["strength"],
            "Book":        p["book"],
            "Game":        p["game"],
            "Kickoff":     _fp_label(p.get("first_pitch","")),
        })
        if show_detail:
            rows[-1]["Detail"] = p.get("detail", "")

    df = pd.DataFrame(rows)
    if sort_by == "score":
        df = df.sort_values("Score", ascending=False)
    elif sort_by == "strength":
        df = df.sort_values("Strength", ascending=False)
    elif sort_by == "signal":
        rank = {"Off-market": 0, "Steam": 1, "Juice": 2}
        df["_sig_rank"] = df["Signal"].map(lambda s: rank.get(_sig_key(s), 9))
        df = df.sort_values(["_sig_rank", "Score"], ascending=[True, False])
        df = df.drop(columns="_sig_rank")

    cfg = {
        "Score":    st.column_config.NumberColumn(format="%d"),
        "Strength": st.column_config.NumberColumn(format="%.1f"),
    }
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=cfg)


with tab_all:
    show_d = st.toggle("🔧 Show detail", value=False, key="d_all")
    render_table(filtered, "all", show_detail=show_d)

with tab_off:
    show_d2 = st.toggle("🔧 Show detail", value=False, key="d_off")
    render_table(off_mkt, "off", show_detail=show_d2)

with tab_steam:
    show_d3 = st.toggle("🔧 Show detail", value=False, key="d_steam")
    render_table(steam, "steam", show_detail=show_d3)

with tab_juice:
    show_d4 = st.toggle("🔧 Show detail", value=False, key="d_juice")
    render_table(juice, "juice", show_detail=show_d4)


# ── Top plays ──
st.markdown("---")
st.markdown("### 🎯 Top 5 strongest signals")
top5 = sorted(filtered, key=lambda p: -_score(p))[:5]
for p in top5:
    sc = _score(p)
    with st.expander(
        f"**{p['sharp_pick']}**  ·  {p['signal']}  ·  {p['game']}  ·  "
        f"Score {sc}  ·  {_fp_label(p.get('first_pitch',''))}",
        expanded=True,
    ):
        st.write(
            f"**Signal:** {p['signal']}  \n"
            f"**Detail:** {p.get('detail','')}  \n"
            f"**Strength:** {p['strength']}  ·  **Market:** {p['market']}  ·  **Book:** {p['book']}"
        )


st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 5 min  ·  "
    f"Books: DK · FD · MGM · CZR · BOV · BOL  ·  "
    f"Odds API (small quota)"
)
