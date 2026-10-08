"""
🏒💰 NHL Polymarket Sharp Money tracker.

Exact mirror of 8_Sharp_Money.py (MLB):
  - Same Polymarket order-book skew logic
  - Same persistence tracking (reads nhl_sharp_money_history/)
  - Same composite sharp score (confluence + persistence + depth ratio + skew + edge)
  - Same cross-venue arb table vs The Odds API NHL lines
  - Same whale detection, tier labels, and confluence display
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
from scripts.sportsbook_matcher import match_signals  # noqa: E402

EASTERN = ZoneInfo("America/New_York")


# =============================================================================
# Confluence helpers — mirrors MLB Sharp Money exactly, NHL-adapted
# =============================================================================

def _normalize_team_conf(s):
    if not s: return ""
    s2 = s.lower().replace(",","").replace(".","").replace("'","").strip()
    parts = s2.split()
    return parts[-1] if parts else ""


def _make_game_key(away, home):
    a = _normalize_team_conf(away); h = _normalize_team_conf(home)
    if not a or not h: return ""
    return "|".join(sorted([a, h]))


def _split_event(event_str):
    if not event_str: return (None, None)
    for sep in (" vs. ", " vs ", " @ ", " at "):
        if sep in event_str:
            a, b = event_str.split(sep, 1)
            return (a.strip(), b.strip())
    return (None, None)


def _load_today_snapshot(dirname):
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    today = _dt.now(tz=_Z("America/New_York")).strftime("%Y-%m-%d")
    path = os.path.join(ROOT, dirname, f"{today}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        try:
            return _json.loads(raw).get("picks", [])
        except Exception:
            pass
        if "<<<<<<<" in raw or "=======" in raw or ">>>>>>>" in raw:
            out, skip = [], False
            for line in raw.split("\n"):
                if line.startswith("<<<<<<<"): skip = True; continue
                if line.startswith("======="): skip = False; continue
                if line.startswith(">>>>>>>"): continue
                if skip: continue
                out.append(line)
            return _json.loads("\n".join(out)).get("picks", [])
        return []
    except Exception:
        return []


def _build_play_index(picks):
    """Index NHL true-prob picks by (game_key, market_type, side_id)."""
    idx = {}
    for p in picks:
        away = p.get("away_team", ""); home = p.get("home_team", "")
        if not away or not home:
            a2, h2 = _split_event(p.get("game") or "")
            if a2: away = a2
            if h2: home = h2
        gkey = _make_game_key(away, home)
        if not gkey:
            continue
        stat = (p.get("stat_key") or "").lower()
        side = (p.get("side") or "").lower()
        point = p.get("point")
        if stat == "h2h":
            sel = _normalize_team_conf(p.get("selection") or p.get("player") or "")
            idx[(gkey, "ml", sel)] = p
        elif stat == "spread":
            side_lower = side
            team_name = (p.get("away_team") if side_lower == "away"
                         else p.get("home_team") if side_lower == "home"
                         else None)
            if not team_name:
                # parse team from selection string e.g. "Red Wings +1.5"
                sel_str = (p.get("selection") or "")
                parts = sel_str.split()
                team_name = " ".join(parts[:-1]) if len(parts) > 1 else sel_str
            team_norm = _normalize_team_conf(team_name)
            try: pt_str = f"{float(point):g}"
            except Exception: pt_str = str(point)
            if team_norm:
                idx[(gkey, "spread", f"{team_norm}_{pt_str}")] = p
        elif stat == "total":
            try: pt_str = f"{float(point):g}"
            except Exception: pt_str = str(point)
            side_lower = side  # "over" or "under"
            if side_lower in ("over", "under"):
                idx[(gkey, "tot", f"{side_lower}_{pt_str}")] = p
    return idx


_tp_picks = _load_today_snapshot("nhl_true_prob_history")
tp_idx = _build_play_index(_tp_picks)

st.set_page_config(page_title="NHL Sharp Money", page_icon="🏒", layout="wide")
st.title("🏒💰 NHL Polymarket Sharp Money")
st.caption(
    "For each open NHL game market on Polymarket, we measure depth on each "
    "side of the inside spread (within ±5¢ of mid). When 70%+ of resting "
    "bids sit on one side, that's where the smart money wants to bet at "
    "better prices than the current mid — a sharp positioning signal.  \n"
    "**Free data — no Odds API quota used.**"
)


# ---------- Cached scan ----------

@st.cache_data(ttl=900, show_spinner="Pulling Polymarket NHL order books (~30s)...")
def cached_scan(min_volume, min_liquidity, top_n):
    return scan(min_volume=min_volume, min_liquidity=min_liquidity, top_n=top_n)


# ---------- Controls ----------

ctrl_cols = st.columns([1.5, 1, 1, 1, 1, 1])
with ctrl_cols[0]:
    refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True,
                        help="Clears the 15-min cache and pulls fresh order books")
with ctrl_cols[1]:
    top_n = st.selectbox("Markets to scan", [30, 50, 75, 100], index=1)
with ctrl_cols[2]:
    min_liquidity = st.number_input("Min liquidity $", value=5000, step=1000,
                                    help="Polymarket liquidity floor — NHL markets are thinner than MLB")
with ctrl_cols[3]:
    min_skew = st.slider("Min skew %", 50, 95, 60, 5)
with ctrl_cols[4]:
    min_depth = st.number_input("Min depth $", value=200, step=100)
with ctrl_cols[5]:
    st.caption(
        "💡 70%+ skew = strong  \n"
        "85%+ skew = extreme  \n"
        "$5K+ liq = real money"
    )

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


# =============================================================================
# Persistence index — mirrors MLB page exactly
# =============================================================================

@st.cache_data(ttl=120, show_spinner=False)
def _load_persistence_index():
    import json as _j
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    path = os.path.join(ROOT, "nhl_sharp_money_history", f"{today}.json")
    idx = {}
    if not os.path.exists(path):
        return idx
    try:
        with open(path, encoding="utf-8") as f:
            for p in _j.load(f).get("picks", []) or []:
                key = (p.get("game", "").strip(), p.get("sharp_pick", "").strip())
                idx[key] = {
                    "n":     p.get("n_appearances") or 1,
                    "first": p.get("first_seen_at"),
                    "last":  p.get("last_seen_at"),
                    "hist":  p.get("skew_history") or [],
                }
    except Exception:
        pass
    return idx


_persist_idx = _load_persistence_index()


def _lookup_persistence(row):
    key = (str(row.get("event", "")).strip(),
           str(row.get("sharp_pick", "")).strip())
    return _persist_idx.get(key)


def _confluence_check(sharp_row):
    """Returns list of source names that ALSO have this play."""
    away = sharp_row.get("away_team", ""); home = sharp_row.get("home_team", "")
    if not away or not home:
        a2, h2 = _split_event(sharp_row.get("event") or "")
        if a2: away = a2
        if h2: home = h2
    gkey = _make_game_key(away, home)
    if not gkey:
        return []
    mt = sharp_row.get("match_type")
    sharp_side = sharp_row.get("skew_side")
    hits = []
    if mt == "h2h":
        target = away if sharp_side == "YES" else home
        target_norm = _normalize_team_conf(target)
        if (gkey, "ml", target_norm) in tp_idx:
            hits.append("True Prob")
        if "True Prob" not in hits:
            for key in list(tp_idx.keys()):
                if (key[0] == gkey and key[1] == "spread"
                        and key[2].startswith(f"{target_norm}_")):
                    hits.append("True Prob")
                    break
    elif mt == "totals":
        side_str = "over" if sharp_side == "YES" else "under"
        pt = sharp_row.get("point")
        try: pt_str = f"{float(pt):g}"
        except Exception: pt_str = str(pt)
        if (gkey, "tot", f"{side_str}_{pt_str}") in tp_idx:
            hits.append("True Prob")
        else:
            for key in list(tp_idx.keys()):
                if key[0] == gkey and key[1] == "tot":
                    try:
                        s, p2 = key[2].rsplit("_", 1)
                        if s == side_str and abs(float(p2) - float(pt)) <= 1.0:
                            if "True Prob" not in hits:
                                hits.append("True Prob")
                            break
                    except Exception:
                        continue
    elif mt == "spreads":
        team_norm = _normalize_team_conf(sharp_row.get("team") or "")
        pt = sharp_row.get("point")
        try: pt_str = f"{float(pt):g}"
        except Exception: pt_str = str(pt)
        if (gkey, "spread", f"{team_norm}_{pt_str}") in tp_idx:
            hits.append("True Prob")
        elif (gkey, "ml", team_norm) in tp_idx:
            hits.append("True Prob")
    return hits


# Annotate each row with persistence AND confluence before scoring
for r in filtered:
    p = _lookup_persistence(r)
    r["_n_seen"] = p["n"] if p else 0
    r["_confluence"] = _confluence_check(r)


# =============================================================================
# Helpers — identical to MLB Sharp Money
# =============================================================================

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
    if whale >= 0.80:              score -= 25
    elif whale >= 0.60:            score -= 15
    elif whale >= 0.45:            score -= 8
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
        if sharp_side == "YES":
            base = f"UNDER {pt}" if pt is not None else "UNDER"
        else:
            base = f"OVER {pt}"  if pt is not None else "OVER"
        return f"{base}{match_str}"
    if mt == "spreads":
        team = r.get("team")
        pt   = r.get("point")
        opp  = r.get("home_team") if (r.get("away_team") or "").lower() in (team or "").lower() else r.get("away_team")
        if sharp_side == "YES":
            opp_pt = -(pt or 0)
            return f"{opp or 'Opponent'} {'+' if opp_pt>0 else ''}{opp_pt}"
        return f"{team} {'+' if (pt or 0)>0 else ''}{pt}"
    return "?"


st.warning(
    "⚠️ **Sharp money ≠ good bet.** Check the Verdict column in the "
    "cross-venue table. Only ✅ BET verdicts (≥5pp edge) are real arbs.",
    icon="⚠️",
)
st.markdown(f"### 🎯 {len(filtered)} markets passing filter")
if not filtered:
    st.warning(
        f"No NHL markets with ≥{min_skew}% skew and ≥${min_depth} depth right now. "
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
        ratio   = _depth_ratio(sharp_depth, other_depth)
        persist = _lookup_persistence(r)
        n_seen  = persist["n"] if persist else None
        n_conf  = len(r.get("_confluence") or [])
        score   = _sharp_score(r, n_conf, n_seen or 0, ratio)
        tier    = _sharp_tier(score)
        row = {
            "Sharp pick":  f"{marker} {r.get('sharp_pick', '')}",
            "Score":       score,
            "Tier":        tier,
            "Skew %":      r["skew_strength"],
            "Depth ratio": ratio,
            "Confluence":  n_conf,
            "Game":        r["event"][:36],
        }
        if show_details:
            row.update({
                "Mkt":             r["category"],
                "Whale?":          _whale_label(r),
                "Seen":            n_seen,
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
        order = sorted(range(len(rows_subset)),
                       key=lambda i: rows_subset[i]["yes_bid_depth"] + rows_subset[i]["no_bid_depth"],
                       reverse=True)
        df_t = df_t.iloc[order]
    elif sort_by == "skew":
        df_t = df_t.sort_values("Skew %", ascending=False)
    elif sort_by == "score":
        df_t = df_t.sort_values("Score", ascending=False)
    elif sort_by == "ratio":
        df_t = df_t.sort_values("Depth ratio", ascending=False)
    elif sort_by == "persistence":
        order = sorted(range(len(rows_subset)),
                       key=lambda i: (_lookup_persistence(rows_subset[i]) or {}).get("n", 0),
                       reverse=True)
        df_t = df_t.iloc[order]

    full_cfg = {
        "Score":             st.column_config.NumberColumn(format="%d",
            help="Composite 0-100 score. Weights: confluence 30, persistence 20, "
                 "depth ratio 20, skew 15, edge 15. Whale penalty up to −25. "
                 "Tiers: 75+ ELITE, 55-74 STRONG, 35-54 DECENT, <35 WEAK."),
        "Confluence":        st.column_config.NumberColumn(format="%d",
            help="Number of other systems (True Prob) that also have this pick. "
                 "1 = True Prob agrees — highest-conviction signal."),
        "Whale?":            st.column_config.TextColumn(
            help="🐋 lone whale (80%+) = fragile · 🐟 concentrated · "
                 "🟢 distributed (5+ orders) · ⚪ small book"),
        "Seen":              st.column_config.NumberColumn(format="%d×",
            help="How many scans this signal has appeared in today."),
        "Mid (YES)":         st.column_config.NumberColumn(format="$%.3f"),
        "Spread":            st.column_config.NumberColumn(format="$%.3f"),
        "$ on sharp pick":   st.column_config.NumberColumn(format="$%,d"),
        "$ on other side":   st.column_config.NumberColumn(format="$%,d"),
        "Depth ratio":       st.column_config.NumberColumn(format="%.1fx"),
        "Skew %":            st.column_config.NumberColumn(format="%.0f%%"),
        "Volume $":          st.column_config.NumberColumn(format="$%,.0f"),
    }
    cfg = {k: v for k, v in full_cfg.items() if k in df_t.columns}
    st.dataframe(df_t, use_container_width=True, hide_index=True, column_config=cfg)


with tab_all:
    cl, cr = st.columns([3, 1])
    with cl:
        sort_choice = st.radio(
            "Sort by",
            ["score", "depth", "skew", "ratio", "persistence"],
            horizontal=True, key="sort_all", index=0,
            help="'score' recommended — composite of persistence + depth ratio + skew + edge")
    with cr:
        show_details = st.toggle("🔧 Show details", value=False, key="details_all",
                                 help="Adds Mkt, Whale?, Seen, bid depths, Mid, Spread, Volume")
    render_table(filtered, sort_by=sort_choice, show_details=show_details)

with tab_yes:
    show_d_y = st.toggle("🔧 Show details", value=False, key="details_yes")
    yes_only = [r for r in filtered if r["skew_side"] == "YES"]
    render_table(sorted(yes_only, key=lambda r: -r["yes_bid_depth"]), show_details=show_d_y)

with tab_no:
    show_d_n = st.toggle("🔧 Show details", value=False, key="details_no")
    no_only = [r for r in filtered if r["skew_side"] == "NO"]
    render_table(sorted(no_only, key=lambda r: -r["no_bid_depth"]), show_details=show_d_n)


# =============================================================================
# Confluence Plays — Sharp Money agrees with True Probability
# =============================================================================

st.markdown("---")
st.markdown("### 🎯 Confluence Plays — Sharp Money + True Probability agree")
st.caption(
    "When the same team / over-under shows up in **NHL Sharp Money** AND in "
    "today's **True Probability** (75%+ consensus, 3+ sharp books), "
    "that's a multi-source signal — much stronger than either alone."
)

confluent = sorted(
    [r for r in filtered if len(r["_confluence"]) >= 1],
    key=lambda r: (-len(r["_confluence"]), -r["skew_strength"],
                   -r["yes_bid_depth"] - r["no_bid_depth"])
)

if not confluent:
    sharp_games = sorted({_make_game_key(r.get("away_team",""), r.get("home_team",""))
                          for r in filtered if r.get("away_team")})
    tp_games = sorted({k[0] for k in tp_idx.keys()})
    overlap = [g for g in sharp_games if g in tp_games]

    st.info(
        "No confluence plays right now — no Sharp Money signals overlap with "
        "True Probability picks for today."
    )
    with st.expander("🔍 Why? (diagnostic)", expanded=False):
        st.markdown(f"""
- **Sharp Money picks today:** {len(filtered)} markets across {len(sharp_games)} games
- **True Prob picks loaded:** {len(_tp_picks)} picks across {len(tp_games)} games
- **Sharp ↔ True Prob game overlap:** {len(overlap)} games
""")
        if not _tp_picks:
            st.warning(
                "⚠️ **No True Probability snapshots saved for today yet.** "
                "Daily snapshots are written once the cron script runs. "
                "Until then, Confluence has nothing to compare against."
            )
        elif not overlap:
            st.warning(
                "Snapshots exist but no game-key overlap with today's Sharp Money scan. "
                "This usually means trackers ran for a different slate. "
                "Possibly stale snapshot."
            )
        else:
            st.info(
                "Same games appear in both — but different markets/sides. "
                "Sharp Money's pick didn't match True Prob's same-game pick. "
                "This is normal — both systems target different angles."
            )
else:
    crows = []
    for r in confluent[:15]:
        crows.append({
            "Tier":        "🟢🟢 + True Prob",
            "Sharp pick":  r.get("sharp_pick", ""),
            "Game":        r["event"][:36],
            "Skew %":      r["skew_strength"],
            "Liquidity $": r.get("liquidity", 0),
            "SB price":    r.get("sb_best_price"),
            "Edge pp":     r.get("edge_pp"),
            "Confirmed by": "True Prob",
        })
    cdf = pd.DataFrame(crows)
    st.dataframe(
        cdf, use_container_width=True, hide_index=True,
        column_config={
            "Skew %":      st.column_config.NumberColumn(format="%.0f%%"),
            "Liquidity $": st.column_config.NumberColumn(format="$%,.0f"),
            "SB price":    st.column_config.NumberColumn(format="%+d"),
            "Edge pp":     st.column_config.NumberColumn(format="%+.1f"),
        },
    )
    st.success(
        f"🔥 **{len(confluent)} confluence play{'s' if len(confluent)!=1 else ''} found.** "
        f"NHL Sharp Money + True Probability (75%+ consensus) both agree — "
        f"highest-conviction signals on the dashboard."
    )


# =============================================================================
# Cross-venue arb — mirrors MLB exactly, sport=icehockey_nhl
# =============================================================================

st.markdown("---")
st.markdown("### 🎯 Cross-venue plays — Polymarket signal vs NHL sportsbook")
st.caption(
    "For each strong NHL signal, we look up the matching sportsbook line. "
    "**Edge pp** = Polymarket sharp-implied % minus sportsbook implied %. "
    "Positive edge + strong skew = actionable mispricing."
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
    st.warning("No `THE_ODDS_API_KEY` configured — can't cross-reference sportsbook prices.")
else:
    @st.cache_data(ttl=300, show_spinner="Matching NHL sportsbook lines...")
    def cached_match(rows_tuple, _odds_key):
        return match_signals([dict(r) for r in rows_tuple], _odds_key,
                             sport="icehockey_nhl")

    strong = [r for r in filtered if r["skew_strength"] >= 65]
    if not strong:
        st.info("No strong signals (≥65% skew) to cross-reference.")
    else:
        rows_key = tuple(tuple(sorted(r.items())) for r in strong)
        try:
            matched = cached_match(rows_key, ODDS_KEY)
        except Exception as e:
            st.error(f"Sportsbook match failed: {e}")
            matched = []

        if matched:
            tbl = []
            for r in matched:
                marker = "💰💰" if r["skew_strength"] >= 85 else "💰"
                sharp_d = r["yes_bid_depth"] if r["skew_side"] == "YES" else r["no_bid_depth"]
                other_d = r["no_bid_depth"]  if r["skew_side"] == "YES" else r["yes_bid_depth"]
                edge = r.get("edge_pp")
                if edge is None:     verdict = "❓ no SB match"
                elif edge >= 5:      verdict = "✅ BET — real edge"
                elif edge >= 3:      verdict = "🟡 marginal edge"
                elif edge >= 0:      verdict = "⚠️ confirms SB — no arb"
                else:                verdict = "🚫 SB favors other side"
                tbl.append({
                    "Verdict":    verdict,
                    "Game":       r.get("event", "")[:36],
                    "Mkt":        r.get("category", ""),
                    "Sharp pick": f"{marker} {r.get('sharp_pick', '')}",
                    "$ on pick":  sharp_d,
                    "Other side": _other_side_label(r),
                    "$ on other": other_d,
                    "Skew %":     r["skew_strength"],
                    "SB price":   r.get("sb_best_price"),
                    "SB book":    r.get("sb_book", ""),
                    "PM %":       round((r["mid"] if r["skew_side"] == "YES"
                                        else 1 - r["mid"]) * 100, 1),
                    "SB %":       r.get("sb_implied_pct"),
                    "Edge pp":    edge,
                    "Play":       r.get("play", "")[:50],
                })
            df = pd.DataFrame(tbl)
            df["_edge_sort"] = pd.to_numeric(df["Edge pp"], errors="coerce").fillna(-999)
            df = df.sort_values("_edge_sort", ascending=False).drop(columns="_edge_sort")

            st.dataframe(
                df, use_container_width=True, hide_index=True,
                column_config={
                    "$ on pick":  st.column_config.NumberColumn(format="$%,d"),
                    "$ on other": st.column_config.NumberColumn(format="$%,d"),
                    "Skew %":     st.column_config.NumberColumn(format="%.0f%%"),
                    "SB price":   st.column_config.NumberColumn(format="%+d"),
                    "PM %":       st.column_config.NumberColumn(format="%.1f%%",
                                   help="Polymarket-implied probability for the sharp side"),
                    "SB %":       st.column_config.NumberColumn(format="%.1f%%",
                                   help="Sportsbook-implied probability for the sharp side"),
                    "Edge pp":    st.column_config.NumberColumn(format="%+.1f",
                                   help="PM % − SB %. Positive = sportsbook underpricing. "
                                        "Actionable at 3pp+, strong at 5pp+"),
                },
            )

            st.caption(
                "**Verdict:** ✅ BET = ≥5pp edge · 🟡 marginal = 3-5pp · "
                "⚠️ confirms SB = no arb · 🚫 SB favors other side — skip  \n"
                "_Always verify the live sportsbook price before betting._"
            )

            MIN_EDGE_FOR_ACTION = 3.0
            actionable = [r for r in matched
                          if r.get("edge_pp") is not None and r["edge_pp"] >= MIN_EDGE_FOR_ACTION
                          and r["skew_strength"] >= 70
                          and (r.get("liquidity") or 0) >= 5000
                          and r.get("sb_best_price") is not None]
            actionable.sort(key=lambda r: -r["edge_pp"])

            if actionable:
                st.markdown(f"#### 🏆 Top {min(5, len(actionable))} actionable plays")
                st.caption(
                    f"≥{MIN_EDGE_FOR_ACTION}pp edge AND ≥70% skew AND ≥$5K liquidity AND sportsbook match found."
                )
                for r in actionable[:5]:
                    edge = r["edge_pp"]
                    color = "🟢🟢" if edge >= 10 else ("🟢" if edge >= 5 else "🟡")
                    game = r.get("event", "?")
                    with st.expander(
                        f"{color} **{game}**  ·  {r['play']}  ·  edge {edge:+.1f}pp",
                        expanded=True,
                    ):
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Polymarket implied (sharp side)",
                                  f"{r['mid']*100 if r['skew_side']=='YES' else (1-r['mid'])*100:.1f}%")
                        c2.metric("Sportsbook implied", f"{r['sb_implied_pct']:.1f}%")
                        c3.metric("Edge", f"{edge:+.1f}pp")
                        st.write(
                            f"**Game:** {game}  \n"
                            f"**The play:** {r['play']}  \n"
                            f"**Sharp pick:** {r.get('sharp_pick', '')}  \n"
                            f"**Why:** Polymarket sharps have loaded {r['skew_side']} side "
                            f"with {r['skew_strength']:.0f}% bid depth on ${r['volume']:,.0f} volume. "
                            f"That implies a true probability of "
                            f"~{r['mid']*100 if r['skew_side']=='YES' else (1-r['mid'])*100:.0f}% "
                            f"while {r['sb_book']} is offering {r['sb_implied_pct']:.0f}% implied. "
                            f"Difference: **{edge:+.1f}pp edge**."
                        )
            else:
                st.info(
                    f"No actionable plays right now. Need ≥{MIN_EDGE_FOR_ACTION}pp edge + "
                    "≥70% skew + ≥$5K liquidity + sportsbook match. "
                    "Strong NHL cross-venue signals typically appear closer to puck drop "
                    "when sportsbook lines firm up."
                )


st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 15 min  ·  "
    f"Polymarket: Gamma + CLOB (free)  ·  "
    f"Sportsbook match: Odds API (small quota)"
)
