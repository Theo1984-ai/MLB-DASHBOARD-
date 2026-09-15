"""
🏈 NFL Polymarket Sharp Money tracker.

For every open NFL game-level market on Polymarket, shows where the
limit-order book has heaviest bid-side depth within the current week.
Strong skew on one side = real money queueing to bet that side at
better prices. Free data — no Odds API quota used.
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

from scripts.nfl_polymarket_sharp import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="NFL Sharp Money", page_icon="🏈", layout="wide")
st.title("🏈 NFL Polymarket Sharp Money")
st.caption(
    "For each open NFL game market on Polymarket, we measure depth on each "
    "side of the inside spread (within ±5¢ of mid). When 70%+ of resting "
    "bids sit on one side, that's where smart money wants to bet at "
    "better prices than the current mid — a sharp positioning signal.  \n"
    "**Current NFL week only · Free data — no Odds API quota used.**"
)


# ---------- Cached scan ----------

@st.cache_data(ttl=900, show_spinner="Pulling Polymarket NFL order books (~30s)...")
def cached_scan(min_volume, min_liquidity, top_n):
    return scan(min_volume=min_volume, min_liquidity=min_liquidity, top_n=top_n)


# ---------- Controls ----------

ctrl_cols = st.columns([1.5, 1, 1, 1, 1, 1])
with ctrl_cols[0]:
    refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True,
                        help="Clears the 15-min cache and pulls fresh order books")
with ctrl_cols[1]:
    top_n = st.selectbox("Markets to scan", [20, 30, 50, 75], index=1,
                         help="Top N NFL markets by volume to fetch order books for")
with ctrl_cols[2]:
    min_liquidity = st.number_input("Min liquidity $", value=5000, step=1000,
                                    help="Polymarket listed liquidity floor — "
                                         "$5K+ guarantees real money is behind the market")
with ctrl_cols[3]:
    min_skew = st.slider("Min skew %", 50, 95, 60, 5,
                         help="Only show markets with this much imbalance")
with ctrl_cols[4]:
    min_depth = st.number_input("Min depth $", value=200, step=100,
                                help="Bid-side depth within 5¢ of mid (filter out tiny books)")
with ctrl_cols[5]:
    st.caption(
        "💡 70%+ skew = strong  \n"
        "85%+ skew = extreme  \n"
        "NFL markets are smaller than MLB"
    )

if refresh:
    cached_scan.clear()
    st.toast("Cache cleared — pulling fresh NFL data...", icon="🔄")

rows, debug = cached_scan(min_volume=500, min_liquidity=min_liquidity, top_n=top_n)

st.caption(
    f"Scanned **{debug['total_events']}** NFL events → "
    f"**{debug['weekly_markets']}** weekly game markets → "
    f"**{debug['candidates']}** with volume/liquidity → "
    f"**{debug['with_book']}** with live order books.  "
    f"(Skipped: {debug['filtered_future_games']} off-week, "
    f"{debug['filtered_started_games']} already started)"
)


# ---------- Filter ----------

filtered = [
    r for r in rows
    if r["skew_strength"] >= min_skew
    and (r["yes_bid_depth"] + r["no_bid_depth"]) >= min_depth
    and (r.get("liquidity") or 0) >= min_liquidity
]

st.warning(
    "⚠️ **Big sharp money ≠ good bet.** Polymarket NFL markets are thinner "
    "than MLB. Always cross-check the sportsbook price before acting. "
    "Focus on markets with $5K+ liquidity and 70%+ skew.",
    icon="⚠️",
)
st.markdown(f"### 🎯 {len(filtered)} markets passing filter")
if not filtered:
    st.warning(
        f"No markets with ≥{min_skew}% skew and ≥${min_depth} depth right now. "
        "Lower the threshold, or check back closer to kickoff — NFL Polymarket "
        "books fill up in the 24-48 hours before game time."
    )
    st.stop()


# ---------- Helpers ----------

def _opponent_team(team, event_title):
    if not team or not event_title:
        return None
    for sep in (" vs. ", " vs ", " @ "):
        if sep in event_title:
            a, b = event_title.split(sep, 1)
            a, b = a.strip(), b.strip()
            t = team.strip().lower()
            if t in a.lower() or a.lower() in t:
                return b
            if t in b.lower() or b.lower() in t:
                return a
    return None


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
        if sharp_side == "YES":
            base = f"UNDER {pt}" if pt is not None else "UNDER"
        else:
            base = f"OVER {pt}" if pt is not None else "OVER"
        return f"{base}{match_str}"
    if mt == "spreads":
        team = r.get("team")
        pt = r.get("point")
        opponent = _opponent_team(team, r.get("event", "")) or "Opponent"
        if sharp_side == "YES":
            opp_pt = -(pt or 0)
            sign = "+" if opp_pt > 0 else ""
            return f"{opponent} {sign}{opp_pt}"
        sign = "+" if (pt or 0) > 0 else ""
        return f"{team} {sign}{pt}"
    if mt == "player_td":
        player = r.get("team") or "Player"
        return f"{player} NO TD"
    return "?"


def _depth_ratio(sharp_d, other_d):
    if not other_d or other_d <= 0:
        return 99.0 if (sharp_d or 0) > 0 else 0.0
    return min(99.0, sharp_d / other_d)


def _whale_label(row):
    whale = row.get("sharp_whale_share")
    n_bids = row.get("sharp_n_bids") or 0
    if whale is None:
        return "—"
    if whale >= 0.80:  return f"🐋 lone whale ({whale*100:.0f}%)"
    if whale >= 0.60:  return f"🐟 concentrated ({whale*100:.0f}%)"
    if whale >= 0.45:  return f"🟡 mixed ({whale*100:.0f}%)"
    if n_bids >= 5:    return f"🟢 distributed ({n_bids} orders)"
    return f"⚪ small book ({n_bids} orders)"


def _sharp_score(row, ratio):
    """Simplified score (no confluence/persistence since we don't snapshot NFL yet)."""
    score = 0
    if ratio >= 10:    score += 30
    elif ratio >= 5:   score += 20
    elif ratio >= 2:   score += 12
    skew = row.get("skew_strength") or 0
    if 80 <= skew < 90:    score += 20
    elif skew >= 90:        score += 14
    elif 70 <= skew < 80:  score += 8
    vol = row.get("volume") or 0
    if vol >= 100000:   score += 20
    elif vol >= 50000:  score += 14
    elif vol >= 10000:  score += 8
    elif vol >= 2000:   score += 4
    whale = row.get("sharp_whale_share") or 0
    n_bids = row.get("sharp_n_bids") or 0
    if whale >= 0.80:    score -= 25
    elif whale >= 0.60:  score -= 15
    elif whale >= 0.45:  score -= 8
    if n_bids > 0 and n_bids <= 2:  score -= 10
    return max(0, min(100, score))


def _sharp_tier(score):
    if score >= 75:  return "🟢🟢🟢 ELITE"
    if score >= 55:  return "🟢🟢 STRONG"
    if score >= 35:  return "🟢 DECENT"
    return "🟡 WEAK"


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
        other_depth = r["no_bid_depth"] if r["skew_side"] == "YES" else r["yes_bid_depth"]
        ratio = _depth_ratio(sharp_depth, other_depth)
        score = _sharp_score(r, ratio)
        tier  = _sharp_tier(score)
        game_start_str = ""
        gs = r.get("game_start")
        if gs:
            try:
                from datetime import datetime as _dt
                gdt = _dt.fromisoformat(gs).astimezone(EASTERN)
                game_start_str = gdt.strftime("%a %-I:%M%p").replace("AM","am").replace("PM","pm")
            except Exception:
                pass
        row_d = {
            "Sharp pick":  f"{marker} {r.get('sharp_pick', '')}",
            "Score":       score,
            "Tier":        tier,
            "Mkt":         r["category"],
            "Skew %":      r["skew_strength"],
            "Depth ratio": ratio,
            "Game":        r["event"][:36],
            "Kickoff":     game_start_str,
        }
        if show_details:
            row_d.update({
                "Whale?":          _whale_label(r),
                "$ sharp pick":    sharp_depth,
                "Other side":      _other_side_label(r),
                "$ other side":    other_depth,
                "Mid (YES)":       r["mid"],
                "Spread":          r["spread"],
                "Volume $":        r["volume"],
            })
        table.append(row_d)
    df = pd.DataFrame(table)
    if sort_by == "score":
        df = df.sort_values("Score", ascending=False)
    elif sort_by == "skew":
        df = df.sort_values("Skew %", ascending=False)
    elif sort_by == "depth":
        df = df.iloc[sorted(range(len(rows_subset)),
                            key=lambda i: -(rows_subset[i]["yes_bid_depth"] +
                                            rows_subset[i]["no_bid_depth"]))]
    elif sort_by == "volume":
        df = df.iloc[sorted(range(len(rows_subset)),
                            key=lambda i: -(rows_subset[i].get("volume", 0) or 0))]
    full_cfg = {
        "Score":         st.column_config.NumberColumn(format="%d",
                          help="0-100 composite. Depth ratio 30, Skew 20, Volume 20. "
                               "Whale penalty up to -25. 75+ ELITE, 55+ STRONG, 35+ DECENT."),
        "Skew %":        st.column_config.NumberColumn(format="%.0f%%"),
        "Depth ratio":   st.column_config.NumberColumn(format="%.1fx",
                          help="Sharp-side $ / other-side $. Higher = more lopsided conviction."),
        "Mid (YES)":     st.column_config.NumberColumn(format="$%.3f"),
        "Spread":        st.column_config.NumberColumn(format="$%.3f"),
        "$ sharp pick":  st.column_config.NumberColumn(format="$%,d"),
        "$ other side":  st.column_config.NumberColumn(format="$%,d"),
        "Volume $":      st.column_config.NumberColumn(format="$%,.0f"),
        "Whale?":        st.column_config.TextColumn(
                          help="How concentrated the sharp-side depth is. "
                               "🐋 lone whale (80%+) = fragile signal."),
    }
    cfg = {k: v for k, v in full_cfg.items() if k in df.columns}
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=cfg)


with tab_all:
    col_l, col_r = st.columns([3, 1])
    with col_l:
        sort_choice = st.radio("Sort by", ["score", "depth", "skew", "volume"],
                               horizontal=True, key="sort_all", index=0)
    with col_r:
        show_details = st.toggle("🔧 Show details", value=False, key="details_all")
    render_table(filtered, sort_by=sort_choice, show_details=show_details)

with tab_yes:
    show_y = st.toggle("🔧 Show details", value=False, key="details_yes")
    yes_rows = sorted([r for r in filtered if r["skew_side"] == "YES"],
                      key=lambda r: -r["yes_bid_depth"])
    render_table(yes_rows, show_details=show_y)

with tab_no:
    show_n = st.toggle("🔧 Show details", value=False, key="details_no")
    no_rows = sorted([r for r in filtered if r["skew_side"] == "NO"],
                     key=lambda r: -r["no_bid_depth"])
    render_table(no_rows, show_details=show_n)


# ---------- Top actionable plays (cross-reference sportsbook) ----------

st.markdown("---")
st.markdown("### 🎯 Cross-venue snapshot — sharp signal vs sportsbook line")
st.caption(
    "For each strong signal, compare the Polymarket mid (what sharps imply) "
    "against the DraftKings/FanDuel price for the same market. "
    "**PM % − SB %** = edge. Positive = sportsbook is softer than sharps think."
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
    st.warning("No `THE_ODDS_API_KEY` configured — can't pull sportsbook cross-reference.")
else:
    import json
    import ssl as _ssl_compat
    import urllib.request

    _SSL2 = _ssl_compat._create_unverified_context()

    @st.cache_data(ttl=300, show_spinner="Fetching DraftKings NFL lines...")
    def _fetch_nfl_h2h(api_key):
        url = (f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
               f"?apiKey={api_key}&regions=us&markets=h2h,spreads,totals"
               f"&bookmakers=draftkings,fanduel&oddsFormat=american")
        try:
            return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL2).read())
        except Exception:
            return []

    sb_games = _fetch_nfl_h2h(ODDS_KEY)

    def _amer_to_imp(am):
        if am > 0:
            return 100.0 / (am + 100.0)
        return abs(am) / (abs(am) + 100.0)

    def _find_sb_price(sb_games, away_short, home_short, market_type, side_yes):
        """Return (best_price, implied_pct, book) or (None, None, None)."""
        for g in sb_games:
            a = (g.get("away_team") or "").split()[-1].lower()
            h = (g.get("home_team") or "").split()[-1].lower()
            if a != away_short.lower() and h != home_short.lower():
                continue
            for bk in g.get("bookmakers", []):
                for m in bk.get("markets", []):
                    mk = m["key"]
                    if market_type == "h2h" and mk == "h2h":
                        target = g["away_team"] if side_yes else g["home_team"]
                        for o in m.get("outcomes", []):
                            if (o.get("name") or "").strip() == target:
                                p = int(o["price"])
                                return p, round(_amer_to_imp(p)*100, 1), bk["key"]
                    elif market_type == "totals" and mk == "totals":
                        target_side = "Over" if side_yes else "Under"
                        for o in m.get("outcomes", []):
                            if (o.get("name") or "").strip() == target_side:
                                p = int(o["price"])
                                return p, round(_amer_to_imp(p)*100, 1), bk["key"]
        return None, None, None

    strong_signals = [r for r in filtered if r["skew_strength"] >= 65]
    cross_rows = []
    for r in strong_signals[:20]:
        mt = r.get("match_type")
        if mt not in ("h2h", "totals"):
            continue
        away = r.get("away_team") or ""
        home = r.get("home_team") or ""
        if not away or not home:
            continue
        away_s = away.split()[-1]
        home_s = home.split()[-1]
        side_yes = r["skew_side"] == "YES"
        sb_p, sb_imp, sb_bk = _find_sb_price(sb_games, away_s, home_s, mt, side_yes)
        pm_imp = round((r["mid"] if side_yes else (1 - r["mid"])) * 100, 1)
        edge = round(pm_imp - sb_imp, 1) if sb_imp is not None else None
        if edge is None:
            verdict = "❓ no SB match"
        elif edge >= 5:
            verdict = "✅ BET — real edge"
        elif edge >= 3:
            verdict = "🟡 marginal edge"
        elif edge >= 0:
            verdict = "⚠️ confirms SB"
        else:
            verdict = "🚫 SB favors other side"
        cross_rows.append({
            "Verdict":     verdict,
            "Sharp pick":  r.get("sharp_pick",""),
            "Game":        r["event"][:36],
            "Mkt":         r["category"],
            "Skew %":      r["skew_strength"],
            "PM %":        pm_imp,
            "SB %":        sb_imp,
            "Edge pp":     edge,
            "SB price":    sb_p,
            "SB book":     sb_bk or "—",
        })

    if cross_rows:
        cdf = pd.DataFrame(cross_rows)
        cdf_sorted = cdf.copy()
        cdf_sorted["_sort"] = pd.to_numeric(cdf_sorted["Edge pp"], errors="coerce").fillna(-999)
        cdf_sorted = cdf_sorted.sort_values("_sort", ascending=False).drop(columns="_sort")
        st.dataframe(
            cdf_sorted, use_container_width=True, hide_index=True,
            column_config={
                "Skew %":  st.column_config.NumberColumn(format="%.0f%%"),
                "PM %":    st.column_config.NumberColumn(format="%.1f%%",
                            help="Polymarket-implied probability (sharp side)"),
                "SB %":    st.column_config.NumberColumn(format="%.1f%%",
                            help="Sportsbook-implied probability"),
                "Edge pp": st.column_config.NumberColumn(format="%+.1f",
                            help="PM % − SB %. 5pp+ = actionable, 3-5pp = marginal"),
                "SB price": st.column_config.NumberColumn(format="%+d"),
            },
        )
        st.caption(
            "✅ BET = 5pp+ edge  ·  🟡 marginal = 3-5pp  ·  "
            "⚠️ confirms SB = sharp money agrees but no edge  ·  "
            "🚫 SB favors other side = skip.  "
            "_Always verify live price before betting._"
        )
    else:
        st.info("No h2h/totals signals strong enough (≥65% skew) to cross-reference right now.")


st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 15 min  ·  "
    f"Polymarket: Gamma + CLOB (free, NFL week-only filter)  ·  "
    f"SB cross-ref: Odds API (small quota, h2h + spreads + totals)"
)
