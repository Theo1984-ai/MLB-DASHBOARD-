"""
💰 Polymarket Sharp Money tracker.

For every open MLB game-level market on Polymarket, shows where the
limit-order book has heaviest bid-side depth. Strong skew on one side
means real money is queueing up to bet that side at better prices —
pinpointing where sharps are positioned.
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

from scripts.polymarket_sharp import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="Sharp Money", page_icon="💰", layout="wide")
st.title("💰 Polymarket Sharp Money")
st.caption(
    "For each open MLB game market on Polymarket, we measure depth on each "
    "side of the inside spread (within ±5¢ of mid). When 70%+ of resting "
    "bids sit on one side, that's where the smart money wants to bet at "
    "better prices than the current mid — a sharp positioning signal.  \n"
    "**Free data — no Odds API quota used.**"
)


# ---------- Cached scan ----------

@st.cache_data(ttl=900, show_spinner="Pulling Polymarket order books (~30s)...")
def cached_scan(min_volume, min_liquidity, top_n):
    return scan(min_volume=min_volume, min_liquidity=min_liquidity, top_n=top_n)


# ---------- Controls ----------

ctrl_cols = st.columns([1.5, 1, 1, 1, 1.5])
with ctrl_cols[0]:
    refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True,
                        help="Clears the 15-min cache and pulls fresh order books")
with ctrl_cols[1]:
    top_n = st.selectbox("Markets to scan", [15, 30, 50, 75], index=1,
                         help="Top N MLB markets by volume to fetch order books for")
with ctrl_cols[2]:
    min_skew = st.slider("Min skew %", 50, 95, 60, 5,
                         help="Only show markets with this much imbalance")
with ctrl_cols[3]:
    min_depth = st.number_input("Min total depth $", value=500, step=500,
                                help="Filter out tiny dead markets")
with ctrl_cols[4]:
    st.caption(
        "💡 70%+ skew = strong sharp positioning  \n"
        "85%+ skew = extreme conviction (small samples!)"
    )

if refresh:
    cached_scan.clear()
    st.toast("Cache cleared — pulling fresh data...", icon="🔄")

rows, debug = cached_scan(min_volume=500, min_liquidity=20000, top_n=top_n)

st.caption(
    f"Scanned **{debug['total_events']}** MLB events → "
    f"**{debug['daily_markets']}** daily markets → "
    f"**{debug['candidates']}** with volume → "
    f"**{debug['with_book']}** with live order books."
)


# ---------- Filter applied ----------

filtered = [
    r for r in rows
    if r["skew_strength"] >= min_skew
    and (r["yes_bid_depth"] + r["no_bid_depth"]) >= min_depth
]

st.markdown(f"### 🎯 {len(filtered)} markets passing filter")
if not filtered:
    st.warning(
        f"No markets with ≥{min_skew}% skew and ≥${min_depth} depth right now. "
        "Lower the threshold or wait — Polymarket books update constantly."
    )
    st.stop()


# ---------- Sharp-money split ----------

tab_all, tab_yes, tab_no = st.tabs([
    f"📋 All ({len(filtered)})",
    f"🟢 YES heavy ({sum(1 for r in filtered if r['skew_side']=='YES')})",
    f"🔴 NO heavy ({sum(1 for r in filtered if r['skew_side']=='NO')})",
])


def render_table(rows_subset, sort_by="depth"):
    if not rows_subset:
        st.info("No markets in this bucket.")
        return
    table = []
    for r in rows_subset:
        total_depth = r["yes_bid_depth"] + r["no_bid_depth"]
        marker = "💰💰" if r["skew_strength"] >= 85 else ("💰" if r["skew_strength"] >= 70 else "")
        table.append({
            "Game":         r["event"][:32],
            "Mkt":          r["category"],
            "Question":     r["question"][:50],
            "Mid (YES)":    r["mid"],
            "Best bid":     r["best_bid"],
            "Best ask":     r["best_ask"],
            "Spread":       r["spread"],
            "YES bid $":    r["yes_bid_depth"],
            "NO bid $":     r["no_bid_depth"],
            "Side":         f"{marker} {r['skew_side']}",
            "Skew %":       r["skew_strength"],
            "Volume $":     r["volume"],
        })
    df = pd.DataFrame(table)
    if sort_by == "depth":
        df["_sort"] = df["YES bid $"] + df["NO bid $"]
        df = df.sort_values("_sort", ascending=False).drop(columns="_sort")
    elif sort_by == "skew":
        df = df.sort_values("Skew %", ascending=False)
    elif sort_by == "volume":
        df = df.sort_values("Volume $", ascending=False)

    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={
            "Mid (YES)":  st.column_config.NumberColumn(format="$%.3f"),
            "Best bid":   st.column_config.NumberColumn(format="$%.3f"),
            "Best ask":   st.column_config.NumberColumn(format="$%.3f"),
            "Spread":     st.column_config.NumberColumn(format="$%.3f"),
            "YES bid $":  st.column_config.NumberColumn(format="$%,d"),
            "NO bid $":   st.column_config.NumberColumn(format="$%,d"),
            "Skew %":     st.column_config.NumberColumn(format="%.0f%%"),
            "Volume $":   st.column_config.NumberColumn(format="$%,.0f"),
        },
    )


with tab_all:
    sort_choice = st.radio("Sort by", ["depth", "skew", "volume"],
                           horizontal=True, key="sort_all")
    render_table(filtered, sort_by=sort_choice)

with tab_yes:
    yes_only = [r for r in filtered if r["skew_side"] == "YES"]
    render_table(sorted(yes_only, key=lambda r: -r["yes_bid_depth"]))

with tab_no:
    no_only = [r for r in filtered if r["skew_side"] == "NO"]
    render_table(sorted(no_only, key=lambda r: -r["no_bid_depth"]))


# ---------- Top 3 strongest signals — detail expanders ----------

st.markdown("---")
st.markdown("### 🔍 Top 3 strongest signals — full detail")

strongest = sorted(filtered, key=lambda r: (
    -r["skew_strength"], -(r["yes_bid_depth"] + r["no_bid_depth"])
))[:3]

for r in strongest:
    fav_side = r["skew_side"]
    fav_pct = r["skew_strength"]
    sharp_depth = r["no_bid_depth"] if fav_side == "NO" else r["yes_bid_depth"]
    other_depth = r["yes_bid_depth"] if fav_side == "NO" else r["no_bid_depth"]
    interpretation = (
        f"**The book is loaded on the {fav_side} side** "
        f"(${sharp_depth:,} vs ${other_depth:,} on the other). "
        f"That means real money is queued to buy {fav_side} at better prices "
        f"than the current mid (${r['mid']:.3f}). "
        f"Sharp interpretation: market expects {fav_side}."
    )

    with st.expander(
        f"**{r['question'][:80]}**  ·  {r['skew_side']} {fav_pct:.0f}% skew",
        expanded=True,
    ):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Mid (YES)", f"${r['mid']:.3f}")
        c2.metric("YES bid depth", f"${r['yes_bid_depth']:,}")
        c3.metric("NO bid depth", f"${r['no_bid_depth']:,}")
        c4.metric("Spread", f"${r['spread']:.3f}")
        st.markdown(interpretation)
        st.caption(f"Volume traded: ${r['volume']:,.0f}  ·  "
                   f"Listed liquidity: ${r['liquidity']:,.0f}  ·  "
                   f"Category: {r['category']}")


st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 15 min  ·  "
    f"Source: Polymarket Gamma + CLOB APIs (no Odds API quota used)"
)
