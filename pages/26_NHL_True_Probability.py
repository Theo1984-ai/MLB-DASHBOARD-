"""
🎯 NHL True Probability — moneyline, game total, and team totals.
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

from scripts.nhl_probability_scanner import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
EDGE_GOOD    =  0.0   # true value (positive edge)
EDGE_LOW     = -2.0   # low juice
EDGE_AVOID   = -4.5   # heavy juice, avoid

st.set_page_config(page_title="NHL True Probability", page_icon="🎯", layout="wide")
st.title("🎯 NHL True Probability")
st.caption(
    "**True probability** = no-vig consensus across Fanatics + FanDuel.  "
    "**Edge** = true% minus best-price implied% — closer to 0 means less juice.  "
    "💎 positive = rare value · ✅ ≥ −2pp = low juice · ⚠️ ≥ −4.5pp = standard · ❌ worse"
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
def _cached_scan(key):
    return scan(key)


col_r, _ = st.columns([1, 5])
with col_r:
    if st.button("🔄 Refresh", type="primary"):
        st.cache_data.clear()
        st.rerun()

with st.spinner("Loading NHL true probabilities…"):
    try:
        games, dbg = _cached_scan(ODDS_KEY)
    except Exception as e:
        import traceback
        st.error(f"Scan failed: {e}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc()[:2000])
        st.stop()

if not games:
    st.warning("No upcoming NHL games found right now.")
    with st.expander("Debug"):
        st.write(dbg)
    st.stop()


def _kickoff(iso):
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %#I:%M %p" if sys.platform == "win32" else "%a %-I:%M %p"
        return ct.strftime(fmt)
    except Exception:
        return "?"


def _edge_icon(edge):
    if edge is None:
        return ""
    if edge >= EDGE_GOOD:
        return "💎"
    if edge >= EDGE_LOW:
        return "✅"
    if edge >= EDGE_AVOID:
        return "⚠️"
    return "❌"


def _prob_bar(pct):
    """Simple ASCII-style probability bar for readability."""
    if pct is None:
        return "?"
    filled = round(pct / 5)
    return f"{'█' * filled}{'░' * (20 - filled)} {pct:.1f}%"


# ═══════════════════════════════════════════════════
# Collect all plays — for best-value section
# ═══════════════════════════════════════════════════
all_plays = []

for g in games:
    ko = _kickoff(g["commence_time"])

    # Moneyline
    if g.get("ml"):
        ml = g["ml"]
        for team, nv, price, book, edge in (
            (g["away_team"], ml["away_nv"], ml["best_away_price"], ml["best_away_book"], ml["away_edge"]),
            (g["home_team"], ml["home_nv"], ml["best_home_price"], ml["best_home_book"], ml["home_edge"]),
        ):
            all_plays.append({
                "game": g["game"], "ko": ko,
                "team": team, "market": "Moneyline", "line": None,
                "true_pct": nv, "best_price": price, "book": book, "edge": edge,
            })

    # Game total
    if g.get("gt"):
        gt = g["gt"]
        for side, nv, price, book, edge in (
            ("Over",  gt["over_nv"],  gt["best_over_price"],  gt["best_over_book"],  gt["over_edge"]),
            ("Under", gt["under_nv"], gt["best_under_price"], gt["best_under_book"], gt["under_edge"]),
        ):
            all_plays.append({
                "game": g["game"], "ko": ko,
                "team": f"Game O/U {gt['line']} — {side}", "market": "Game Total", "line": gt["line"],
                "true_pct": nv, "best_price": price, "book": book, "edge": edge,
            })

    # Team totals
    for side_key, team_name in (("away_tt", g["away_team"]), ("home_tt", g["home_team"])):
        tt = g.get(side_key)
        if not tt:
            continue
        for s, nv, price, book, edge in (
            ("Over",  tt["over_nv"],  tt["best_over_price"],  tt["best_over_book"],  tt["over_edge"]),
            ("Under", tt["under_nv"], tt["best_under_price"], tt["best_under_book"], tt["under_edge"]),
        ):
            all_plays.append({
                "game": g["game"], "ko": ko,
                "team": f"{team_name} O/U {tt['line']} — {s}", "market": "Team Total", "line": tt["line"],
                "true_pct": nv, "best_price": price, "book": book, "edge": edge,
            })

# Sort by edge descending (best relative value first)
all_plays.sort(key=lambda x: -(x["edge"] or -99))


# ═══════════════════════════════════════════════════
# Summary metrics
# ═══════════════════════════════════════════════════
n_value  = sum(1 for p in all_plays if (p["edge"] or -99) >= EDGE_GOOD)
n_low    = sum(1 for p in all_plays if EDGE_LOW <= (p["edge"] or -99) < EDGE_GOOD)
n_games  = len(games)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Games", n_games)
m2.metric("💎 True Value Plays", n_value)
m3.metric("✅ Low-Juice Plays", n_low)
m4.metric("Total Outcomes", len(all_plays))

st.divider()


# ═══════════════════════════════════════════════════
# Best Relative Value section (top 10 by edge)
# ═══════════════════════════════════════════════════
st.subheader("📊 Best Relative Value (least juice paid)")
st.caption("Sorted by edge — closest to 0 means you're paying the least vig. 💎 means the price actually beats true probability.")

top_plays = all_plays[:15]
if top_plays:
    rows = []
    for p in top_plays:
        price_str = f"{p['best_price']:+d}" if p["best_price"] is not None else "N/A"
        edge_str = f"{p['edge']:+.1f} pp" if p["edge"] is not None else "?"
        rows.append({
            "":          _edge_icon(p["edge"]),
            "Game":      p["game"],
            "Time":      p["ko"],
            "Bet":       p["team"],
            "Market":    p["market"],
            "True %":    p["true_pct"],
            "Best Price": p["best_price"],
            "@ Book":    p["book"] or "?",
            "Edge":      p["edge"],
        })
    df_top = pd.DataFrame(rows)
    st.dataframe(
        df_top, use_container_width=True, hide_index=True,
        column_config={
            "True %":    st.column_config.ProgressColumn("True %", min_value=0, max_value=100, format="%.1f%%"),
            "Best Price": st.column_config.NumberColumn(format="%+d"),
            "Edge":      st.column_config.NumberColumn("Edge (pp)", format="%+.1f"),
        },
    )

st.divider()


# ═══════════════════════════════════════════════════
# Tabs — per market type
# ═══════════════════════════════════════════════════
tab_ml, tab_gt, tab_tt = st.tabs(["🏒 Moneyline", "📊 Game Total", "🥅 Team Totals"])


# ── Moneyline tab ─────────────────────────────────
with tab_ml:
    ml_rows = []
    for g in games:
        if not g.get("ml"):
            continue
        ml = g["ml"]
        ko = _kickoff(g["commence_time"])
        for team, nv, price, book, edge, role in (
            (g["away_team"], ml["away_nv"], ml["best_away_price"], ml["best_away_book"], ml["away_edge"], "Away"),
            (g["home_team"], ml["home_nv"], ml["best_home_price"], ml["best_home_book"], ml["home_edge"], "Home"),
        ):
            fan_p = (ml.get("books") or {}).get("fanatics", {}).get("away" if role == "Away" else "home")
            fd_p  = (ml.get("books") or {}).get("fanduel",  {}).get("away" if role == "Away" else "home")
            ml_rows.append({
                "":          _edge_icon(edge),
                "Game":      g["game"],
                "Time":      ko,
                "Team":      team,
                "H/A":       role,
                "True %":    nv,
                "Fanatics":  fan_p,
                "FanDuel":   fd_p,
                "Best Price": price,
                "@ Book":    book or "?",
                "Edge":      edge,
            })
    if ml_rows:
        ml_rows.sort(key=lambda r: -(r["Edge"] or -99))
        df_ml = pd.DataFrame(ml_rows)
        st.dataframe(
            df_ml, use_container_width=True, hide_index=True,
            column_config={
                "True %":    st.column_config.ProgressColumn("True %", min_value=0, max_value=100, format="%.1f%%"),
                "Fanatics":  st.column_config.NumberColumn(format="%+d"),
                "FanDuel":   st.column_config.NumberColumn(format="%+d"),
                "Best Price": st.column_config.NumberColumn(format="%+d"),
                "Edge":      st.column_config.NumberColumn("Edge (pp)", format="%+.1f"),
            },
        )
    else:
        st.info("No moneyline data available.")


# ── Game Total tab ────────────────────────────────
with tab_gt:
    gt_rows = []
    for g in games:
        if not g.get("gt"):
            continue
        gt = g["gt"]
        ko = _kickoff(g["commence_time"])
        for side, nv, price, book, edge in (
            ("Over",  gt["over_nv"],  gt["best_over_price"],  gt["best_over_book"],  gt["over_edge"]),
            ("Under", gt["under_nv"], gt["best_under_price"], gt["best_under_book"], gt["under_edge"]),
        ):
            fan_bk = (gt.get("books") or {}).get("fanatics", {})
            fd_bk  = (gt.get("books") or {}).get("fanduel",  {})
            fan_p  = fan_bk.get("over_price" if side == "Over" else "under_price")
            fd_p   = fd_bk.get("over_price"  if side == "Over" else "under_price")
            fan_ln = fan_bk.get("line")
            fd_ln  = fd_bk.get("line")
            gap    = f"Fan {fan_ln} / FD {fd_ln}" if fan_ln and fd_ln and fan_ln != fd_ln else ""
            gt_rows.append({
                "":          _edge_icon(edge),
                "Game":      g["game"],
                "Time":      ko,
                "Line":      gt["line"],
                "Side":      side,
                "True %":    nv,
                "Fanatics":  fan_p,
                "FanDuel":   fd_p,
                "Book Gap":  gap,
                "Best Price": price,
                "@ Book":    book or "?",
                "Edge":      edge,
            })
    if gt_rows:
        gt_rows.sort(key=lambda r: -(r["Edge"] or -99))
        df_gt = pd.DataFrame(gt_rows)
        st.dataframe(
            df_gt, use_container_width=True, hide_index=True,
            column_config={
                "Line":      st.column_config.NumberColumn(format="%.1f"),
                "True %":    st.column_config.ProgressColumn("True %", min_value=0, max_value=100, format="%.1f%%"),
                "Fanatics":  st.column_config.NumberColumn(format="%+d"),
                "FanDuel":   st.column_config.NumberColumn(format="%+d"),
                "Best Price": st.column_config.NumberColumn(format="%+d"),
                "Edge":      st.column_config.NumberColumn("Edge (pp)", format="%+.1f"),
            },
        )
    else:
        st.info("No game total data available.")


# ── Team Totals tab ───────────────────────────────
with tab_tt:
    tt_rows = []
    for g in games:
        ko = _kickoff(g["commence_time"])
        for side_key, team_name, role in (
            ("away_tt", g["away_team"], "Away"),
            ("home_tt", g["home_team"], "Home"),
        ):
            tt = g.get(side_key)
            if not tt:
                continue
            for s, nv, price, book, edge in (
                ("Over",  tt["over_nv"],  tt["best_over_price"],  tt["best_over_book"],  tt["over_edge"]),
                ("Under", tt["under_nv"], tt["best_under_price"], tt["best_under_book"], tt["under_edge"]),
            ):
                fan_bk = (tt.get("books") or {}).get("fanatics", {})
                fd_bk  = (tt.get("books") or {}).get("fanduel",  {})
                fan_p  = fan_bk.get("over_price" if s == "Over" else "under_price")
                fd_p   = fd_bk.get("over_price"  if s == "Over" else "under_price")
                fan_ln = fan_bk.get("line")
                fd_ln  = fd_bk.get("line")
                gap    = f"Fan {fan_ln} / FD {fd_ln}" if fan_ln and fd_ln and fan_ln != fd_ln else ""
                tt_rows.append({
                    "":          _edge_icon(edge),
                    "Team":      team_name,
                    "H/A":       role,
                    "Game":      g["game"],
                    "Time":      ko,
                    "Line":      tt["line"],
                    "Side":      s,
                    "True %":    nv,
                    "Fanatics":  fan_p,
                    "FanDuel":   fd_p,
                    "Book Gap":  gap,
                    "Best Price": price,
                    "@ Book":    book or "?",
                    "Edge":      edge,
                })
    if tt_rows:
        tt_rows.sort(key=lambda r: -(r["Edge"] or -99))
        df_tt = pd.DataFrame(tt_rows)
        st.dataframe(
            df_tt, use_container_width=True, hide_index=True,
            column_config={
                "Line":      st.column_config.NumberColumn(format="%.1f"),
                "True %":    st.column_config.ProgressColumn("True %", min_value=0, max_value=100, format="%.1f%%"),
                "Fanatics":  st.column_config.NumberColumn(format="%+d"),
                "FanDuel":   st.column_config.NumberColumn(format="%+d"),
                "Best Price": st.column_config.NumberColumn(format="%+d"),
                "Edge":      st.column_config.NumberColumn("Edge (pp)", format="%+.1f"),
            },
        )
    else:
        st.info("No team total data available.")

st.divider()
with st.expander("🔍 Scan info"):
    st.write(dbg)
