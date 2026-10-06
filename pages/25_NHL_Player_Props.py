"""
🏒 NHL Player Props — Goals & Shots on Goal
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

from scripts.nhl_props_scanner import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="NHL Player Props", page_icon="🏒", layout="wide")
st.title("🏒 NHL Player Props")
st.caption(
    "**Goals** and **Shots on Goal** O/U props from FanDuel + BetRivers.  \n"
    "**No-Vig Over %** = true probability (vig removed from one book's line).  "
    "**💎 Value** = best price beats no-vig by 3+ pp.  "
    "**📊 Book Split** = both books priced this player."
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
def _cached_scan(api_key):
    return scan(api_key)


if st.button("🔄 Refresh", type="primary"):
    st.cache_data.clear()
    st.rerun()

with st.spinner("Loading NHL player props..."):
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
        "No NHL props found right now — books typically post props a few hours "
        "before puck drop."
    )
    with st.expander("🔍 Diagnostic info"):
        st.write({
            "Events on API":   dbg.get("n_events", 0),
            "Upcoming games":  dbg.get("n_upcoming", 0),
            "Outcomes parsed": dbg.get("outcomes_parsed", 0),
            "Market hits":     dbg.get("market_hits", {}),
            "Errors":          dbg.get("errors", []),
        })
    st.stop()


def _kickoff(iso):
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %#I:%M %p" if sys.platform == "win32" else "%a %-I:%M %p"
        return ct.strftime(fmt)
    except Exception:
        return iso or "?"


# ---- Sidebar filters ----
st.sidebar.markdown("### 🏒 Filters")

all_markets = ["Goals", "Shots on Goal"]
market_sel = st.sidebar.radio("Market", all_markets, index=0)

all_games = sorted({r["game"] for r in rows})
game_sel = st.sidebar.multiselect(
    "Game", options=all_games, default=[],
    help="Leave empty to show all games"
)

player_search = st.sidebar.text_input("Player search", placeholder="e.g. Matthews, Kaprizov")

min_nv = st.sidebar.slider("Min No-Vig Over %", 0, 80, 0, step=5)
value_only = st.sidebar.toggle("💎 Value plays only (3+ pp edge)", value=False)
two_book_only = st.sidebar.toggle("📊 Both books only", value=False)

sort_by = st.sidebar.radio(
    "Sort by",
    ["No-Vig Over % (highest)", "Best Over Price", "Value Edge (over)"],
    index=0,
)

# ---- Apply filters ----
filtered = [r for r in rows if r["market"] == market_sel]
if game_sel:
    filtered = [r for r in filtered if r["game"] in game_sel]
if player_search.strip():
    q = player_search.strip().lower()
    filtered = [r for r in filtered if q in r["player"].lower()]
if min_nv > 0:
    filtered = [r for r in filtered if (r.get("nv_over_pct") or 0) >= min_nv]
if value_only:
    filtered = [r for r in filtered if (r.get("over_edge") or 0) >= 3.0]
if two_book_only:
    filtered = [r for r in filtered if r.get("n_books", 0) >= 2]

# ---- Sort ----
if sort_by == "No-Vig Over % (highest)":
    filtered.sort(key=lambda r: -(r.get("nv_over_pct") or 0))
elif sort_by == "Best Over Price":
    filtered.sort(key=lambda r: -(r.get("best_over_price") or -9999))
else:
    filtered.sort(key=lambda r: -(r.get("over_edge") or -999))

# ---- Column config ----
COL_CFG = {
    "No-Vig Over %": st.column_config.NumberColumn(format="%.1f%%"),
    "Best Over":     st.column_config.NumberColumn(format="%+d"),
    "Best Under":    st.column_config.NumberColumn(format="%+d"),
    "Over Edge":     st.column_config.NumberColumn(format="%+.1f pp"),
    "FD Over":       st.column_config.NumberColumn(format="%+d"),
    "BR Over":       st.column_config.NumberColumn(format="%+d"),
    "FD Under":      st.column_config.NumberColumn(format="%+d"),
    "BR Under":      st.column_config.NumberColumn(format="%+d"),
}


def _flags(r):
    f = ""
    if (r.get("over_edge") or 0) >= 3.0:  f += "💎"
    if r.get("n_books", 0) >= 2:           f += "📊"
    return f


def _build_df(subset):
    if not subset:
        return pd.DataFrame()
    df_rows = []
    for r in subset:
        df_rows.append({
            "":              _flags(r),
            "Player":        r["player"],
            "Line":          f"O/U {r['line']}",
            "Game":          r["game"],
            "Kickoff":       _kickoff(r["first_pitch"]),
            "No-Vig Over %": r.get("nv_over_pct"),
            "FD Over":       r.get("over_FD"),
            "BR Over":       r.get("over_BR"),
            "FD Under":      r.get("under_FD"),
            "BR Under":      r.get("under_BR"),
            "Best Over":     r.get("best_over_price"),
            "Best Over @":   r.get("best_over_book"),
            "Best Under":    r.get("best_under_price"),
            "Best Under @":  r.get("best_under_book"),
            "Over Edge":     r.get("over_edge"),
            "# Books":       r.get("n_books"),
        })
    return pd.DataFrame(df_rows)


# ---- Summary stats ----
total_players = len({r["player"] for r in filtered})
total_games   = len({r["game"] for r in filtered})
value_ct      = sum(1 for r in filtered if (r.get("over_edge") or 0) >= 3.0)
two_book_ct   = sum(1 for r in filtered if r.get("n_books", 0) >= 2)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Players shown", total_players)
m2.metric("Games", total_games)
m3.metric("💎 Value plays", value_ct)
m4.metric("📊 Both books", two_book_ct)

st.divider()

# ---- Main table ----
if not filtered:
    st.info("No props match your current filters.")
else:
    df = _build_df(filtered)
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=COL_CFG)

    # ---- Value plays callout ----
    value_rows = [r for r in filtered if (r.get("over_edge") or 0) >= 3.0]
    if value_rows:
        st.divider()
        st.markdown("### 💎 Value Plays (Over edge ≥ 3 pp)")
        st.caption(
            "No-vig probability is higher than what the best available over price implies — "
            "the market is giving you a better-than-fair price on the over."
        )
        st.dataframe(
            _build_df(value_rows),
            use_container_width=True,
            hide_index=True,
            column_config=COL_CFG,
        )

st.divider()
with st.expander("🔍 Scan info"):
    st.write({
        "Events scanned":  dbg.get("n_events", 0),
        "Upcoming games":  dbg.get("n_upcoming", 0),
        "Outcomes parsed": dbg.get("outcomes_parsed", 0),
        "Market hits":     dbg.get("market_hits", {}),
    })
