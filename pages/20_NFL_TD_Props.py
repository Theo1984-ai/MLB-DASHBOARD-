"""
🏈🎯 NFL TD Props — full scorer board.

Shows every player priced on anytime TD, first TD, last TD, and QB
passing-TD over/unders.  Sorted by consensus true probability (most
likely scorers first).  Value flag when the best available price beats
consensus by 3 pts.  Sharp flag when the sharp books (DK/FD) have the
player at 5+ pts higher probability than lagging books.
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
    "All players priced for **Anytime TD**, **First TD**, **Last TD**, and "
    "**Pass TDs** (O/U).  Sourced from DK · FD · MGM · CZR · BOV · PIN.  \n"
    "**Consensus %** = average true probability across all priced books.  "
    "**💎 Value** = best available price beats consensus by 3+ pts.  "
    "**⚡ Sharp** = DK/FD have player 5+ pts higher than lag books (sharp steam)."
)


# ---------- Secrets ----------

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


# ---------- Controls ----------

cc1, cc2, cc3, cc4 = st.columns([1, 1, 1, 2])
with cc1:
    refresh = st.button("🔄 Refresh", type="primary", use_container_width=True)
with cc2:
    min_books = st.number_input("Min books", min_value=1, max_value=6, value=2,
                                help="Minimum number of books pricing the prop")
with cc3:
    value_only = st.toggle("💎 Value only", value=False,
                           help="Show only plays where best price beats consensus by 3+ pts")
with cc4:
    game_filter = st.text_input("Filter by team", placeholder="e.g. Chiefs, Mahomes",
                                help="Case-insensitive substring match on player or game")

if refresh:
    st.cache_data.clear()
    st.toast("Cache cleared — scanning TD props...", icon="🔄")


# ---------- Fetch ----------

@st.cache_data(ttl=180, show_spinner="Scanning NFL TD props across 6 books...")
def _cached_scan(api_key):
    return scan(api_key, sport="americanfootball_nfl")


with st.spinner("Scanning NFL TD props..."):
    try:
        rows = _cached_scan(ODDS_KEY)
    except Exception as e:
        import traceback
        st.error(f"Scan failed: {e}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc()[:2000])
        st.stop()

if not rows:
    st.warning(
        "No NFL TD props found right now — lines may not be posted yet "
        "(typically available Tuesday/Wednesday before game week). Check back later."
    )
    st.stop()


# ---------- Filter ----------

def _kickoff(iso):
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %#I:%M %p" if sys.platform == "win32" else "%a %-I:%M %p"
        return ct.strftime(fmt)
    except Exception:
        return iso or "?"


filtered = [r for r in rows if r["n_books"] >= min_books]
if value_only:
    filtered = [r for r in filtered if r["value_edge"] >= 3.0]
if game_filter.strip():
    q = game_filter.strip().lower()
    filtered = [r for r in filtered if
                q in r["player"].lower() or q in r["game"].lower()
                or q in r["away_team"].lower() or q in r["home_team"].lower()]

# Partition by market
anytime  = [r for r in filtered if r["market"] == "Anytime TD"]
first_td = [r for r in filtered if r["market"] == "First TD"]
last_td  = [r for r in filtered if r["market"] == "Last TD"]
pass_tds = [r for r in filtered if r["market"] == "Pass TDs"]

st.markdown(f"### 🏈 {len(filtered)} TD props across {len({r['game'] for r in filtered})} games")

# Tabs
t1, t2, t3, t4 = st.tabs([
    f"🏃 Anytime TD ({len(anytime)})",
    f"🥇 First TD ({len(first_td)})",
    f"🏆 Last TD ({len(last_td)})",
    f"📊 Pass TDs ({len(pass_tds)})",
])

BOOK_COLS  = ["DK", "FD", "MGM", "CZR", "BOV", "PIN"]
PRICE_COLS = [f"price_{b}" for b in BOOK_COLS]

COL_CFG = {
    "Consensus %":  st.column_config.NumberColumn(format="%.1f%%"),
    "Best Price":   st.column_config.NumberColumn(format="%+d"),
    "Value Edge":   st.column_config.NumberColumn(format="%+.1f pp"),
    "Sharp Gap":    st.column_config.NumberColumn(format="%+.1f pp"),
    **{b: st.column_config.NumberColumn(label=b, format="%+d")
       for b in BOOK_COLS},
}


def _flags(r):
    flags = ""
    if r["value_edge"] >= 3.0:
        flags += "💎"
    if r["sharp_gap"] >= 5.0:
        flags += "⚡"
    return flags


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
        if r["market"] == "Pass TDs":
            row["Side"] = r["side"]
            row["Line"] = r.get("point")
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


def _render(subset, pass_td=False):
    if not subset:
        st.info("No props in this bucket with current filters.")
        return

    # Summary bar
    n_value = sum(1 for r in subset if r["value_edge"] >= 3.0)
    n_sharp = sum(1 for r in subset if r["sharp_gap"] >= 5.0)
    games   = sorted({r["game"] for r in subset})
    m1, m2, m3 = st.columns(3)
    m1.metric("Players priced", len(subset))
    m2.metric("💎 Value plays", n_value)
    m3.metric("⚡ Sharp plays", n_sharp)

    # Game filter inside tab
    game_sel = st.multiselect("Filter by game", options=games, default=[],
                               key=f"game_{subset[0]['market']}")
    show = [r for r in subset if (not game_sel or r["game"] in game_sel)]

    df = _build_df(show)
    if df.empty:
        st.info("No results.")
        return

    cfg = dict(COL_CFG)
    if not pass_td and "Side" in df.columns:
        df = df.drop(columns=["Side", "Line"], errors="ignore")
    if pass_td:
        cfg["Line"] = st.column_config.NumberColumn(format="%.1f")

    st.dataframe(df, use_container_width=True, hide_index=True, column_config=cfg)

    # Top 5 expander — most likely scorers with full price comparison
    st.markdown("##### 🔍 Highest probability — all book prices")
    for r in show[:5]:
        flag = _flags(r)
        pt   = f" {r['side']} {r['point']}" if r.get("point") is not None else ""
        with st.expander(
            f"{flag} **{r['player']}** — {r['game']} {_kickoff(r['first_pitch'])}  "
            f"·  Consensus {r['consensus_prob']:.1f}%  ·  Best {r['best_price']:+d} @ {r['best_book']}",
            expanded=False,
        ):
            price_strs = []
            for b, pc in zip(BOOK_COLS, PRICE_COLS):
                p = r.get(pc)
                if p is not None:
                    price_strs.append(f"**{b}** {p:+d}")
            st.write("  ·  ".join(price_strs))
            st.write(
                f"Value edge: **{r['value_edge']:+.1f} pp** at {r['best_book']}  ·  "
                f"Sharp gap: **{r['sharp_gap']:+.1f} pp** (DK/FD vs lag books)"
            )


with t1:
    _render(anytime)

with t2:
    _render(first_td)

with t3:
    _render(last_td)

with t4:
    _render(pass_tds, pass_td=True)


# ---------- Cross-market view for top scorers ----------

st.markdown("---")
st.subheader("🔀 Cross-market — same player priced in 2+ markets")
st.caption(
    "Players appearing in multiple TD markets (e.g., priced for both Anytime "
    "and First TD) show up here. Useful for multi-leg correlation research."
)

player_markets: dict[str, list] = {}
for r in filtered:
    player_markets.setdefault(r["player"], []).append(r)

cross = {p: ms for p, ms in player_markets.items() if len(ms) >= 2}

if cross:
    cross_rows = []
    for player, ms in sorted(cross.items(), key=lambda x: -max(r["consensus_prob"] for r in x[1])):
        for r in ms:
            cross_rows.append({
                "Player":       player,
                "Market":       r["market"],
                "Game":         r["game"],
                "Consensus %":  r["consensus_prob"],
                "Best Price":   r["best_price"],
                "Best Book":    r["best_book"],
                "Value Edge":   r["value_edge"],
            })
    cross_df = pd.DataFrame(cross_rows)
    st.dataframe(cross_df, use_container_width=True, hide_index=True,
                 column_config={
                     "Consensus %": st.column_config.NumberColumn(format="%.1f%%"),
                     "Best Price":  st.column_config.NumberColumn(format="%+d"),
                     "Value Edge":  st.column_config.NumberColumn(format="%+.1f pp"),
                 })
else:
    st.info("No players priced in 2+ TD markets right now.")


st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 3 min  ·  "
    f"Books: DK · FD · MGM · CZR · BOV · PIN  ·  "
    f"💎 Value = best price beats consensus 3+ pp  ·  "
    f"⚡ Sharp = DK/FD 5+ pp higher than lag books"
)
