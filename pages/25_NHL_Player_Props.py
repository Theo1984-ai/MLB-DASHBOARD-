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
    "**Goals** and **Shots on Goal** O/U props — FanDuel + BetRivers.  \n"
    "**No-Vig Over %** = true probability (vig removed).  "
    "**💎 Value** = best price beats no-vig by 3+ pp.  "
    "**📊** = both books posted this player."
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


@st.cache_data(ttl=300, show_spinner=False)
def _cached_scan(api_key):
    return scan(api_key)


col_r, col_s = st.columns([1, 5])
with col_r:
    if st.button("🔄 Refresh", type="primary"):
        st.cache_data.clear()
        st.rerun()

with st.spinner("Loading NHL player props + context stats…"):
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


def _toi_fmt(seconds):
    if seconds is None:
        return None
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


# ════════════════════════════════════════════════
# ACTIONABLE PLAYS — shown before sidebar filters
# ════════════════════════════════════════════════
EDGE_THRESHOLD = 3.0

# Collect all value plays (over or under edge ≥ threshold)
action_rows = []
for r in rows:
    oe = r.get("over_edge") or 0
    ue = r.get("under_edge") or 0
    if oe >= EDGE_THRESHOLD:
        action_rows.append((r, "Over",  oe,
                            r.get("best_over_price"),
                            r.get("best_over_book"),
                            r.get("nv_over_pct")))
    elif ue >= EDGE_THRESHOLD:
        action_rows.append((r, "Under", ue,
                            r.get("best_under_price"),
                            r.get("best_under_book"),
                            r.get("nv_under_pct")))

# Sort by edge descending
action_rows.sort(key=lambda x: -x[2])

if action_rows:
    st.markdown("## 🎯 Actionable Plays")
    st.caption(f"Props where the best available price beats no-vig probability by {EDGE_THRESHOLD}+ pp")

    for r, side, edge, price, book, nv_pct in action_rows:
        mkt   = r["market"]           # "Goals" / "Shots on Goal"
        line  = r["line"]
        player = r["player"]
        game  = r["game"]
        ko    = _kickoff(r["first_pitch"])
        toi   = _toi_fmt(r.get("avg_toi_s"))

        price_str = f"{price:+d}" if price is not None else "N/A"
        nv_str    = f"{nv_pct:.1f}%" if nv_pct is not None else "?"
        edge_str  = f"+{edge:.1f} pp"
        toi_str   = f"TOI {toi}" if toi else ""

        # Human-readable market label
        if mkt == "Goals" and line == 0.5:
            mkt_label = "🥅 Anytime Goal"
        elif mkt == "Goals":
            mkt_label = f"🥅 Goals O/U {line}"
        else:
            mkt_label = f"🏒 Shots on Goal O/U {line}"

        arrow = "⬆️" if side == "Over" else "⬇️"

        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
            with c1:
                st.markdown(f"**{player}**")
                st.caption(f"{game} · {ko}")
            with c2:
                st.markdown(f"**{mkt_label}**")
                st.markdown(f"{arrow} **{side}**")
                if toi_str:
                    st.caption(toi_str)
            with c3:
                st.markdown(f"**{price_str}** @ {book or '?'}")
                st.caption(f"No-Vig: {nv_str}")
            with c4:
                st.metric("Edge", edge_str)

    st.divider()
else:
    st.info("No value plays right now (no prop has ≥ 3 pp edge). Check back closer to puck drop.")
    st.divider()


# ════════════════════════════════
# Sidebar filters
# ════════════════════════════════
st.sidebar.markdown("### 🏒 Filters")

all_markets = ["Goals", "Shots on Goal"]
market_sel = st.sidebar.radio("Market", all_markets, index=0)

side_sel = st.sidebar.radio(
    "Side", ["Over", "Under", "Both"], index=0,
    help="Focus the table and value callout on Overs, Unders, or show both columns"
)

st.sidebar.divider()

all_teams = sorted({t for r in rows for t in [r["away_team"], r["home_team"]]})
team_sel = st.sidebar.multiselect(
    "Team", options=all_teams, default=[],
    help="Show only players from these teams"
)

all_games = sorted({r["game"] for r in rows})
game_sel = st.sidebar.multiselect(
    "Game", options=all_games, default=[],
    help="Leave empty to show all games"
)

all_lines = sorted({r["line"] for r in rows if r["market"] == market_sel})
line_sel = st.sidebar.multiselect(
    "Line (O/U)", options=[str(l) for l in all_lines], default=[],
    help="Leave empty to show all lines"
)

player_search = st.sidebar.text_input("Player search", placeholder="e.g. Matthews, Kaprizov")

st.sidebar.divider()
st.sidebar.markdown("**Odds & probability**")

oc1, oc2 = st.sidebar.columns(2)
min_over_price = oc1.number_input("Best Over min", value=-300, step=10)
max_over_price = oc2.number_input("Best Over max", value=1000, step=10)

min_nv = st.sidebar.slider("Min No-Vig Over %", 0, 80, 0, step=5)
max_nv = st.sidebar.slider("Max No-Vig Over %", 20, 100, 100, step=5)

st.sidebar.divider()
st.sidebar.markdown("**Player context**")

all_toi = [r["avg_toi_s"] for r in rows if r.get("avg_toi_s")]
if all_toi:
    max_toi_min = int(max(all_toi) / 60) + 1
    toi_min_filter = st.sidebar.slider(
        "Min avg TOI (min/game)", 0, max_toi_min, 0, step=1,
        help="Filter to players averaging at least this many minutes on ice"
    )
else:
    toi_min_filter = 0

all_opp_ga = [r["opp_ga_pgp"] for r in rows if r.get("opp_ga_pgp") is not None]
if all_opp_ga:
    max_ga = max(all_opp_ga)
    opp_ga_max = st.sidebar.slider(
        "Max opp GA/game", 1.0, max(max_ga, 6.0), max(max_ga, 6.0), step=0.5,
        help="Higher = weaker defense. Lower this to only see players facing weak teams."
    )
else:
    opp_ga_max = 99.0

st.sidebar.divider()

value_only    = st.sidebar.toggle("💎 Value plays only (3+ pp edge)", value=False)
two_book_only = st.sidebar.toggle("📊 Both books only", value=False)

sort_by = st.sidebar.radio(
    "Sort by",
    ["No-Vig Over % (highest)", "Best Over Price", "Value Edge", "Avg TOI"],
    index=0,
)

# ---- Apply filters ----
filtered = [r for r in rows if r["market"] == market_sel]
if team_sel:
    filtered = [r for r in filtered if r["away_team"] in team_sel or r["home_team"] in team_sel]
if game_sel:
    filtered = [r for r in filtered if r["game"] in game_sel]
if line_sel:
    filtered = [r for r in filtered if str(r["line"]) in line_sel]
if player_search.strip():
    q = player_search.strip().lower()
    filtered = [r for r in filtered if q in r["player"].lower()]
filtered = [r for r in filtered
            if r.get("best_over_price") is not None
            and min_over_price <= r["best_over_price"] <= max_over_price]
if min_nv > 0 or max_nv < 100:
    filtered = [r for r in filtered
                if min_nv <= (r.get("nv_over_pct") or 0) <= max_nv]
if toi_min_filter > 0:
    filtered = [r for r in filtered
                if (r.get("avg_toi_s") or 0) / 60 >= toi_min_filter]
if all_opp_ga and opp_ga_max < max(all_opp_ga):
    filtered = [r for r in filtered
                if r.get("opp_ga_pgp") is None or r["opp_ga_pgp"] <= opp_ga_max]
if value_only:
    filtered = [r for r in filtered if (r.get("over_edge") or 0) >= EDGE_THRESHOLD]
if two_book_only:
    filtered = [r for r in filtered if r.get("n_books", 0) >= 2]

# ---- Sort ----
if sort_by == "No-Vig Over % (highest)":
    filtered.sort(key=lambda r: -(r.get("nv_over_pct") or 0))
elif sort_by == "Best Over Price":
    filtered.sort(key=lambda r: -(r.get("best_over_price") or -9999))
elif sort_by == "Value Edge":
    filtered.sort(key=lambda r: -(r.get("over_edge") or -999))
else:
    filtered.sort(key=lambda r: -(r.get("avg_toi_s") or 0))

# ---- Column config ----
COL_CFG = {
    "No-Vig Over %": st.column_config.NumberColumn(format="%.1f%%"),
    "Best Over":     st.column_config.NumberColumn(format="%+d"),
    "Best Under":    st.column_config.NumberColumn(format="%+d"),
    "Over Edge":     st.column_config.NumberColumn(format="%+.1f pp"),
    "Under Edge":    st.column_config.NumberColumn(format="%+.1f pp"),
    "FD Over":       st.column_config.NumberColumn(format="%+d"),
    "BR Over":       st.column_config.NumberColumn(format="%+d"),
    "FD Under":      st.column_config.NumberColumn(format="%+d"),
    "BR Under":      st.column_config.NumberColumn(format="%+d"),
    "G/GP":          st.column_config.NumberColumn(format="%.2f"),
    "S/GP":          st.column_config.NumberColumn(format="%.2f"),
    "Opp GA/G":      st.column_config.NumberColumn(format="%.2f"),
}


def _flags(r):
    f = ""
    if (r.get("over_edge") or 0) >= EDGE_THRESHOLD: f += "💎"
    if (r.get("under_edge") or 0) >= EDGE_THRESHOLD: f += "💎"
    if r.get("n_books", 0) >= 2: f += "📊"
    return f


def _build_df(subset, market):
    if not subset:
        return pd.DataFrame()
    is_goals = market == "Goals"
    df_rows = []
    for r in subset:
        if market == "Goals" and r["line"] == 0.5:
            line_label = "ATG (0.5G)"
        elif market == "Goals":
            line_label = f"Goals O/U {r['line']}"
        else:
            line_label = f"SOG O/U {r['line']}"

        row = {
            "":              _flags(r),
            "Player":        r["player"],
            "Line":          line_label,
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
            "Under Edge":    r.get("under_edge"),
            "# Books":       r.get("n_books"),
            "TOI":           _toi_fmt(r.get("avg_toi_s")),
            "GP":            r.get("player_gp"),
            "Opp GA/G":      r.get("opp_ga_pgp"),
        }
        if is_goals:
            row["G/GP"] = r.get("goals_pgp")
        else:
            row["S/GP"] = r.get("shots_pgp")
        df_rows.append(row)
    return pd.DataFrame(df_rows)


# ---- Summary metrics ----
total_players = len({r["player"] for r in filtered})
total_games   = len({r["game"] for r in filtered})
value_ct      = sum(1 for r in filtered
                    if (r.get("over_edge") or 0) >= EDGE_THRESHOLD
                    or (r.get("under_edge") or 0) >= EDGE_THRESHOLD)
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
    df = _build_df(filtered, market_sel)
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=COL_CFG)

st.divider()
with st.expander("🔍 Scan info"):
    st.write({
        "Events scanned":  dbg.get("n_events", 0),
        "Upcoming games":  dbg.get("n_upcoming", 0),
        "Outcomes parsed": dbg.get("outcomes_parsed", 0),
        "Market hits":     dbg.get("market_hits", {}),
        "Skaters loaded":  dbg.get("n_skaters_loaded", 0),
        "Errors":          dbg.get("errors", []),
    })
