"""
🏈🎯 CFB True Probability — 75%+ consensus plays.

Scans CFB game lines and player props. Shows only plays where
the consensus of sharp books implies 75%+ true probability.
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

from scripts.cfb_true_prob_scanner import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="CFB True Probability", page_icon="🏈", layout="wide")
st.title("🏈🎯 CFB True Probability — 75%+ Plays")
st.caption(
    "All CFB markets filtered to plays where the **consensus of sharp books** "
    "implies 75%+ true probability. Covers player props (Pass Yds, Rush Yds, "
    "Rec Yds, Receptions, TDs), spreads, totals, and moneylines.  \n"
    "Min EV: $3/$100 · Min juice: -400 cap · Min 4 books."
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


@st.cache_data(ttl=300, show_spinner=False)
def cached_scan(include_alts):
    return scan(ODDS_KEY, include_alts=include_alts)


cc1, cc2, cc3 = st.columns([1, 1, 3])
with cc1:
    refresh_btn = st.button("🔄 Refresh Now", type="primary", use_container_width=True)
with cc2:
    show_alts = st.toggle("Include alt lines", value=True)
with cc3:
    st.caption("Cache refreshes every 5 min automatically.")

if refresh_btn:
    cached_scan.clear()
    st.toast("Cache cleared — pulling fresh CFB data...", icon="🔄")

with st.spinner("Scanning CFB sharp books..."):
    try:
        plays = cached_scan(include_alts=show_alts)
    except Exception as e:
        st.error(f"Scan failed: {e}")
        st.stop()

if not plays:
    st.warning(
        "No CFB plays at 75%+ true probability right now. "
        "Check back once lines are posted (typically Monday/Tuesday for the upcoming week). "
        "CFB has fewer prop markets than NFL."
    )
    st.stop()


def _fp_label(iso_str):
    try:
        ct = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %-I:%M %p ET" if sys.platform != "win32" else "%a %#I:%M %p ET"
        return ct.strftime(fmt)
    except Exception:
        return iso_str or "?"


all_rows = []
for p in plays:
    all_rows.append({
        "Kickoff":     _fp_label(p.get("first_pitch") or ""),
        "Game":        p["game"],
        "Market":      p["market"],
        "Selection":   p["selection"],
        "Side":        p["side"],
        "Line":        p.get("point"),
        "Best Book":   p["best_book"],
        "Best Price":  p["best_price"],
        "True Prob %": p["true_prob_pct"],
        "EV/$100":     p["ev_per_100"],
        "# Books":     p["n_books"],
    })

df = pd.DataFrame(all_rows)
for c in ("True Prob %", "EV/$100", "Best Price", "Line"):
    if c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")

PROP_MARKETS = {"Pass Yds", "Pass TDs", "Rush Yds", "Rush Att",
                "Rec Yds", "Receptions", "Anytime TD", "First TD"}
GAME_MARKETS = {"Moneyline", "Spread", "Total", "Spread alt", "Total alt"}

props_df = df[df["Market"].isin(PROP_MARKETS)].copy()
games_df = df[df["Market"].isin(GAME_MARKETS)].copy()

st.markdown(f"### 🎯 {len(df)} plays at 75%+ true probability")

t1, t2, t3 = st.tabs([
    f"📋 All ({len(df)})",
    f"🏈 Player Props ({len(props_df)})",
    f"📊 Game Lines ({len(games_df)})",
])

COL_CFG = {
    "True Prob %": st.column_config.NumberColumn(format="%.1f%%"),
    "EV/$100":     st.column_config.NumberColumn(format="$%+.2f"),
    "Best Price":  st.column_config.NumberColumn(format="%+d"),
    "Line":        st.column_config.NumberColumn(format="%.1f"),
}

with t1:
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=COL_CFG)
with t2:
    if len(props_df) == 0:
        st.info("No qualifying player props right now.")
    else:
        st.dataframe(props_df, use_container_width=True, hide_index=True, column_config=COL_CFG)
with t3:
    if len(games_df) == 0:
        st.info("No qualifying game lines right now.")
    else:
        st.dataframe(games_df, use_container_width=True, hide_index=True, column_config=COL_CFG)

st.markdown("---")
st.markdown("#### 🔍 Top 5 — every book's price side by side")
for p in plays[:5]:
    pt_str = f"{p['point']}" if p.get("point") is not None else "—"
    with st.expander(
        f"**{p['selection']}** • {p['market']} {p['side']} {pt_str} • "
        f"{p['game']} • {_fp_label(p['first_pitch'])} • "
        f"TrueProb {p['true_prob_pct']:.1f}% • EV ${p['ev_per_100']:+.2f}/$100",
        expanded=True,
    ):
        prices_str = "  ·  ".join(
            f"{bk}: {pr:+d}" for bk, pr in p.get("all_prices", [])
        )
        st.write(
            f"Best book: **{p['best_book']}** @ **{p['best_price']:+d}**  "
            f"({p['n_books']} sharp books)  \n{prices_str}"
        )

st.markdown("---")
st.caption(
    f"Last refresh: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
    f"Min true prob: 75%  •  Min books: 4  •  Min EV: $3/$100  •  Cache TTL: 5 min"
)
