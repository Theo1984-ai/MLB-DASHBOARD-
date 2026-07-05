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

# Ensure project root on sys.path so the Update button can import
# scripts.picks_updater (the shared scanner module).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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


# ---------- Auth for Update button ----------
def resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


ODDS_KEY = resolve_secret("THE_ODDS_API_KEY")
GH_TOKEN = resolve_secret("GITHUB_TOKEN")

# ---------- Buttons ----------
col_update, col_refresh, col_spacer = st.columns([1.4, 1, 2])

with col_update:
    update_btn = st.button(
        "⚡ Update Now",
        type="primary",
        use_container_width=True,
        help="Run a fresh scan of tonight's slate (10-30 sec)",
    )

with col_refresh:
    if st.button("🔄 Reload", use_container_width=True,
                 help="Reload the cached picks file"):
        load_picks.clear()
        st.rerun()

# ---------- Update Now handler ----------
if update_btn:
    if not ODDS_KEY:
        st.error("❌ THE_ODDS_API_KEY secret not configured. Cannot run a fresh scan.")
    else:
        with st.spinner("🔍 Scanning MLB + NBA slates... (10-30 sec)"):
            try:
                # Defensive import in case the cron module isn't loaded yet
                sys.path.insert(0, ROOT)
                from scripts.picks_updater import run_scan
                fresh = run_scan(ODDS_KEY)
                # Push to GitHub so other devices see it too
                if GH_TOKEN:
                    try:
                        from data import github_storage as gh
                        gh.save_json(
                            GH_TOKEN, "Theo1984-ai", "MLB-DASHBOARD-",
                            "tonight_picks/latest.json", fresh,
                            commit_msg=f"Update tonight's picks (manual) {fresh.get('date','')}",
                        )
                        st.success(
                            f"✅ Fresh scan complete — {len(fresh.get('top_picks', []))} top picks loaded. "
                            f"Pushed to GitHub (other devices will see it on next reload)."
                        )
                    except Exception as e_gh:
                        st.warning(f"Scan complete but couldn't push to GitHub: {e_gh}")
                else:
                    st.success(
                        f"✅ Fresh scan complete — {len(fresh.get('top_picks', []))} top picks loaded. "
                        f"(Note: GITHUB_TOKEN not set, so this update is only on your current view.)"
                    )
                # Display fresh data immediately by injecting into the load_picks cache
                load_picks.clear()
                st.session_state["_fresh_picks"] = fresh
            except Exception as e:
                st.error(f"❌ Scan failed: {e}")

# ---------- Load data: prefer in-session fresh data, fall back to GitHub ----------
if "_fresh_picks" in st.session_state:
    data = st.session_state["_fresh_picks"]
else:
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

# ---------- 🏆 Soft Scanner A-Grade (highest-ROI system, +14.7%) ----------
# This is the single most profitable system on the dashboard (302 picks,
# 59.9% hit rate, +14.7% ROI). Elevated to the top of Tonight's Picks so
# users see it before scrolling. Reads from soft_scanner_history if today's
# snapshot exists.
def _load_soft_agrade():
    today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    path = os.path.join(ROOT, "soft_scanner_history", f"{today_et}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
        picks = snap.get("picks", [])
    except Exception:
        return []
    # A-Grade filter (matches Data Status page + Soft Scanner logic):
    # H+R+R or Hits AND (5+ books OR price <= -150 OR edge >= 10pp)
    a = []
    for p in picks:
        m = p.get("market", "")
        nb = p.get("n_books", 0)
        price = p.get("best_price", 0)
        ep = p.get("edge_pp", 0)
        if m in ("H+R+R", "Hits") and (nb >= 5 or price <= -150 or ep >= 10):
            a.append(p)
    return sorted(a, key=lambda x: -(x.get("edge_pp") or 0))[:8]

_a_grade = _load_soft_agrade()
if _a_grade:
    st.markdown("---")
    st.markdown("## 🏆 Soft A-Grade (highest-ROI system: +14.7%)")
    st.caption(
        "Soft Scanner A-Grade picks — H+R+R or Hits with 5+ books, "
        "price ≤ −150, or ≥10pp edge. Backtested at 59.9% hit / +14.7% ROI "
        "over 302 picks. This is the most profitable system on the dashboard."
    )
    _rows = []
    for p in _a_grade:
        _rows.append({
            "Player":  p.get("player", "?"),
            "Team":    p.get("team") or "—",
            "Market":  p.get("market", "?"),
            "Side":    p.get("side", "?"),
            "Line":    p.get("point", "—"),
            "Price":   p.get("best_price"),
            "Book":    p.get("best_book", "?"),
            "Edge":    p.get("edge_pp"),
            "Books":   p.get("n_books"),
        })
    _adf = pd.DataFrame(_rows)
    st.dataframe(_adf, use_container_width=True, hide_index=True,
                 column_config={
                     "Price": st.column_config.NumberColumn(format="%+d"),
                     "Edge":  st.column_config.NumberColumn(format="%+.1fpp"),
                 })

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
