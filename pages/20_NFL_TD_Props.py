"""
🏈🎯 NFL TD Props — full scorer board with filters.
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

from scripts.td_props_scanner import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="NFL TD Props", page_icon="🏈", layout="wide")
st.title("🏈🎯 NFL TD Props — Scorer Board")
st.caption(
    "All players priced for **Anytime TD**, **First TD**, and **Last TD**.  "
    "Sourced from DK · FD · MGM · CZR · BOV · PIN.  \n"
    "**Consensus %** = average true probability across all priced books.  "
    "**💎 Value** = best available price beats consensus by 3+ pts.  "
    "**⚡ Sharp** = DK/FD have player 5+ pts higher than lag books (sharp steam)."
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


# ---------- Fetch ----------

@st.cache_data(ttl=180, show_spinner="Scanning NFL TD props across 6 books...")
def _cached_scan(api_key):
    return scan(api_key, sport="americanfootball_nfl")


# Refresh button at top
if st.button("🔄 Refresh", type="primary"):
    st.cache_data.clear()
    st.toast("Cache cleared — scanning TD props...", icon="🔄")

with st.spinner("Scanning NFL TD props..."):
    try:
        rows, dbg = _cached_scan(ODDS_KEY)
    except Exception as e:
        import traceback
        st.error(f"Scan failed: {e}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc()[:2000])
        st.stop()

if not rows:
    st.warning(
        "No NFL TD props found right now — books may not have posted props yet "
        "(typically available by Thursday/Friday before game week)."
    )
    with st.expander("🔍 Diagnostic info"):
        st.write({
            "Events on API":         dbg.get("n_events", 0),
            "Upcoming games":        dbg.get("n_upcoming", 0),
            "Games w/ scorer props": dbg.get("n_with_scorer_props", 0),
            "Markets seen":          dbg.get("markets_seen", []),
            "Outcomes parsed":       dbg.get("outcomes_parsed", 0),
            "Errors":                dbg.get("errors", []),
        })
    st.stop()


# ---------- Helpers ----------

def _kickoff(iso):
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %#I:%M %p" if sys.platform == "win32" else "%a %-I:%M %p"
        return ct.strftime(fmt)
    except Exception:
        return iso or "?"


# ---------- Filters sidebar ----------

st.sidebar.header("🔽 TD Props Filters")

all_games = sorted({r["game"] for r in rows})
game_sel = st.sidebar.multiselect(
    "Games", options=all_games, default=[],
    help="Leave empty to show all games"
)

player_search = st.sidebar.text_input(
    "Player name", placeholder="e.g. Kelce, Jefferson",
    help="Case-insensitive substring"
)

min_consensus = st.sidebar.slider(
    "Min consensus %", min_value=0, max_value=80, value=0, step=5,
    help="Only show players where the books imply at least this probability"
)

max_price = st.sidebar.number_input(
    "Max best price (American)", value=0, step=50,
    help="E.g. 300 = only show players priced +300 or better (longer shots). 0 = no cap."
)

col_sig1, col_sig2 = st.sidebar.columns(2)
with col_sig1:
    value_only = st.toggle("💎 Value only", value=False,
                           help="Best price beats consensus by 3+ pts")
with col_sig2:
    sharp_only = st.toggle("⚡ Sharp only", value=False,
                           help="DK/FD 5+ pts higher than lag books")

sort_by = st.sidebar.radio(
    "Sort by",
    options=["Consensus % (most likely)", "Value Edge (best value)", "Sharp Gap (most steam)", "Best Price"],
    index=0,
)

st.sidebar.markdown("---")
st.sidebar.caption("💎 Value = best price beats consensus 3+ pp  \n⚡ Sharp = DK/FD 5+ pp higher than lag")


# ---------- Apply filters ----------

filtered = list(rows)

if game_sel:
    filtered = [r for r in filtered if r["game"] in game_sel]

if player_search.strip():
    q = player_search.strip().lower()
    filtered = [r for r in filtered if q in r["player"].lower()]

if min_consensus > 0:
    filtered = [r for r in filtered if r["consensus_prob"] >= min_consensus]

if max_price > 0:
    filtered = [r for r in filtered if r["best_price"] <= max_price]

if value_only:
    filtered = [r for r in filtered if r["value_edge"] >= 3.0]

if sharp_only:
    filtered = [r for r in filtered if r["sharp_gap"] >= 5.0]

# Sort
if "Value Edge" in sort_by:
    filtered.sort(key=lambda r: -r["value_edge"])
elif "Sharp Gap" in sort_by:
    filtered.sort(key=lambda r: -r["sharp_gap"])
elif "Best Price" in sort_by:
    filtered.sort(key=lambda r: r["best_price"])  # lower (more negative) = more likely
else:
    market_order = {"Anytime TD": 0, "First TD": 1, "Last TD": 2}
    filtered.sort(key=lambda r: (market_order.get(r["market"], 9), -r["consensus_prob"]))

# Partition
anytime  = [r for r in filtered if r["market"] == "Anytime TD"]
first_td = [r for r in filtered if r["market"] == "First TD"]
last_td  = [r for r in filtered if r["market"] == "Last TD"]

n_value = sum(1 for r in filtered if r["value_edge"] >= 3.0)
n_sharp = sum(1 for r in filtered if r["sharp_gap"] >= 5.0)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Showing", len(filtered))
m2.metric("💎 Value", n_value)
m3.metric("⚡ Sharp", n_sharp)
m4.metric("Games", len({r["game"] for r in filtered}))

# ---------- Tabs ----------

BOOK_COLS  = ["DK", "FD", "MGM", "CZR", "BOV", "PIN"]
PRICE_COLS = [f"price_{b}" for b in BOOK_COLS]

COL_CFG = {
    "Consensus %": st.column_config.NumberColumn(format="%.1f%%"),
    "Best Price":  st.column_config.NumberColumn(format="%+d"),
    "Value Edge":  st.column_config.NumberColumn(format="%+.1f pp"),
    "Sharp Gap":   st.column_config.NumberColumn(format="%+.1f pp"),
    **{b: st.column_config.NumberColumn(label=b, format="%+d") for b in BOOK_COLS},
}


def _flags(r):
    f = ""
    if r["value_edge"] >= 3.0: f += "💎"
    if r["sharp_gap"] >= 5.0:  f += "⚡"
    return f


def _build_df(subset):
    if not subset:
        return pd.DataFrame()
    df_rows = []
    for r in subset:
        row = {
            "":            _flags(r),
            "Player":      r["player"],
            "Game":        r["game"],
            "Kickoff":     _kickoff(r["first_pitch"]),
        }
        for b, pc in zip(BOOK_COLS, PRICE_COLS):
            row[b] = r.get(pc)
        row.update({
            "Consensus %": r["consensus_prob"],
            "Best Price":  r["best_price"],
            "Best Book":   r["best_book"],
            "Value Edge":  r["value_edge"],
            "Sharp Gap":   r["sharp_gap"],
            "# Books":     r["n_books"],
        })
        df_rows.append(row)
    return pd.DataFrame(df_rows)


def _render(subset):
    if not subset:
        st.info("No props match your current filters.")
        return
    df = _build_df(subset)
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=COL_CFG)


t1, t2, t3 = st.tabs([
    f"🏃 Anytime TD ({len(anytime)})",
    f"🥇 First TD ({len(first_td)})",
    f"🏆 Last TD ({len(last_td)})",
])

with t1:
    _render(anytime)
with t2:
    _render(first_td)
with t3:
    _render(last_td)


# ---------- Cross-market ----------

st.markdown("---")
st.subheader("🔀 Players priced in 2+ markets")

player_markets: dict[str, list] = {}
for r in filtered:
    player_markets.setdefault(r["player"], []).append(r)

cross = {p: ms for p, ms in player_markets.items() if len(ms) >= 2}
if cross:
    cross_rows = []
    for player, ms in sorted(cross.items(),
                              key=lambda x: -max(r["consensus_prob"] for r in x[1])):
        for r in ms:
            cross_rows.append({
                "Player":      player,
                "Market":      r["market"],
                "Game":        r["game"],
                "Consensus %": r["consensus_prob"],
                "Best Price":  r["best_price"],
                "Best Book":   r["best_book"],
                "Value Edge":  r["value_edge"],
                "Sharp Gap":   r["sharp_gap"],
            })
    st.dataframe(
        pd.DataFrame(cross_rows),
        use_container_width=True, hide_index=True,
        column_config={
            "Consensus %": st.column_config.NumberColumn(format="%.1f%%"),
            "Best Price":  st.column_config.NumberColumn(format="%+d"),
            "Value Edge":  st.column_config.NumberColumn(format="%+.1f pp"),
            "Sharp Gap":   st.column_config.NumberColumn(format="%+.1f pp"),
        },
    )
else:
    st.info("No players priced in 2+ markets with current filters.")

st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 3 min  ·  Books: DK · FD · MGM · CZR · BOV · PIN"
)
