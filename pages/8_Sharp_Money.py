"""
💰 Polymarket Sharp Money tracker.

For every open MLB game-level market on Polymarket, shows where the
limit-order book has heaviest bid-side depth. Strong skew on one side
means real money is queueing up to bet that side at better prices —
pinpointing where sharps are positioned.
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

from scripts.polymarket_sharp import scan  # noqa: E402
from scripts.sportsbook_matcher import match_signals  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="Sharp Money", page_icon="💰", layout="wide")
st.title("💰 Polymarket Sharp Money")
st.caption(
    "For each open MLB game market on Polymarket, we measure depth on each "
    "side of the inside spread (within ±5¢ of mid). When 70%+ of resting "
    "bids sit on one side, that's where the smart money wants to bet at "
    "better prices than the current mid — a sharp positioning signal.  \n"
    "**Free data — no Odds API quota used.**"
)


# ---------- Cached scan ----------

@st.cache_data(ttl=900, show_spinner="Pulling Polymarket order books (~30s)...")
def cached_scan(min_volume, min_liquidity, top_n):
    return scan(min_volume=min_volume, min_liquidity=min_liquidity, top_n=top_n)


# ---------- Controls ----------

ctrl_cols = st.columns([1.5, 1, 1, 1, 1, 1])
with ctrl_cols[0]:
    refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True,
                        help="Clears the 15-min cache and pulls fresh order books")
with ctrl_cols[1]:
    top_n = st.selectbox("Markets to scan", [30, 50, 75, 100], index=1,
                         help="Top N MLB markets by volume to fetch order books for")
with ctrl_cols[2]:
    min_liquidity = st.number_input("Min liquidity $", value=10000, step=1000,
                                     help="Polymarket listed liquidity floor — "
                                          "$10K+ guarantees real money is behind the market")
with ctrl_cols[3]:
    min_skew = st.slider("Min skew %", 50, 95, 60, 5,
                         help="Only show markets with this much imbalance")
with ctrl_cols[4]:
    min_depth = st.number_input("Min depth $", value=500, step=500,
                                help="Bid-side depth within 5¢ of mid (filter out tiny books)")
with ctrl_cols[5]:
    st.caption(
        "💡 70%+ skew = strong  \n"
        "85%+ skew = extreme  \n"
        "$10K+ liq = real money"
    )

if refresh:
    cached_scan.clear()
    st.toast("Cache cleared — pulling fresh data...", icon="🔄")

# Scan with $10K min so we include all the $10K+ markets the user wants
rows, debug = cached_scan(min_volume=500, min_liquidity=min_liquidity, top_n=top_n)

st.caption(
    f"Scanned **{debug['total_events']}** MLB events → "
    f"**{debug['daily_markets']}** daily markets → "
    f"**{debug['candidates']}** with volume → "
    f"**{debug['with_book']}** with live order books."
)


# ---------- Filter applied ----------

filtered = [
    r for r in rows
    if r["skew_strength"] >= min_skew
    and (r["yes_bid_depth"] + r["no_bid_depth"]) >= min_depth
    and (r.get("liquidity") or 0) >= min_liquidity
]

st.warning(
    "⚠️ **Big sharp money ≠ good bet.** Always check the **Verdict** column "
    "in the Cross-venue plays table below. A market can have $2M of sharp money "
    "on one side and STILL be bad value if the sportsbook is already pricing it "
    "correctly. Only ✅ BET verdicts (≥5pp edge) are real arbs.",
    icon="⚠️",
)
st.markdown(f"### 🎯 {len(filtered)} markets passing filter")
if not filtered:
    st.warning(
        f"No markets with ≥{min_skew}% skew and ≥${min_depth} depth right now. "
        "Lower the threshold or wait — Polymarket books update constantly."
    )
    st.stop()


# ---------- Sharp-money split ----------

tab_all, tab_yes, tab_no = st.tabs([
    f"📋 All ({len(filtered)})",
    f"🟢 YES heavy ({sum(1 for r in filtered if r['skew_side']=='YES')})",
    f"🔴 NO heavy ({sum(1 for r in filtered if r['skew_side']=='NO')})",
])


def _opponent_team(team, event_title):
    """Given one team's name and the event title 'A vs B', return the other team."""
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
    """The label for the OPPOSITE side of the sharp pick (in human terms)."""
    mt = r.get("match_type")
    sharp_side = r.get("skew_side")
    if mt == "h2h":
        # Sharp on AWAY team if YES, HOME team if NO; other is the other team
        return r.get("home_team", "?") if sharp_side == "YES" else r.get("away_team", "?")
    if mt == "totals":
        # Sharp on OVER if YES, UNDER if NO
        pt = r.get("point")
        if sharp_side == "YES":
            return f"UNDER {pt}" if pt is not None else "UNDER"
        return f"OVER {pt}" if pt is not None else "OVER"
    if mt == "spreads":
        team = r.get("team")
        pt = r.get("point")
        # The opposite of "Team A -1.5 covers" is "Team B +1.5 covers"
        # (i.e., Team B wins OR Team A wins by exactly 1)
        opponent = _opponent_team(team, r.get("event", "")) or "Opponent"
        if sharp_side == "YES":
            # Sharp says Team A covers -> Other side = Team B gets the +N points
            opp_pt = -(pt or 0)
            sign = "+" if opp_pt > 0 else ""
            return f"{opponent} {sign}{opp_pt}"
        # Sharp says Team A doesn't cover -> Other side = Team A covers the original spread
        sign = "+" if (pt or 0) > 0 else ""
        return f"{team} {sign}{pt}"
    return "?"


def render_table(rows_subset, sort_by="depth"):
    if not rows_subset:
        st.info("No markets in this bucket.")
        return
    table = []
    for r in rows_subset:
        marker = "💰💰" if r["skew_strength"] >= 85 else ("💰" if r["skew_strength"] >= 70 else "")
        # Depth on the sharp side vs the other side (no more "YES bid $" jargon)
        sharp_depth = (r["yes_bid_depth"] if r["skew_side"] == "YES"
                       else r["no_bid_depth"])
        other_depth = (r["no_bid_depth"] if r["skew_side"] == "YES"
                       else r["yes_bid_depth"])
        table.append({
            "Game":             r["event"][:32],
            "Mkt":              r["category"],
            "Sharp pick":       f"{marker} {r.get('sharp_pick', '')}",
            "$ on sharp pick":  sharp_depth,
            "Other side":       _other_side_label(r),
            "$ on other side":  other_depth,
            "Skew %":           r["skew_strength"],
            "Mid (YES)":        r["mid"],
            "Spread":           r["spread"],
            "Volume $":         r["volume"],
        })
    df = pd.DataFrame(table)
    if sort_by == "depth":
        df["_sort"] = df["$ on sharp pick"] + df["$ on other side"]
        df = df.sort_values("_sort", ascending=False).drop(columns="_sort")
    elif sort_by == "skew":
        df = df.sort_values("Skew %", ascending=False)
    elif sort_by == "volume":
        df = df.sort_values("Volume $", ascending=False)

    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={
            "Mid (YES)":         st.column_config.NumberColumn(format="$%.3f"),
            "Spread":            st.column_config.NumberColumn(format="$%.3f"),
            "$ on sharp pick":   st.column_config.NumberColumn(format="$%,d"),
            "$ on other side":   st.column_config.NumberColumn(format="$%,d"),
            "Skew %":            st.column_config.NumberColumn(format="%.0f%%"),
            "Volume $":          st.column_config.NumberColumn(format="$%,.0f"),
        },
    )


with tab_all:
    sort_choice = st.radio("Sort by", ["depth", "skew", "volume"],
                           horizontal=True, key="sort_all")
    render_table(filtered, sort_by=sort_choice)

with tab_yes:
    yes_only = [r for r in filtered if r["skew_side"] == "YES"]
    render_table(sorted(yes_only, key=lambda r: -r["yes_bid_depth"]))

with tab_no:
    no_only = [r for r in filtered if r["skew_side"] == "NO"]
    render_table(sorted(no_only, key=lambda r: -r["no_bid_depth"]))


# ---------- Top 3 strongest signals — detail expanders ----------

st.markdown("---")
st.markdown("### 🔍 Top 3 strongest signals — full detail")

strongest = sorted(filtered, key=lambda r: (
    -r["skew_strength"], -(r["yes_bid_depth"] + r["no_bid_depth"])
))[:3]

for r in strongest:
    fav_side = r["skew_side"]
    fav_pct = r["skew_strength"]
    sharp_depth = r["no_bid_depth"] if fav_side == "NO" else r["yes_bid_depth"]
    other_depth = r["yes_bid_depth"] if fav_side == "NO" else r["no_bid_depth"]
    interpretation = (
        f"**The book is loaded on the {fav_side} side** "
        f"(${sharp_depth:,} vs ${other_depth:,} on the other). "
        f"That means real money is queued to buy {fav_side} at better prices "
        f"than the current mid (${r['mid']:.3f}). "
        f"Sharp interpretation: market expects {fav_side}."
    )

    with st.expander(
        f"**{r['question'][:80]}**  ·  {r['skew_side']} {fav_pct:.0f}% skew",
        expanded=True,
    ):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Mid (YES)", f"${r['mid']:.3f}")
        c2.metric("YES bid depth", f"${r['yes_bid_depth']:,}")
        c3.metric("NO bid depth", f"${r['no_bid_depth']:,}")
        c4.metric("Spread", f"${r['spread']:.3f}")
        st.markdown(interpretation)
        st.caption(f"Volume traded: ${r['volume']:,.0f}  ·  "
                   f"Listed liquidity: ${r['liquidity']:,.0f}  ·  "
                   f"Category: {r['category']}")



# =============================================================================
# CROSS-VENUE ARB — Polymarket sharp signal vs sportsbook line
# =============================================================================

st.markdown("---")
st.markdown("### 🎯 Cross-venue plays — sharp signal + sportsbook line")
st.caption(
    "For each strong Polymarket signal, we look up the matching sportsbook "
    "line. **Edge pp** = how many percentage points sharper than the "
    "sportsbook's implied probability the Polymarket sharps believe the play "
    "is. Positive edge + strong skew = actionable mispricing."
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
    st.warning("No `THE_ODDS_API_KEY` configured — can't cross-reference sportsbook prices.")
else:
    @st.cache_data(ttl=300, show_spinner="Matching sportsbook lines...")
    def cached_match(rows_tuple, _odds_key):
        # Convert back to list[dict] for the matcher
        return match_signals([dict(r) for r in rows_tuple], _odds_key)

    # Use only strongly-skewed rows (less noise)
    strong = [r for r in filtered if r["skew_strength"] >= 65]
    if not strong:
        st.info("No strong signals (≥65% skew) to cross-reference.")
    else:
        # cache key needs hashability — pass tuples of items
        rows_key = tuple(tuple(sorted(r.items())) for r in strong)
        try:
            matched = cached_match(rows_key, ODDS_KEY)
        except Exception as e:
            st.error(f"Sportsbook match failed: {e}")
            matched = []

        # Display table
        if matched:
            tbl = []
            for r in matched:
                marker = "💰💰" if r['skew_strength']>=85 else "💰"
                sharp_d = (r["yes_bid_depth"] if r["skew_side"]=="YES"
                           else r["no_bid_depth"])
                other_d = (r["no_bid_depth"] if r["skew_side"]=="YES"
                           else r["yes_bid_depth"])
                edge = r.get("edge_pp")
                # Verdict that makes it obvious whether to bet
                if edge is None:
                    verdict = "❓ no SB match"
                elif edge >= 5:
                    verdict = "✅ BET — real edge"
                elif edge >= 3:
                    verdict = "🟡 marginal edge"
                elif edge >= 0:
                    verdict = "⚠️ confirms SB — no arb"
                else:
                    verdict = "🚫 SB favors other side"
                tbl.append({
                    "Verdict":     verdict,
                    "Game":        r.get("event", "")[:32],
                    "Mkt":         r.get("category", ""),
                    "Sharp pick":  f"{marker} {r.get('sharp_pick', '')}",
                    "$ on pick":   sharp_d,
                    "Other side":  _other_side_label(r),
                    "$ on other":  other_d,
                    "Skew %":      r["skew_strength"],
                    "SB price":    r.get("sb_best_price"),
                    "SB book":     r.get("sb_book", ""),
                    "PM %":        round((r["mid"] if r["skew_side"]=="YES"
                                         else (1-r["mid"]))*100, 1),
                    "SB %":        r.get("sb_implied_pct"),
                    "Edge pp":     edge,
                    "Play":        r.get("play", "")[:50],
                })
            df = pd.DataFrame(tbl)
            # Sort by edge (highest first)
            df_sorted = df.copy()
            df_sorted["_edge_for_sort"] = pd.to_numeric(df_sorted["Edge pp"], errors="coerce").fillna(-999)
            df_sorted = df_sorted.sort_values("_edge_for_sort", ascending=False).drop(columns="_edge_for_sort")

            st.dataframe(
                df_sorted, use_container_width=True, hide_index=True,
                column_config={
                    "$ on pick":   st.column_config.NumberColumn(format="$%,d"),
                    "$ on other":  st.column_config.NumberColumn(format="$%,d"),
                    "Skew %":      st.column_config.NumberColumn(format="%.0f%%"),
                    "SB price":    st.column_config.NumberColumn(format="%+d"),
                    "PM %":        st.column_config.NumberColumn(format="%.1f%%",
                                    help="Polymarket-implied probability for the sharp side"),
                    "SB %":        st.column_config.NumberColumn(format="%.1f%%",
                                    help="Sportsbook-implied probability for the sharp side"),
                    "Edge pp":     st.column_config.NumberColumn(format="%+.1f",
                                    help="PM % − SB %. Positive = sportsbook underpricing, "
                                         "actionable at 3pp+, strong at 5pp+, very strong at 10pp+"),
                },
            )

            st.caption(
                "**Verdict column legend:** "
                "✅ BET = ≥5pp edge (real arb)  ·  "
                "🟡 marginal = 3-5pp edge (small bet or skip)  ·  "
                "⚠️ confirms SB = sharp money agrees with sportsbook, no edge to exploit  ·  "
                "🚫 SB favors other side = sportsbook prices the OPPOSITE of sharps — skip the sharp pick  \n"
                "_Always verify the live sportsbook price before betting — odds move._"
            )

            # Top actionable plays — REAL edge (>=3pp), strong skew, real liquidity.
            # Sub-3pp edges get eaten by sportsbook vig (~4-5pp typical).
            MIN_EDGE_FOR_ACTION = 3.0
            actionable = [r for r in matched
                          if r.get("edge_pp") is not None and r["edge_pp"] >= MIN_EDGE_FOR_ACTION
                          and r["skew_strength"] >= 70
                          and (r.get("liquidity") or 0) >= 10000
                          and r.get("sb_best_price") is not None]
            actionable.sort(key=lambda r: -r["edge_pp"])

            if actionable:
                st.markdown(f"#### 🏆 Top {min(5, len(actionable))} actionable plays")
                st.caption(
                    f"≥{MIN_EDGE_FOR_ACTION}pp edge AND ≥70% skew AND ≥$10K liquidity AND sportsbook match found. "
                    "Sub-3pp edges get eaten by sportsbook vig (~4-5pp) — those aren't shown."
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
                        c3.metric("Edge", f"{edge:+.1f}pp", help="PM sharp implied − SB implied")
                        st.write(
                            f"**Game:** {game}  \n"
                            f"**The play:** {r['play']}  \n"
                            f"**Sharp pick:** {r.get('sharp_pick', '')}  \n"
                            f"**Why:** Polymarket sharps have loaded {r['skew_side']} side "
                            f"with {r['skew_strength']:.0f}% bid depth on ${r['volume']:,.0f} volume. "
                            f"That implies they think the true probability is "
                            f"~{r['mid']*100 if r['skew_side']=='YES' else (1-r['mid'])*100:.0f}% "
                            f"while {r['sb_book']} is offering {r['sb_implied_pct']:.0f}% implied. "
                            f"Difference: **{edge:+.1f}pp edge**."
                        )
            else:
                st.info(
                    f"**No actionable plays right now.** "
                    f"Need ≥{MIN_EDGE_FOR_ACTION}pp edge + ≥70% skew + ≥$10K liquidity + sportsbook match.  \n"
                    f"This is normal — strong cross-venue mispricing only shows up "
                    f"a few times per slate, usually closer to first pitch when "
                    f"sportsbook lines firm up. Marginal-edge plays (sub-3pp) "
                    f"appear in the table above but aren't worth betting after vig."
                )


st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 15 min  ·  "
    f"Polymarket: Gamma + CLOB (free)  ·  "
    f"Sportsbook match: Odds API (small quota)"
)
