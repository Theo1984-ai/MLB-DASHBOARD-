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
from scripts.sportsbook_matcher import match_signals  # noqa: E402

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



# =============================================================================
# CROSS-VENUE ARB — Polymarket sharp signal vs sportsbook line
# =============================================================================

st.markdown("---")
st.markdown("### 🎯 Cross-venue plays — sharp signal + sportsbook line")
st.caption(
    "For each strong Polymarket signal, we look up the matching sportsbook "
    "line. **Edge pp** = how many percentage points sharper than the "
    "sportsbook's implied probability the Polymarket sharps believe the play "
    "is. Positive edge + strong skew = actionable mispricing."
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
    st.warning("No `THE_ODDS_API_KEY` configured — can't cross-reference sportsbook prices.")
else:
    @st.cache_data(ttl=300, show_spinner="Matching sportsbook lines...")
    def cached_match(rows_tuple, _odds_key):
        # Convert back to list[dict] for the matcher
        return match_signals([dict(r) for r in rows_tuple], _odds_key)

    # Use only strongly-skewed rows (less noise)
    strong = [r for r in filtered if r["skew_strength"] >= 65]
    if not strong:
        st.info("No strong signals (≥65% skew) to cross-reference.")
    else:
        # cache key needs hashability — pass tuples of items
        rows_key = tuple(tuple(sorted(r.items())) for r in strong)
        try:
            matched = cached_match(rows_key, ODDS_KEY)
        except Exception as e:
            st.error(f"Sportsbook match failed: {e}")
            matched = []

        # Display table
        if matched:
            tbl = []
            for r in matched:
                tbl.append({
                    "Game":        r.get("event", "")[:32],
                    "Mkt":         r.get("category", ""),
                    "Sharp side":  f"{'💰💰' if r['skew_strength']>=85 else '💰'} {r['skew_side']} {r['skew_strength']:.0f}%",
                    "PM mid":      r.get("mid"),
                    "PM YES depth $": r.get("yes_bid_depth"),
                    "PM NO depth $":  r.get("no_bid_depth"),
                    "SB price":    r.get("sb_best_price"),
                    "SB book":     r.get("sb_book", ""),
                    "SB %":        r.get("sb_implied_pct"),
                    "Edge pp":     r.get("edge_pp"),
                    "Play":        r.get("play", "")[:50],
                    "Volume $":    r.get("volume"),
                })
            df = pd.DataFrame(tbl)
            # Sort by edge (highest first)
            df_sorted = df.copy()
            df_sorted["_edge_for_sort"] = pd.to_numeric(df_sorted["Edge pp"], errors="coerce").fillna(-999)
            df_sorted = df_sorted.sort_values("_edge_for_sort", ascending=False).drop(columns="_edge_for_sort")

            st.dataframe(
                df_sorted, use_container_width=True, hide_index=True,
                column_config={
                    "PM mid":         st.column_config.NumberColumn(format="$%.3f"),
                    "PM YES depth $": st.column_config.NumberColumn(format="$%,d"),
                    "PM NO depth $":  st.column_config.NumberColumn(format="$%,d"),
                    "SB price":       st.column_config.NumberColumn(format="%+d"),
                    "SB %":           st.column_config.NumberColumn(format="%.1f%%"),
                    "Edge pp":        st.column_config.NumberColumn(format="%+.1f"),
                    "Volume $":       st.column_config.NumberColumn(format="$%,.0f"),
                },
            )

            # Top 3 actionable plays — positive edge + strong skew + decent volume
            actionable = [r for r in matched
                          if r.get("edge_pp") is not None and r["edge_pp"] > 0
                          and r["skew_strength"] >= 70
                          and r["volume"] > 1000
                          and r.get("sb_best_price") is not None]
            actionable.sort(key=lambda r: -r["edge_pp"])

            if actionable:
                st.markdown(f"#### 🏆 Top {min(5, len(actionable))} actionable plays")
                st.caption("Positive edge AND ≥70% skew AND ≥$1K volume AND sportsbook match found")
                for r in actionable[:5]:
                    edge = r["edge_pp"]
                    color = "🟢" if edge >= 5 else "🟡"
                    with st.expander(
                        f"{color} **{r['play']}**  ·  edge {edge:+.1f}pp  ·  "
                        f"{r['skew_side']} {r['skew_strength']:.0f}% on Polymarket",
                        expanded=True,
                    ):
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Polymarket implied (sharp side)",
                                  f"{r['mid']*100 if r['skew_side']=='YES' else (1-r['mid'])*100:.1f}%")
                        c2.metric("Sportsbook implied", f"{r['sb_implied_pct']:.1f}%")
                        c3.metric("Edge", f"{edge:+.1f}pp", help="PM sharp implied − SB implied")
                        st.write(
                            f"**The play:** {r['play']}  \n"
                            f"**Why:** Polymarket sharps have loaded {r['skew_side']} side "
                            f"with {r['skew_strength']:.0f}% bid depth on ${r['volume']:,.0f} volume. "
                            f"That implies they think the true probability is "
                            f"~{r['mid']*100 if r['skew_side']=='YES' else (1-r['mid'])*100:.0f}% "
                            f"while {r['sb_book']} is offering {r['sb_implied_pct']:.0f}% implied. "
                            f"Difference: **{edge:+.1f}pp edge**."
                        )
            else:
                st.info(
                    "No clean cross-venue arbs right now (need: +edge, ≥70% skew, "
                    "≥$1K volume, sportsbook match). Try lowering the strict filter "
                    "above, or check back closer to first pitch when lines firm up."
                )


st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 15 min  ·  "
    f"Polymarket: Gamma + CLOB (free)  ·  "
    f"Sportsbook match: Odds API (small quota)"
)
