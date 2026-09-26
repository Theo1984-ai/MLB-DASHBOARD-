"""
💰 Sharp Money — NFL (Polymarket order-book depth) + CFB (Odds API sportsbook comparison).
"""
import json
import os
import ssl as _ssl_compat
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.nfl_polymarket_sharp import scan as nfl_scan  # noqa: E402
from scripts.cfb_sharp_scanner import scan as cfb_scan      # noqa: E402

EASTERN = ZoneInfo("America/New_York")
_SSL = _ssl_compat._create_unverified_context()


def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


ODDS_KEY = _resolve_secret("THE_ODDS_API_KEY")

st.set_page_config(page_title="Sharp Money", page_icon="💰", layout="wide")
st.title("💰 Sharp Money")


# ---------- Cached scan functions (module-level for cache persistence) ----------

@st.cache_data(ttl=900, show_spinner="Pulling Polymarket NFL order books (~30s)...")
def _nfl_cached_scan(min_volume, min_liquidity, top_n):
    return nfl_scan(min_volume=min_volume, min_liquidity=min_liquidity, top_n=top_n)


@st.cache_data(ttl=300, show_spinner="Scanning CFB lines across 6 books...")
def _cfb_cached_scan(api_key):
    return cfb_scan(api_key)


@st.cache_data(ttl=300, show_spinner="Fetching DraftKings NFL lines...")
def _fetch_nfl_h2h(api_key):
    url = (f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
           f"?apiKey={api_key}&regions=us&markets=h2h,spreads,totals"
           f"&bookmakers=draftkings,fanduel&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════
#  NFL tab — Polymarket order-book depth
# ═══════════════════════════════════════════════════════════════════

def _render_nfl_sharp():
    st.caption(
        "For each open NFL game market on Polymarket, we measure depth on each "
        "side of the inside spread (within ±5¢ of mid). When 70%+ of resting "
        "bids sit on one side, that's where smart money wants to bet at "
        "better prices than the current mid — a sharp positioning signal.  \n"
        "**Current NFL week only · Free data — no Odds API quota used.**"
    )

    ctrl_cols = st.columns([1.5, 1, 1, 1, 1, 1])
    with ctrl_cols[0]:
        refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True,
                            key="nfl_refresh",
                            help="Clears the 15-min cache and pulls fresh order books")
    with ctrl_cols[1]:
        top_n = st.selectbox("Markets to scan", [20, 30, 50, 75], index=1, key="nfl_top_n",
                             help="Top N NFL markets by volume to fetch order books for")
    with ctrl_cols[2]:
        min_liquidity = st.number_input("Min liquidity $", value=5000, step=1000,
                                        key="nfl_min_liq",
                                        help="Polymarket listed liquidity floor")
    with ctrl_cols[3]:
        min_skew = st.slider("Min skew %", 50, 95, 60, 5, key="nfl_min_skew",
                             help="Only show markets with this much imbalance")
    with ctrl_cols[4]:
        min_depth = st.number_input("Min depth $", value=200, step=100, key="nfl_min_depth",
                                    help="Bid-side depth within 5¢ of mid")
    with ctrl_cols[5]:
        st.caption("💡 70%+ skew = strong  \n85%+ skew = extreme  \nNFL markets are smaller than MLB")

    if refresh:
        _nfl_cached_scan.clear()
        st.toast("Cache cleared — pulling fresh NFL data...", icon="🔄")

    rows, debug = _nfl_cached_scan(min_volume=500, min_liquidity=min_liquidity, top_n=top_n)

    st.caption(
        f"Scanned **{debug['total_events']}** NFL events → "
        f"**{debug['weekly_markets']}** weekly game markets → "
        f"**{debug['candidates']}** with volume/liquidity → "
        f"**{debug['with_book']}** with live order books.  "
        f"(Skipped: {debug['filtered_future_games']} off-week, "
        f"{debug['filtered_started_games']} already started)"
    )

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
        return

    # ---- helpers ----

    def _opponent_team(team, event_title):
        if not team or not event_title:
            return None
        for sep in (" vs. ", " vs ", " @ "):
            if sep in event_title:
                a, b = event_title.split(sep, 1)
                a, b = a.strip(), b.strip()
                t = team.strip().lower()
                if t in a.lower() or a.lower() in t: return b
                if t in b.lower() or b.lower() in t: return a
        return None

    def _other_side_label(r):
        mt = r.get("match_type"); sharp_side = r.get("skew_side")
        if mt == "h2h":
            return r.get("home_team", "?") if sharp_side == "YES" else r.get("away_team", "?")
        if mt == "totals":
            pt = r.get("point")
            away = (r.get("away_team","") or "").split()[-1] or ""
            home = (r.get("home_team","") or "").split()[-1] or ""
            match_str = f" ({away} @ {home})" if away and home else ""
            base = (f"UNDER {pt}" if pt is not None else "UNDER") if sharp_side == "YES" else (f"OVER {pt}" if pt is not None else "OVER")
            return f"{base}{match_str}"
        if mt == "spreads":
            team = r.get("team"); pt = r.get("point")
            opponent = _opponent_team(team, r.get("event","")) or "Opponent"
            if sharp_side == "YES":
                opp_pt = -(pt or 0); sign = "+" if opp_pt > 0 else ""
                return f"{opponent} {sign}{opp_pt}"
            sign = "+" if (pt or 0) > 0 else ""
            return f"{team} {sign}{pt}"
        if mt == "player_td":
            return f"{r.get('team') or 'Player'} NO TD"
        return "?"

    def _depth_ratio(sharp_d, other_d):
        if not other_d or other_d <= 0:
            return 99.0 if (sharp_d or 0) > 0 else 0.0
        return min(99.0, sharp_d / other_d)

    def _whale_label(row):
        whale = row.get("sharp_whale_share"); n_bids = row.get("sharp_n_bids") or 0
        if whale is None: return "—"
        if whale >= 0.80: return f"🐋 lone whale ({whale*100:.0f}%)"
        if whale >= 0.60: return f"🐟 concentrated ({whale*100:.0f}%)"
        if whale >= 0.45: return f"🟡 mixed ({whale*100:.0f}%)"
        if n_bids >= 5:   return f"🟢 distributed ({n_bids} orders)"
        return f"⚪ small book ({n_bids} orders)"

    def _sharp_score(row, ratio):
        score = 0
        if ratio >= 10:   score += 30
        elif ratio >= 5:  score += 20
        elif ratio >= 2:  score += 12
        skew = row.get("skew_strength") or 0
        if 80 <= skew < 90:   score += 20
        elif skew >= 90:       score += 14
        elif 70 <= skew < 80: score += 8
        vol = row.get("volume") or 0
        if vol >= 100000: score += 20
        elif vol >= 50000: score += 14
        elif vol >= 10000: score += 8
        elif vol >= 2000:  score += 4
        whale = row.get("sharp_whale_share") or 0
        n_bids = row.get("sharp_n_bids") or 0
        if whale >= 0.80:   score -= 25
        elif whale >= 0.60: score -= 15
        elif whale >= 0.45: score -= 8
        if 0 < n_bids <= 2: score -= 10
        return max(0, min(100, score))

    def _sharp_tier(score):
        if score >= 75: return "🟢🟢🟢 ELITE"
        if score >= 55: return "🟢🟢 STRONG"
        if score >= 35: return "🟢 DECENT"
        return "🟡 WEAK"

    tab_all, tab_yes, tab_no = st.tabs([
        f"📋 All ({len(filtered)})",
        f"🟢 YES heavy ({sum(1 for r in filtered if r['skew_side']=='YES')})",
        f"🔴 NO heavy ({sum(1 for r in filtered if r['skew_side']=='NO')})",
    ])

    def _render_nfl_table(rows_subset, sort_by="score", show_details=False):
        if not rows_subset:
            st.info("No markets in this bucket.")
            return
        table = []
        for r in rows_subset:
            marker = "💰💰" if r["skew_strength"] >= 85 else ("💰" if r["skew_strength"] >= 70 else "")
            sharp_d = r["yes_bid_depth"] if r["skew_side"] == "YES" else r["no_bid_depth"]
            other_d = r["no_bid_depth"] if r["skew_side"] == "YES" else r["yes_bid_depth"]
            ratio = _depth_ratio(sharp_d, other_d)
            score = _sharp_score(r, ratio)
            game_start_str = ""
            gs = r.get("game_start")
            if gs:
                try:
                    gdt = datetime.fromisoformat(gs).astimezone(EASTERN)
                    fmt = "%a %#I:%M%p" if sys.platform == "win32" else "%a %-I:%M%p"
                    game_start_str = gdt.strftime(fmt).replace("AM","am").replace("PM","pm")
                except Exception:
                    pass
            row_d = {
                "Sharp pick": f"{marker} {r.get('sharp_pick', '')}",
                "Score": score, "Tier": _sharp_tier(score),
                "Mkt": r["category"], "Skew %": r["skew_strength"],
                "Depth ratio": ratio, "Game": r["event"][:36], "Kickoff": game_start_str,
            }
            if show_details:
                row_d.update({
                    "Whale?": _whale_label(r), "$ sharp pick": sharp_d,
                    "Other side": _other_side_label(r), "$ other side": other_d,
                    "Mid (YES)": r["mid"], "Spread": r["spread"], "Volume $": r["volume"],
                })
            table.append(row_d)
        df = pd.DataFrame(table)
        if sort_by == "score":    df = df.sort_values("Score", ascending=False)
        elif sort_by == "skew":   df = df.sort_values("Skew %", ascending=False)
        elif sort_by == "depth":
            df = df.iloc[sorted(range(len(rows_subset)),
                                key=lambda i: -(rows_subset[i]["yes_bid_depth"] +
                                                rows_subset[i]["no_bid_depth"]))]
        elif sort_by == "volume":
            df = df.iloc[sorted(range(len(rows_subset)),
                                key=lambda i: -(rows_subset[i].get("volume", 0) or 0))]
        full_cfg = {
            "Score":        st.column_config.NumberColumn(format="%d"),
            "Skew %":       st.column_config.NumberColumn(format="%.0f%%"),
            "Depth ratio":  st.column_config.NumberColumn(format="%.1fx"),
            "Mid (YES)":    st.column_config.NumberColumn(format="$%.3f"),
            "Spread":       st.column_config.NumberColumn(format="$%.3f"),
            "$ sharp pick": st.column_config.NumberColumn(format="$%,d"),
            "$ other side": st.column_config.NumberColumn(format="$%,d"),
            "Volume $":     st.column_config.NumberColumn(format="$%,.0f"),
        }
        cfg = {k: v for k, v in full_cfg.items() if k in df.columns}
        st.dataframe(df, use_container_width=True, hide_index=True, column_config=cfg)

    with tab_all:
        col_l, col_r = st.columns([3, 1])
        with col_l:
            sort_choice = st.radio("Sort by", ["score", "depth", "skew", "volume"],
                                   horizontal=True, key="nfl_sort_all", index=0)
        with col_r:
            show_details = st.toggle("🔧 Show details", value=False, key="nfl_details_all")
        _render_nfl_table(filtered, sort_by=sort_choice, show_details=show_details)

    with tab_yes:
        show_y = st.toggle("🔧 Show details", value=False, key="nfl_details_yes")
        yes_rows = sorted([r for r in filtered if r["skew_side"] == "YES"],
                          key=lambda r: -r["yes_bid_depth"])
        _render_nfl_table(yes_rows, show_details=show_y)

    with tab_no:
        show_n = st.toggle("🔧 Show details", value=False, key="nfl_details_no")
        no_rows = sorted([r for r in filtered if r["skew_side"] == "NO"],
                         key=lambda r: -r["no_bid_depth"])
        _render_nfl_table(no_rows, show_details=show_n)

    # ---- Cross-venue snapshot ----

    if not ODDS_KEY:
        return

    st.markdown("---")
    st.markdown("### 🎯 Cross-venue snapshot — sharp signal vs sportsbook line")
    st.caption(
        "For each strong signal, compare the Polymarket mid (what sharps imply) "
        "against the DraftKings/FanDuel price for the same market. "
        "**PM % − SB %** = edge. Positive = sportsbook is softer than sharps think."
    )

    sb_games = _fetch_nfl_h2h(ODDS_KEY)

    def _amer_to_imp(am):
        return 100.0 / (am + 100.0) if am > 0 else abs(am) / (abs(am) + 100.0)

    def _find_sb_price(games, away_short, home_short, market_type, side_yes):
        for g in games:
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
        away = r.get("away_team") or ""; home = r.get("home_team") or ""
        if not away or not home:
            continue
        side_yes = r["skew_side"] == "YES"
        sb_p, sb_imp, sb_bk = _find_sb_price(sb_games, away.split()[-1], home.split()[-1], mt, side_yes)
        pm_imp = round((r["mid"] if side_yes else (1 - r["mid"])) * 100, 1)
        edge = round(pm_imp - sb_imp, 1) if sb_imp is not None else None
        if edge is None:   verdict = "❓ no SB match"
        elif edge >= 5:    verdict = "✅ BET — real edge"
        elif edge >= 3:    verdict = "🟡 marginal edge"
        elif edge >= 0:    verdict = "⚠️ confirms SB"
        else:              verdict = "🚫 SB favors other side"
        cross_rows.append({
            "Verdict": verdict, "Sharp pick": r.get("sharp_pick",""),
            "Game": r["event"][:36], "Mkt": r["category"],
            "Skew %": r["skew_strength"], "PM %": pm_imp,
            "SB %": sb_imp, "Edge pp": edge,
            "SB price": sb_p, "SB book": sb_bk or "—",
        })

    if cross_rows:
        cdf = pd.DataFrame(cross_rows)
        cdf["_sort"] = pd.to_numeric(cdf["Edge pp"], errors="coerce").fillna(-999)
        cdf = cdf.sort_values("_sort", ascending=False).drop(columns="_sort")
        st.dataframe(cdf, use_container_width=True, hide_index=True,
            column_config={
                "Skew %":  st.column_config.NumberColumn(format="%.0f%%"),
                "PM %":    st.column_config.NumberColumn(format="%.1f%%"),
                "SB %":    st.column_config.NumberColumn(format="%.1f%%"),
                "Edge pp": st.column_config.NumberColumn(format="%+.1f"),
                "SB price": st.column_config.NumberColumn(format="%+d"),
            })
        st.caption(
            "✅ BET = 5pp+ edge  ·  🟡 marginal = 3-5pp  ·  "
            "⚠️ confirms SB = sharp agrees but no edge  ·  🚫 SB favors other side = skip."
        )
    else:
        st.info("No h2h/totals signals strong enough (≥65% skew) to cross-reference right now.")

    st.markdown("---")
    st.caption(
        f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
        f"Cache TTL: 15 min  ·  Polymarket: Gamma + CLOB (free, NFL week-only filter)  ·  "
        f"SB cross-ref: Odds API (small quota)"
    )


# ═══════════════════════════════════════════════════════════════════
#  CFB tab — Odds API sportsbook line comparison
# ═══════════════════════════════════════════════════════════════════

def _render_cfb_sharp():
    st.caption(
        "Detects sharp money by comparing lines across 6 books (DK, FD, MGM, CZR, BOV, BOL).  \n"
        "**🔴 Off-market** = one book's line is already 1.5+ pts different from consensus — sharps moved it early.  \n"
        "**⚡ Steam** = DK+FD moved ahead of lag books by 1.0+ pts — sharp steam in progress.  \n"
        "**💧 Juice** = one side priced 15+ cents more expensive — book absorbing sharp action on that side.  \n"
        "Uses Odds API (small quota). Polymarket has no CFB markets."
    )

    if not ODDS_KEY:
        st.error("Missing `THE_ODDS_API_KEY` in secrets.")
        return

    ctrl_cols = st.columns([1.5, 1, 1, 1, 1])
    with ctrl_cols[0]:
        refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True,
                            key="cfb_refresh")
    with ctrl_cols[1]:
        min_strength = st.number_input("Min strength", value=1.0, step=0.5, key="cfb_min_strength",
                                       help="Minimum signal magnitude (pts or cents)")
    with ctrl_cols[2]:
        signal_filter = st.selectbox("Signal type", ["All", "Off-market", "Steam", "Juice"],
                                     key="cfb_signal_filter")
    with ctrl_cols[3]:
        market_filter = st.selectbox("Market", ["All", "Spread", "Total"], key="cfb_market_filter")
    with ctrl_cols[4]:
        sort_by = st.radio("Sort by", ["score", "strength", "signal"],
                           horizontal=True, key="cfb_sort_main")

    if refresh:
        _cfb_cached_scan.clear()
        st.toast("Cache cleared — scanning CFB lines...", icon="🔄")

    with st.spinner("Scanning CFB sportsbook lines..."):
        try:
            plays = _cfb_cached_scan(ODDS_KEY)
        except Exception as e:
            st.error(f"Scan failed: {e}")
            return

    if not plays:
        st.warning(
            "No CFB sharp signals right now — lines may not be posted yet "
            "(typically post Mon/Tue for the upcoming week). Check back later."
        )
        return

    def _sig_key(s):
        if "Off-market" in s: return "Off-market"
        if "Steam"      in s: return "Steam"
        if "Juice"      in s: return "Juice"
        return s

    filtered = [p for p in plays if p["strength"] >= min_strength]
    if signal_filter != "All":
        filtered = [p for p in filtered if signal_filter.lower() in p["signal"].lower()]
    if market_filter != "All":
        filtered = [p for p in filtered if p["market"] == market_filter]

    SIGNAL_BASE = {"Off-market": 45, "Steam": 32, "Juice": 18}

    def _score(p):
        base = SIGNAL_BASE.get(_sig_key(p["signal"]), 15)
        extra = min(30, int((p["strength"] - 1.0) / 0.5) * 8)
        return min(100, base + extra)

    def _tier(score):
        if score >= 70: return "🟢🟢🟢 ELITE"
        if score >= 50: return "🟢🟢 STRONG"
        if score >= 30: return "🟢 DECENT"
        return "🟡 WEAK"

    def _fp_label(iso_str):
        if not iso_str: return "?"
        try:
            ct = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(EASTERN)
            fmt = "%a %b %d %#I:%M %p ET" if sys.platform == "win32" else "%a %b %d %-I:%M %p ET"
            return ct.strftime(fmt)
        except Exception:
            return iso_str

    off_mkt = [p for p in filtered if "Off-market" in p["signal"]]
    steam   = [p for p in filtered if "Steam"      in p["signal"]]
    juice   = [p for p in filtered if "Juice"      in p["signal"]]

    st.markdown(f"### 🎯 {len(filtered)} sharp signals")

    tab_all, tab_off, tab_steam_t, tab_juice_t = st.tabs([
        f"📋 All ({len(filtered)})",
        f"🔴 Off-market ({len(off_mkt)})",
        f"⚡ Steam ({len(steam)})",
        f"💧 Juice ({len(juice)})",
    ])

    def _render_cfb_table(subset, tab_key, show_detail=False):
        if not subset:
            st.info("No signals in this bucket.")
            return
        rows = []
        for p in subset:
            sc = _score(p)
            row_d = {
                "Score": sc, "Tier": _tier(sc), "Signal": p["signal"],
                "Sharp pick": p["sharp_pick"], "Market": p["market"],
                "Strength": p["strength"], "Book": p["book"],
                "Game": p["game"], "Kickoff": _fp_label(p.get("first_pitch","")),
            }
            if show_detail:
                row_d["Detail"] = p.get("detail", "")
            rows.append(row_d)
        df = pd.DataFrame(rows)
        if sort_by == "score":
            df = df.sort_values("Score", ascending=False)
        elif sort_by == "strength":
            df = df.sort_values("Strength", ascending=False)
        elif sort_by == "signal":
            rank = {"Off-market": 0, "Steam": 1, "Juice": 2}
            df["_sig_rank"] = df["Signal"].map(lambda s: rank.get(_sig_key(s), 9))
            df = df.sort_values(["_sig_rank", "Score"], ascending=[True, False])
            df = df.drop(columns="_sig_rank")
        st.dataframe(df, use_container_width=True, hide_index=True,
            column_config={
                "Score":    st.column_config.NumberColumn(format="%d"),
                "Strength": st.column_config.NumberColumn(format="%.1f"),
            })

    with tab_all:
        show_d = st.toggle("🔧 Show detail", value=False, key="cfb_d_all")
        _render_cfb_table(filtered, "all", show_detail=show_d)
    with tab_off:
        show_d2 = st.toggle("🔧 Show detail", value=False, key="cfb_d_off")
        _render_cfb_table(off_mkt, "off", show_detail=show_d2)
    with tab_steam_t:
        show_d3 = st.toggle("🔧 Show detail", value=False, key="cfb_d_steam")
        _render_cfb_table(steam, "steam", show_detail=show_d3)
    with tab_juice_t:
        show_d4 = st.toggle("🔧 Show detail", value=False, key="cfb_d_juice")
        _render_cfb_table(juice, "juice", show_detail=show_d4)

    st.markdown("---")
    st.markdown("### 🎯 Top 5 strongest signals")
    top5 = sorted(filtered, key=lambda p: -_score(p))[:5]
    for p in top5:
        sc = _score(p)
        with st.expander(
            f"**{p['sharp_pick']}**  ·  {p['signal']}  ·  {p['game']}  ·  "
            f"Score {sc}  ·  {_fp_label(p.get('first_pitch',''))}",
            expanded=True,
        ):
            st.write(
                f"**Signal:** {p['signal']}  \n"
                f"**Detail:** {p.get('detail','')}  \n"
                f"**Strength:** {p['strength']}  ·  **Market:** {p['market']}  ·  **Book:** {p['book']}"
            )

    st.markdown("---")
    st.caption(
        f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
        f"Cache TTL: 5 min  ·  Books: DK · FD · MGM · CZR · BOV · BOL  ·  Odds API (small quota)"
    )


# ═══════════════════════════════════════════════════════════════════
#  Sport tabs
# ═══════════════════════════════════════════════════════════════════

tab_nfl, tab_cfb = st.tabs(["🏈 NFL", "🎓 CFB"])

with tab_nfl:
    _render_nfl_sharp()

with tab_cfb:
    _render_cfb_sharp()
