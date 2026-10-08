"""
🎯 True Probability — NHL (75%+ consensus plays).

Shows only plays where the consensus of 3+ sharp books implies 75%+ true probability.
Covers player props (Goals, SOG, Points, Assists), alternate puck lines/totals,
mainline puck lines/totals, and moneylines.
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.nhl_true_prob_scanner import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="True Probability", page_icon="🎯", layout="wide")
st.title("🎯 True Probability — 75%+ Plays (NHL)")
st.caption(
    "All markets, all games — filtered to only show plays where the **consensus of 3+ "
    "sharp books** (DraftKings, FanDuel, Bovada, Pinnacle, BetRivers) implies a 75%+ "
    "true probability of hitting. Covers player props (Goals, SOG, Points, Assists), "
    "alternate puck lines / totals, mainline puck lines / totals, and moneylines.  \n"
    "**No price cap** — every play that clears the 75% probability filter is shown."
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


# ---------- Cached scan ----------

@st.cache_data(ttl=300, show_spinner=False)
def _cached_scan(include_alts):
    return scan(ODDS_KEY, include_alts=include_alts)


# ---------- Controls ----------

cc1, cc2, cc3 = st.columns([1, 1, 3])
with cc1:
    refresh_btn = st.button("🔄 Refresh Now", type="primary", use_container_width=True,
                            help="Clear cache and pull fresh data from the Odds API")
with cc2:
    show_alts = st.toggle("Include alternates", value=True,
                          help="Alternate puck lines and alt totals")
with cc3:
    st.caption("Cache refreshes every 5 min automatically. Hit Refresh to force.")

if refresh_btn:
    _cached_scan.clear()
    st.toast("Cache cleared. Pulling fresh data...", icon="🔄")


# ---------- Pull data ----------

with st.spinner("Fetching upcoming NHL games and scanning all markets…"):
    try:
        all_plays = _cached_scan(show_alts)
    except Exception as e:
        import traceback
        st.error(f"Scan failed: {e}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc()[:2000])
        st.stop()

if not all_plays:
    st.warning(
        "No plays at 75%+ true probability right now. "
        "This is normal earlier in the day before all sharp books post lines. "
        "Try refreshing closer to puck drop."
    )
    st.stop()

# ---------- Build display ----------

TAB_PROPS = ["Goals", "Shots on Goal", "Points", "Assists", "PP Points", "Anytime Goal"]
TAB_GAME  = ["Moneyline", "Puck Line", "Total", "Puck Line alt", "Total alt"]

df = pd.DataFrame(all_plays).rename(columns={
    "game":          "Game",
    "market":        "Market",
    "selection":     "Selection",
    "side":          "Side",
    "point":         "Line",
    "best_book":     "Best Book",
    "best_price":    "Best Price",
    "true_prob_pct": "True Prob %",
    "ev_per_100":    "EV/$100",
    "n_books":       "# Books",
})

# Add first-pitch display column
def _fp(iso):
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %#I:%M %p" if sys.platform == "win32" else "%a %-I:%M %p"
        return ct.strftime(fmt)
    except Exception:
        return "?"

df["First Pitch"] = df["first_pitch"].apply(_fp)

for col in ("True Prob %", "EV/$100", "Best Price", "Line"):
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

props_df = df[df["Market"].isin(TAB_PROPS)].copy()
games_df = df[df["Market"].isin(TAB_GAME)].copy()

games_count  = df["Game"].nunique()
n_props      = len(props_df)
n_game_lines = len(games_df)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Games", games_count)
m2.metric("Total Plays", len(df))
m3.metric("Props",       n_props)
m4.metric("Game Lines",  n_game_lines)

st.markdown(f"### 🎯 {len(df)} plays at 75%+ true probability")

DISPLAY_COLS = ["First Pitch", "Game", "Market", "Selection", "Best Book",
                "Best Price", "True Prob %", "EV/$100", "# Books"]

COL_CFG = {
    "True Prob %": st.column_config.NumberColumn(format="%.1f%%"),
    "EV/$100":     st.column_config.NumberColumn(format="$%+.2f"),
    "Best Price":  st.column_config.NumberColumn(format="%+d"),
    "Line":        st.column_config.NumberColumn(format="%.1f"),
}

t1, t2, t3 = st.tabs([
    f"📋 All ({len(df)})",
    f"🏒 Player Props ({n_props})",
    f"📊 Game Lines ({n_game_lines})",
])

with t1:
    st.dataframe(df[DISPLAY_COLS], use_container_width=True,
                 hide_index=True, column_config=COL_CFG)

with t2:
    if n_props == 0:
        st.info("No qualifying player props right now.")
    else:
        st.dataframe(props_df[DISPLAY_COLS], use_container_width=True,
                     hide_index=True, column_config=COL_CFG)

with t3:
    if n_game_lines == 0:
        st.info("No qualifying game lines right now. Most won't clear 75% unless "
                "the line is well off the mainline.")
    else:
        st.dataframe(games_df[DISPLAY_COLS], use_container_width=True,
                     hide_index=True, column_config=COL_CFG)


# ---------- Top 5 detail expanders ----------

st.markdown("---")
st.markdown("#### 🔍 Top 5 Highest True Probability — every book's price side by side")

for r in all_plays[:5]:
    pt_str = f"{r['point']}" if r.get("point") is not None else "—"
    with st.expander(
        f"**{r['selection']}** • {r['market']} {r['side']} {pt_str} • "
        f"{r['game']} • {_fp(r['first_pitch'])} • "
        f"TrueProb {r['true_prob_pct']:.1f}% • EV ${r['ev_per_100']:+.2f}/$100",
        expanded=True,
    ):
        price_list = "  |  ".join(
            f"{bk}: {pr:+d}" for bk, pr in r.get("all_prices", [])
        )
        st.write(
            f"Best book: **{r['best_book']}** @ **{r['best_price']:+d}** — "
            f"{r['n_books']} sharp books  \n"
            f"{price_list}"
        )


# ---------- Performance / Forward-Test Tracking ----------

st.markdown("---")
st.markdown("### 📈 Forward-Test Performance")
st.caption(
    "Daily snapshots of the 75%+ picks, settled the next morning vs actual results. "
    "Accumulating from the first snapshot forward."
)

HISTORY_DIR = os.path.join(ROOT, "nhl_true_prob_history")


@st.cache_data(ttl=600, show_spinner=False)
def _load_history():
    if not os.path.isdir(HISTORY_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(HISTORY_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(HISTORY_DIR, fn)) as f:
                out.append(json.load(f))
        except Exception:
            pass
    return out


history = _load_history()

if not history:
    st.info(
        "No history snapshots yet. Snapshots are taken automatically once daily. "
        "Results settle the next morning."
    )
else:
    all_picks = []
    for snap in history:
        for p in snap.get("picks", []):
            p2 = dict(p)
            p2["snapshot_date"] = snap.get("date")
            all_picks.append(p2)

    settled   = [p for p in all_picks if p.get("result") in ("WIN", "LOSS", "PUSH")]
    wins      = sum(1 for p in settled if p["result"] == "WIN")
    losses    = sum(1 for p in settled if p["result"] == "LOSS")
    pushes    = sum(1 for p in settled if p["result"] == "PUSH")
    settled_n = wins + losses
    hit_rate  = wins / settled_n * 100 if settled_n else 0

    risk_total = profit_total = 0.0
    for p in settled:
        if p["result"] == "PUSH":
            continue
        am = p.get("best_price", 0)
        risk = 100 if am > 0 else abs(am)
        payout = am if am > 0 else 100
        risk_total  += risk
        profit_total += payout if p["result"] == "WIN" else -risk
    roi = profit_total / risk_total * 100 if risk_total else 0

    avg_pred  = sum(p.get("true_prob_pct", 0) for p in settled) / len(settled) if settled else 0
    cal_gap   = hit_rate - avg_pred if settled_n else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Picks",  f"{len(all_picks)}", f"{settled_n} settled")
    c2.metric("Hit Rate",     f"{hit_rate:.1f}%",
              f"vs predicted {avg_pred:.1f}%" if settled_n else "no data")
    c3.metric("Calibration",  f"{cal_gap:+.1f}pp",
              "Good" if abs(cal_gap) < 3 else ("Hot" if cal_gap > 0 else "Cold"))
    c4.metric("Net Profit",   f"${profit_total:+.0f}", f"on ${risk_total:.0f}")
    c5.metric("ROI",          f"{roi:+.1f}%", "per $ risked")

    # Day-by-day table
    st.markdown("#### Day-by-day")
    day_rows = []
    for snap in history:
        s = snap.get("summary") or {}
        day_rows.append({
            "Date":    snap.get("date"),
            "Picks":   snap.get("n_picks"),
            "Settled": s.get("n_settled", "pending") if s else "pending",
            "W-L-P":   f"{s.get('wins',0)}-{s.get('losses',0)}-{s.get('pushes',0)}" if s else "—",
            "Hit %":   s.get("hit_rate"),
            "Net":     s.get("profit_total"),
            "ROI %":   s.get("roi_pct"),
        })
    if day_rows:
        st.dataframe(
            pd.DataFrame(day_rows), use_container_width=True, hide_index=True,
            column_config={
                "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
                "Net":    st.column_config.NumberColumn(format="$%+.0f"),
                "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
            },
        )

    # By-market breakdown
    if settled_n > 0:
        st.markdown("#### Performance by market")
        by_mkt = defaultdict(lambda: {"w": 0, "l": 0, "risk": 0.0, "profit": 0.0})
        for p in settled:
            am  = p.get("best_price", 0)
            risk = 100 if am > 0 else abs(am)
            payout = am if am > 0 else 100
            mkt = p.get("market", "?")
            if p["result"] == "WIN":
                by_mkt[mkt]["w"] += 1
                by_mkt[mkt]["risk"]   += risk
                by_mkt[mkt]["profit"] += payout
            elif p["result"] == "LOSS":
                by_mkt[mkt]["l"] += 1
                by_mkt[mkt]["risk"]   += risk
                by_mkt[mkt]["profit"] -= risk
        mkt_rows = []
        for mkt, s in sorted(by_mkt.items(), key=lambda x: -(x[1]["w"] + x[1]["l"])):
            n = s["w"] + s["l"]
            if n == 0:
                continue
            mkt_rows.append({
                "Market": mkt,
                "W-L":    f"{s['w']}-{s['l']}",
                "Hit %":  round(s["w"] / n * 100, 1),
                "Net":    round(s["profit"], 2),
                "ROI %":  round(s["profit"] / s["risk"] * 100, 1) if s["risk"] else 0,
            })
        if mkt_rows:
            st.dataframe(
                pd.DataFrame(mkt_rows), use_container_width=True, hide_index=True,
                column_config={
                    "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
                    "Net":    st.column_config.NumberColumn(format="$%+.0f"),
                    "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
                },
            )

st.markdown("---")
st.caption(
    f"Last refresh: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
    f"Min books: 3  •  Min true prob: 75%  •  Min EV: $3/$100  •  "
    f"Books: DraftKings, FanDuel, Bovada, Pinnacle, BetRivers  •  Cache TTL: 5 min"
)
