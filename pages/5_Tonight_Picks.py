"""
Tonight's Picks — Mobile-friendly summary of the deep-dive analysis.

Reads tonight_picks/latest.json from GitHub (auto-updated when Claude does a
fresh slate analysis). Shows top plays in a clean phone-readable format.
"""
import os
import sys
import json
import urllib.request
import ssl as _ssl_compat
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd

_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="Tonight's Picks", page_icon="🌟", layout="centered")
st.title("🌟 Tonight's Picks")
st.caption(
    "Deep-dive analysis from the latest scan. Updated whenever new analysis "
    "is generated. Bookmark this page on your phone for instant access."
)

# ---------- Load picks JSON from GitHub ----------

RAW_URL = ("https://raw.githubusercontent.com/Theo1984-ai/MLB-DASHBOARD-/"
           "main/tonight_picks/latest.json")

@st.cache_data(ttl=300, show_spinner=False)
def load_picks():
    try:
        return json.loads(urllib.request.urlopen(
            RAW_URL, timeout=10, context=_UNVERIFIED_SSL).read())
    except Exception as e:
        return {"error": str(e)}


col_refresh, col_info = st.columns([1, 3])
with col_refresh:
    if st.button("🔄 Refresh", use_container_width=True):
        load_picks.clear()
        st.rerun()

data = load_picks()

if "error" in data:
    st.error(f"Could not load picks: {data['error']}")
    st.info(
        "If this is the first time, no picks have been published yet. "
        "Ask Claude to **'update tonight's picks'** to generate the JSON."
    )
    st.stop()

# Header info
generated = data.get("generated_at", "?")
date = data.get("date", "?")
analyst = data.get("analyst", "Claude")
intro = data.get("intro", "")

st.markdown(f"### 📅 {date}")
st.caption(f"Generated: {generated}  •  Analyst: {analyst}")
if intro:
    st.info(intro)

# Concentration plays — the strongest signals
if data.get("concentration_plays"):
    st.markdown("---")
    st.markdown("## 🔥 Concentration Plays")
    st.caption("Same player appearing in multiple +EV picks = systematic soft pricing.")
    for cp in data["concentration_plays"]:
        with st.expander(
            f"**{cp.get('player','?')}** "
            f"({cp.get('game','?')}) — "
            f"{cp.get('book','?')} soft on {cp.get('n_picks',0)} props",
            expanded=True,
        ):
            for pick in cp.get("picks", []):
                price = pick.get("price")
                price_s = f"{price:+d}" if isinstance(price, int) else str(price)
                ev = pick.get("ev")
                ev_s = f"${ev:+.2f}" if isinstance(ev, (int, float)) else str(ev)
                line = pick.get("line")
                line_s = f"{line}" if line is not None else "—"
                st.write(
                    f"- **{pick.get('market','?')} {pick.get('side','')} {line_s}** "
                    f"@ **{price_s}** ({pick.get('book','?')})  →  EV {ev_s}"
                )

# Top single picks
if data.get("top_picks"):
    st.markdown("---")
    st.markdown("## 🏆 Top Single Plays")
    df = pd.DataFrame(data["top_picks"])
    if not df.empty:
        cols = ["player", "game", "market", "side", "line", "price", "book", "ev", "fair_pct"]
        df = df.reindex(columns=cols).rename(columns={
            "player": "Player", "game": "Game", "market": "Mkt",
            "side": "Side", "line": "Line", "price": "Price",
            "book": "Book", "ev": "EV", "fair_pct": "Fair %",
        })
        st.dataframe(df, use_container_width=True, hide_index=True,
                     column_config={
                         "Price": st.column_config.NumberColumn(format="%+d"),
                         "EV": st.column_config.NumberColumn(format="$%+.2f"),
                         "Fair %": st.column_config.NumberColumn(format="%.1f%%"),
                     })

# Recommended bet card
if data.get("bet_card"):
    st.markdown("---")
    st.markdown("## 🎯 Recommended Bet Card")
    card = data["bet_card"]
    if isinstance(card, dict):
        for tier_name, picks in card.items():
            if not picks: continue
            st.markdown(f"### {tier_name}")
            for p in picks:
                stake = p.get("stake", "?")
                desc = p.get("description", p.get("pick", "?"))
                price = p.get("price")
                price_s = f"{price:+d}" if isinstance(price, int) else str(price)
                book = p.get("book", "?")
                st.write(f"- **{stake}** | {desc} **@ {price_s}** ({book})")

# Safe locks
if data.get("safe_locks"):
    st.markdown("---")
    st.markdown("## 🛡️ Safe Juicy Locks (high-probability)")
    for s in data["safe_locks"]:
        price = s.get("price")
        price_s = f"{price:+d}" if isinstance(price, int) else str(price)
        ev = s.get("ev")
        ev_s = f"${ev:+.2f}" if isinstance(ev, (int, float)) else str(ev)
        st.write(
            f"- **{s.get('player','?')}** {s.get('market','?')} "
            f"{s.get('side','')} {s.get('line','')} "
            f"@ **{price_s}** ({s.get('book','?')})  →  "
            f"{s.get('fair_pct','?')}% fair, EV {ev_s}"
        )

# NBA section (if present)
if data.get("nba_picks"):
    st.markdown("---")
    st.markdown("## 🏀 NBA Plays")
    st.caption(data.get("nba_game", "Tonight"))
    nba_df = pd.DataFrame(data["nba_picks"])
    if not nba_df.empty:
        cols = ["player", "market", "side", "line", "price", "book", "ev"]
        nba_df = nba_df.reindex(columns=cols).rename(columns={
            "player": "Player", "market": "Stat", "side": "Side",
            "line": "Line", "price": "Price", "book": "Book", "ev": "EV",
        })
        st.dataframe(nba_df, use_container_width=True, hide_index=True,
                     column_config={
                         "Price": st.column_config.NumberColumn(format="%+d"),
                         "EV": st.column_config.NumberColumn(format="$%+.2f"),
                     })

# Skip these (FADE picks)
if data.get("avoid"):
    st.markdown("---")
    st.markdown("## ⛔ Avoid Tonight")
    for a in data["avoid"]:
        st.write(f"- {a}")

# Notes / strategy
if data.get("strategy_notes"):
    st.markdown("---")
    st.markdown("## 🧠 Strategy Notes")
    for n in data["strategy_notes"]:
        st.write(f"- {n}")

st.markdown("---")
st.caption(
    "📱 Bookmark this page on your phone for one-tap access to tonight's analysis. "
    "Updated whenever Claude runs a fresh slate scan."
)
