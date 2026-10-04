"""
🏒💰 NHL Polymarket Sharp Money tracker.

Mirrors 8_Sharp_Money.py (MLB) exactly, swapping in the NHL Polymarket
scanner. Checks for NHL game markets on Polymarket and measures bid-side
depth imbalance — same logic, same scoring, same cross-venue arb table.
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

from scripts.nhl_polymarket_sharp import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="NHL Sharp Money", page_icon="🏒", layout="wide")
st.title("🏒💰 NHL Polymarket Sharp Money")
st.caption(
    "For each open NHL game market on Polymarket, we measure depth on each "
    "side of the inside spread (within ±5¢ of mid). When 70%+ of resting "
    "bids sit on one side, that's where the smart money wants to bet at "
    "better prices than the current mid — a sharp positioning signal.  \n"
    "**Free Polymarket data — no Odds API quota used.**"
)


# ---------- Cached scan ----------

@st.cache_data(ttl=900, show_spinner="Pulling Polymarket NHL order books (~30s)...")
def cached_scan(min_volume, min_liquidity, top_n):
    return scan(min_volume=min_volume, min_liquidity=min_liquidity, top_n=top_n)


# ---------- Controls ----------

ctrl_cols = st.columns([1.5, 1, 1, 1, 1, 1])
with ctrl_cols[0]:
    refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True)
with ctrl_cols[1]:
    top_n = st.selectbox("Markets to scan", [30, 50, 75, 100], index=1)
with ctrl_cols[2]:
    min_liquidity = st.number_input("Min liquidity $", value=5000, step=1000)
with ctrl_cols[3]:
    min_skew = st.slider("Min skew %", 50, 95, 60, 5)
with ctrl_cols[4]:
    min_depth = st.number_input("Min depth $", value=200, step=100)
with ctrl_cols[5]:
    st.caption("💡 70%+ skew = strong  \n85%+ = extreme  \n$5K+ liq = real money")

if refresh:
    cached_scan.clear()
    st.toast("Cache cleared — pulling fresh NHL data...", icon="🔄")

rows, debug = cached_scan(min_volume=200, min_liquidity=min_liquidity, top_n=top_n)

st.caption(
    f"Scanned **{debug['total_events']}** NHL events → "
    f"**{debug['daily_markets']}** daily markets → "
    f"**{debug['candidates']}** with volume → "
    f"**{debug['with_book']}** with live order books."
)


# ---------- Filter ----------

filtered = [
    r for r in rows
    if r["skew_strength"] >= min_skew
    and (r["yes_bid_depth"] + r["no_bid_depth"]) >= min_depth
    and (r.get("liquidity") or 0) >= min_liquidity
]


# ---------- Helpers (mirrors MLB Sharp Money exactly) ----------

def _normalize_team(s):
    if not s: return ""
    s2 = s.lower().replace(",","").replace(".","").replace("'","").strip()
    parts = s2.split()
    return parts[-1] if parts else ""


def _depth_ratio(sharp_d, other_d):
    if not other_d or other_d <= 0:
        return 99.0 if (sharp_d or 0) > 0 else 0.0
    return min(99.0, sharp_d / other_d)


def _sharp_score(row, n_confluence, n_appearances, ratio):
    score = 0
    if n_confluence >= 2:   score += 30
    elif n_confluence == 1: score += 18
    if n_appearances >= 2:   score += 20
    elif n_appearances == 1: score += 4
    if ratio >= 10:          score += 20
    elif ratio >= 5:         score += 16
    elif ratio >= 2:         score += 10
    skew = row.get("skew_strength") or 0
    if 80 <= skew < 90:      score += 15
    elif 90 <= skew:         score += 10
    elif 70 <= skew < 80:    score += 5
    edge = row.get("edge_pp")
    if edge is not None:
        if 5 <= edge < 8 or edge >= 20:  score += 15
        elif 12 <= edge < 20:            score += 10
        elif 3 <= edge < 5:              score += 4
        elif 8 <= edge < 12:             score += 4
    whale = row.get("sharp_whale_share") or 0
    n_bids = row.get("sharp_n_bids") or 0
    if whale >= 0.80:          score -= 25
    elif whale >= 0.60:        score -= 15
    elif whale >= 0.45:        score -= 8
    if n_bids > 0 and n_bids <= 2: score -= 10
    return max(0, min(100, score))


def _whale_label(row):
    whale = row.get("sharp_whale_share")
    n_bids = row.get("sharp_n_bids") or 0
    if whale is None: return "—"
    if whale >= 0.80: return f"🐋 lone whale ({whale*100:.0f}%)"
    if whale >= 0.60: return f"🐟 concentrated ({whale*100:.0f}%)"
    if whale >= 0.45: return f"🟡 mixed ({whale*100:.0f}%)"
    if n_bids >= 5:   return f"🟢 distributed ({n_bids} orders)"
    return f"⚪ small book ({n_bids} orders)"


def _sharp_tier(score):
    if score >= 75: return "🟢🟢🟢 ELITE"
    if score >= 55: return "🟢🟢 STRONG"
    if score >= 35: return "🟢 DECENT"
    return "🟡 WEAK"


def _other_side_label(r):
    mt = r.get("match_type")
    sharp_side = r.get("skew_side")
    if mt == "h2h":
        return r.get("home_team", "?") if sharp_side == "YES" else r.get("away_team", "?")
    if mt == "totals":
        pt = r.get("point")
        away = (r.get("away_team","") or "").split()[-1] or ""
        home = (r.get("home_team","") or "").split()[-1] or ""
        match_str = f" ({away} @ {home})" if away and home else ""
        base = (f"UNDER {pt}" if sharp_side == "YES" else f"OVER {pt}") if pt is not None else ("UNDER" if sharp_side == "YES" else "OVER")
        return f"{base}{match_str}"
    if mt == "spreads":
        team = r.get("team")
        pt = r.get("point")
        opp = r.get("home_team") if (r.get("away_team") or "").lower() in (team or "").lower() else r.get("away_team")
        if sharp_side == "YES":
            opp_pt = -(pt or 0)
            return f"{opp or 'Opponent'} {'+' if opp_pt>0 else ''}{opp_pt}"
        return f"{team} {'+' if (pt or 0)>0 else ''}{pt}"
    return "?"


st.warning(
    "⚠️ **Sharp money ≠ good bet.** Check the Verdict column in the "
    "cross-venue table below. Only ✅ BET verdicts (≥5pp edge) are real arbs.",
    icon="⚠️",
)
st.markdown(f"### 🎯 {len(filtered)} markets passing filter")
if not filtered:
    st.warning(
        f"No NHL markets with ≥{min_skew}% skew and ≥${min_depth} depth right now.  \n"
        "Lower the threshold, refresh, or wait — Polymarket may have fewer NHL markets "
        "than MLB during off-peak hours."
    )
    st.stop()


# ---------- Tabs ----------

tab_all, tab_yes, tab_no = st.tabs([
    f"📋 All ({len(filtered)})",
    f"🟢 YES heavy ({sum(1 for r in filtered if r['skew_side']=='YES')})",
    f"🔴 NO heavy ({sum(1 for r in filtered if r['skew_side']=='NO')})",
])


def render_table(rows_subset, sort_by="score", show_details=False):
    if not rows_subset:
        st.info("No markets in this bucket.")
        return
    table = []
    for r in rows_subset:
        marker = "💰💰" if r["skew_strength"] >= 85 else ("💰" if r["skew_strength"] >= 70 else "")
        sharp_depth = r["yes_bid_depth"] if r["skew_side"] == "YES" else r["no_bid_depth"]
        other_depth = r["no_bid_depth"]  if r["skew_side"] == "YES" else r["yes_bid_depth"]
        ratio = _depth_ratio(sharp_depth, other_depth)
        score = _sharp_score(r, 0, 0, ratio)
        tier  = _sharp_tier(score)
        row = {
            "Sharp pick":  f"{marker} {r.get('sharp_pick', '')}",
            "Score":       score,
            "Tier":        tier,
            "Skew %":      r["skew_strength"],
            "Depth ratio": ratio,
            "Game":        r["event"][:36],
        }
        if show_details:
            row.update({
                "Mkt":             r["category"],
                "Whale?":          _whale_label(r),
                "$ on sharp pick": sharp_depth,
                "Other side":      _other_side_label(r),
                "$ on other side": other_depth,
                "Mid (YES)":       r["mid"],
                "Spread":          r["spread"],
                "Volume $":        r["volume"],
            })
        table.append(row)
    df_t = pd.DataFrame(table)
    if sort_by == "depth":
        order = sorted(range(len(rows_subset)), key=lambda i: rows_subset[i]["yes_bid_depth"] + rows_subset[i]["no_bid_depth"], reverse=True)
        df_t = df_t.iloc[order]
    elif sort_by == "skew":
        df_t = df_t.sort_values("Skew %", ascending=False)
    elif sort_by == "score":
        df_t = df_t.sort_values("Score", ascending=False)
    elif sort_by == "ratio":
        df_t = df_t.sort_values("Depth ratio", ascending=False)
    full_cfg = {
        "Score":             st.column_config.NumberColumn(format="%d"),
        "Depth ratio":       st.column_config.NumberColumn(format="%.1fx"),
        "Skew %":            st.column_config.NumberColumn(format="%.0f%%"),
        "Mid (YES)":         st.column_config.NumberColumn(format="$%.3f"),
        "Spread":            st.column_config.NumberColumn(format="$%.3f"),
        "$ on sharp pick":   st.column_config.NumberColumn(format="$%,d"),
        "$ on other side":   st.column_config.NumberColumn(format="$%,d"),
        "Volume $":          st.column_config.NumberColumn(format="$%,.0f"),
    }
    cfg = {k: v for k, v in full_cfg.items() if k in df_t.columns}
    st.dataframe(df_t, use_container_width=True, hide_index=True, column_config=cfg)


with tab_all:
    cl, cr = st.columns([3, 1])
    with cl:
        sort_choice = st.radio("Sort by", ["score", "depth", "skew", "ratio"],
                               horizontal=True, key="sort_all", index=0)
    with cr:
        show_details = st.toggle("🔧 Show details", value=False, key="details_all")
    render_table(filtered, sort_by=sort_choice, show_details=show_details)

with tab_yes:
    show_d_y = st.toggle("🔧 Show details", value=False, key="details_yes")
    render_table([r for r in filtered if r["skew_side"] == "YES"], show_details=show_d_y)

with tab_no:
    show_d_n = st.toggle("🔧 Show details", value=False, key="details_no")
    render_table([r for r in filtered if r["skew_side"] == "NO"], show_details=show_d_n)


# ---------- Cross-venue arb ----------

st.markdown("---")
st.markdown("### 🎯 Cross-venue plays — Polymarket signal vs NHL sportsbook")
st.caption(
    "For each strong signal, we look up the matching NHL sportsbook line. "
    "**Edge pp** = Polymarket sharp-implied % minus sportsbook implied %. "
    "Positive + strong skew = actionable mispricing."
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
    st.warning("No `THE_ODDS_API_KEY` — can't cross-reference sportsbook prices.")
else:
    import json as _json
    import ssl as _ssl2
    import urllib.request as _req

    _SSL2 = _ssl2._create_unverified_context()
    SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada,pinnacle"

    @st.cache_data(ttl=300, show_spinner="Fetching NHL odds...")
    def _fetch_nhl_lines(api_key):
        url = (f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds"
               f"?apiKey={api_key}&regions=us&markets=h2h,spreads,totals"
               f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
        try:
            return _json.loads(_req.urlopen(url, timeout=20, context=_SSL2).read())
        except Exception:
            return []

    def _amer_to_imp(am):
        if am > 0: return 100.0 / (am + 100.0)
        return abs(am) / (abs(am) + 100.0)

    def _norm(s):
        return (s or "").lower().strip().split()[-1]

    strong = [r for r in filtered if r["skew_strength"] >= 65]
    if not strong:
        st.info("No strong signals (≥65% skew) to cross-reference.")
    else:
        nhl_lines = _fetch_nhl_lines(ODDS_KEY)

        matched = []
        for r in strong:
            a_norm = _norm(r.get("away_team") or "")
            h_norm = _norm(r.get("home_team") or "")
            for ev in nhl_lines:
                if not (_norm(ev.get("away_team","")) == a_norm or _norm(ev.get("home_team","")) == h_norm
                        or _norm(ev.get("away_team","")) == h_norm or _norm(ev.get("home_team","")) == a_norm):
                    continue
                mt = r.get("match_type")
                best_price = None
                best_book  = None
                for bm in ev.get("bookmakers", []):
                    for mkt in bm.get("markets", []):
                        if mt == "h2h" and mkt["key"] == "h2h":
                            for o in mkt.get("outcomes", []):
                                team = o.get("name","")
                                if (r["skew_side"] == "YES" and _norm(team) == a_norm) or \
                                   (r["skew_side"] == "NO"  and _norm(team) == h_norm):
                                    p = o.get("price")
                                    if p is not None and (best_price is None or p > best_price):
                                        best_price = p; best_book = bm.get("key","")
                        elif mt == "totals" and mkt["key"] == "totals":
                            pt = r.get("point")
                            side = "over" if r["skew_side"] == "YES" else "under"
                            for o in mkt.get("outcomes", []):
                                if (o.get("name","").lower() == side
                                        and (pt is None or abs((o.get("point") or 0) - pt) <= 0.5)):
                                    p = o.get("price")
                                    if p is not None and (best_price is None or p > best_price):
                                        best_price = p; best_book = bm.get("key","")
                if best_price is not None:
                    sb_impl = _amer_to_imp(best_price) * 100
                    pm_impl = (r["mid"] if r["skew_side"] == "YES" else 1 - r["mid"]) * 100
                    edge = round(pm_impl - sb_impl, 1)
                    matched.append({**r, "sb_best_price": best_price,
                                    "sb_book": best_book, "sb_implied_pct": sb_impl,
                                    "edge_pp": edge})
                    break

        if matched:
            tbl = []
            for r in sorted(matched, key=lambda x: -(x.get("edge_pp") or -999)):
                edge = r.get("edge_pp")
                if edge is None:           verdict = "❓ no SB match"
                elif edge >= 5:            verdict = "✅ BET — real edge"
                elif edge >= 3:            verdict = "🟡 marginal edge"
                elif edge >= 0:            verdict = "⚠️ confirms SB — no arb"
                else:                      verdict = "🚫 SB favors other side"
                sharp_d = r["yes_bid_depth"] if r["skew_side"]=="YES" else r["no_bid_depth"]
                tbl.append({
                    "Verdict":    verdict,
                    "Game":       r.get("event","")[:36],
                    "Mkt":        r.get("category",""),
                    "Sharp pick": r.get("sharp_pick",""),
                    "Skew %":     r["skew_strength"],
                    "SB price":   r.get("sb_best_price"),
                    "SB book":    r.get("sb_book",""),
                    "PM %":       round((r["mid"] if r["skew_side"]=="YES" else 1-r["mid"])*100,1),
                    "SB %":       round(r.get("sb_implied_pct",0),1),
                    "Edge pp":    edge,
                })
            st.dataframe(
                pd.DataFrame(tbl), use_container_width=True, hide_index=True,
                column_config={
                    "Skew %":  st.column_config.NumberColumn(format="%.0f%%"),
                    "SB price":st.column_config.NumberColumn(format="%+d"),
                    "PM %":    st.column_config.NumberColumn(format="%.1f%%"),
                    "SB %":    st.column_config.NumberColumn(format="%.1f%%"),
                    "Edge pp": st.column_config.NumberColumn(format="%+.1f"),
                },
            )
            st.caption(
                "✅ BET = ≥5pp edge · 🟡 marginal = 3-5pp · "
                "⚠️ confirms SB = no arb · 🚫 SB favors other side — skip  \n"
                "_Always verify live sportsbook price before betting._"
            )
        else:
            st.info("No strong NHL signals matched sportsbook lines right now.")


st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 15 min  ·  Polymarket: Gamma + CLOB (free)"
)
