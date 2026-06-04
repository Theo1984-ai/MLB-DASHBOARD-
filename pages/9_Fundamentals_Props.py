"""
📊 Fundamentals Props — applies the Statcast-only framework:

  1. xwOBA / xSLG percentile (luck-stripped quality of contact)
  2. Hard-Hit % + Barrel % percentile (regression signal)
  3. Macro pitch-type / L-R splits (NOT individual BvP)
  4. Cross-book line dispersion (sharp money / line movement proxy)

Surfaces today's plays where elite Statcast batters have actionable props,
ranked by composite × consensus × cross-book gap.
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

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="Fundamentals Props", page_icon="📊", layout="wide")
st.title("📊 Fundamentals Props")
st.caption(
    "Today's prop plays found via the Statcast framework — **no model trust, "
    "no BvP noise.** Ranks plays by composite Statcast percentile × market "
    "consensus probability × cross-book disagreement (line-movement proxy)."
)


# ---------- Secrets ----------

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


# ---------- Cached scan ----------

@st.cache_data(ttl=3600, show_spinner="Loading Statcast data + scanning today's slate (~45s)...")
def cached_scan(_api_key):
    from scripts.fundamentals_scanner import scan
    return scan(_api_key)


# ---------- Controls ----------

c1, c2, c3, c4 = st.columns([1.4, 1, 1, 2.5])
with c1:
    refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True,
                        help="Clear cache and re-pull Statcast + odds data")
with c2:
    min_composite = st.slider("Min Statcast %ile", 50, 95, 70, 5,
                               help="Filter elite batters to top-N percentile composite "
                                    "(xwOBA + xSLG + Hard-Hit + Barrel)")
with c3:
    min_consensus = st.slider("Min consensus %", 0, 80, 50, 5,
                               help="Minimum market consensus probability for the pick")
with c4:
    st.caption(
        "💡 **Composite ≥85** = top-tier hitter quality  \n"
        "**Cross-book Δ ≥3pp** = sharp money signal  \n"
        "**Consensus ≥70%** = market alignment"
    )

if refresh:
    cached_scan.clear()
    st.toast("Cache cleared — re-pulling fresh data...", icon="🔄")


# ---------- Run scan ----------

result = cached_scan(ODDS_KEY)
elite = result["elite"]
plays = result["plays"]
scanned_at = datetime.fromisoformat(result["scanned_at"])

st.caption(
    f"Last scan: **{scanned_at.strftime('%I:%M %p %Z')}**  ·  "
    f"{result['n_games']} games  ·  "
    f"{result['n_elite_batters']} elite batters facing today  ·  "
    f"{result['n_plays']} prop offers analyzed"
)


# ============================================================
# 1) TOP PLAYS — composite × consensus × cross-book gap
# ============================================================

st.markdown("---")
st.markdown("### 🏆 Top Prop Plays")
st.caption(
    "Ranked by: **Statcast composite × market consensus % × cross-book dispersion**. "
    "The framework's three pillars combined into one score."
)

# Filter applied
filtered_plays = [p for p in plays
                  if p["score"] >= min_composite
                  and p["consensus_pct"] >= min_consensus]

# Dedupe: best play per (batter, market type)
seen_keys = set()
deduped = []
for p in filtered_plays:
    key = (p["name"], p["market"].split("*")[0], p["side"], p["point"])
    if key in seen_keys: continue
    seen_keys.add(key)
    deduped.append(p)

if not deduped:
    st.warning(
        f"No plays passing filter (composite ≥{min_composite}%ile, "
        f"consensus ≥{min_consensus}%). Try lowering the thresholds."
    )
else:
    # Tag each row by signal strength
    rows = []
    for p in deduped:
        # Signal tier — combined view of all 3 framework pillars
        tier = ""
        if p["score"] >= 90 and p["consensus_pct"] >= 70 and p["cross_book_pp"] >= 4:
            tier = "🟢🟢 Triple-aligned"
        elif p["score"] >= 85 and p["consensus_pct"] >= 65 and p["cross_book_pp"] >= 3:
            tier = "🟢 Strong"
        elif p["score"] >= 80 and p["consensus_pct"] >= 60:
            tier = "🟡 Moderate"
        else:
            tier = "—"
        pt_str = "-" if p["point"] is None else str(p["point"]).rstrip("0").rstrip(".") or str(p["point"])
        rows.append({
            "Tier":         tier,
            "Batter":       p["name"][:24],
            "Comp":         p["score"],
            "Mkt":          p["market"],
            "Side":         p["side"],
            "Line":         pt_str,
            "Price":        p["best_price"],
            "Book":         p["best_book"][:8],
            "Cons %":       p["consensus_pct"],
            "X-Book Δ":     p["cross_book_pp"],
            "EV / $100":    p["ev_per_100"],
            "Matchup":      p["matchup"],
            "vs Pitcher":   p["opp_pitcher"],
        })
    df = pd.DataFrame(rows)
    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={
            "Comp":      st.column_config.NumberColumn(format="%.0f",
                          help="Statcast composite percentile (xwOBA+xSLG+Hard-Hit+Barrel)"),
            "Price":     st.column_config.NumberColumn(format="%+d"),
            "Cons %":    st.column_config.NumberColumn(format="%.1f%%",
                          help="Market consensus probability across 3+ sharp books"),
            "X-Book Δ":  st.column_config.NumberColumn(format="%+.1f",
                          help="Best vs worst book disagreement (line movement / sharp signal)"),
            "EV / $100": st.column_config.NumberColumn(format="$%+.2f"),
        },
    )


# ============================================================
# 2) TOP 5 STRONGEST SIGNALS — full detail
# ============================================================

st.markdown("---")
st.markdown("### 🔍 Top 5 Strongest Signals — full detail")

if not deduped:
    st.info("Adjust filter sliders to see plays.")
else:
    # Top 5 by combined score
    for p in deduped[:5]:
        line_str = "—" if p["point"] is None else str(p["point"]).rstrip("0").rstrip(".") or str(p["point"])
        play_str = f"{p['name']} — {p['market']} {p['side']} {line_str}"
        with st.expander(
            f"**{play_str}**  @  **{p['best_price']:+d}** ({p['best_book']})  ·  "
            f"comp {p['score']:.0f}  ·  cons {p['consensus_pct']:.0f}%  ·  Δ{p['cross_book_pp']:+.1f}pp",
            expanded=True,
        ):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Composite", f"{p['score']:.0f} pctile")
            c2.metric("Consensus", f"{p['consensus_pct']:.1f}%")
            c3.metric("Cross-book Δ", f"{p['cross_book_pp']:+.1f}pp")
            c4.metric("EV per $100", f"${p['ev_per_100']:+.2f}")

            framework_lines = []
            if p.get("xwoba") is not None:
                framework_lines.append(f"**xwOBA:** {p['xwoba']:.3f}")
            if p.get("hard_hit") is not None:
                framework_lines.append(f"**Hard-Hit %:** {p['hard_hit']:.1f}")
            if p.get("barrel") is not None:
                framework_lines.append(f"**Barrel %:** {p['barrel']:.1f}")
            st.write(
                f"**Game:** {p['matchup']} (vs **{p['opp_pitcher']}**)  \n"
                + ("  ·  ".join(framework_lines) + "  \n" if framework_lines else "")
                + f"**Why:** Statcast composite **{p['score']:.0f} percentile** "
                f"(elite tier). Sharp books consensus **{p['consensus_pct']:.1f}%** "
                f"for {p['side']} {line_str}. Cross-book gap of "
                f"**{p['cross_book_pp']:+.1f}pp** between best and worst book "
                f"= line-movement signal (sharp money flowing toward this side)."
            )


# ============================================================
# 3) CONTRARIAN FLAGS — elite hitters with UNDER lines
# ============================================================

st.markdown("---")
st.markdown("### ⚠️ Contrarian Flags")
st.caption(
    "Elite Statcast batters with UNDER lines priced strongly. **Not necessarily plays** "
    "— more likely a signal that the opposing pitcher is elite and the market knows. "
    "Investigate before betting either side."
)

contrarian = [p for p in plays
              if p["score"] >= 85 and p["side"] == "Under"
              and p["consensus_pct"] >= 55]
seen2 = set()
contrarian_unique = []
for p in contrarian:
    k = (p["name"], p["market"].split("*")[0])
    if k in seen2: continue
    seen2.add(k)
    contrarian_unique.append(p)

if not contrarian_unique:
    st.info("No contrarian flags right now.")
else:
    crows = []
    for p in contrarian_unique[:10]:
        line_str = "—" if p["point"] is None else str(p["point"]).rstrip("0").rstrip(".") or str(p["point"])
        crows.append({
            "Batter":     p["name"][:24],
            "Comp":       p["score"],
            "Mkt":        p["market"],
            "Side":       p["side"],
            "Line":       line_str,
            "Price":      p["best_price"],
            "Cons %":     p["consensus_pct"],
            "vs Pitcher": p["opp_pitcher"],
            "Matchup":    p["matchup"],
        })
    cdf = pd.DataFrame(crows)
    st.dataframe(
        cdf, use_container_width=True, hide_index=True,
        column_config={
            "Comp":   st.column_config.NumberColumn(format="%.0f"),
            "Price":  st.column_config.NumberColumn(format="%+d"),
            "Cons %": st.column_config.NumberColumn(format="%.1f%%"),
        },
    )


# ============================================================
# 4) ELITE BATTERS REFERENCE — full list facing today
# ============================================================

with st.expander(f"📋 All {len(elite)} elite batters facing today (composite ≥ {min_composite})"):
    erows = []
    for e in elite:
        erows.append({
            "Comp":       e["score"],
            "Batter":     e["name"],
            "xwOBA":      e.get("xwoba"),
            "Hard-Hit %": e.get("hard_hit"),
            "Barrel %":   e.get("barrel"),
            "Team":       e["team"],
            "vs Pitcher": e["opp_pitcher"],
            "Matchup":    e["matchup"],
        })
    edf = pd.DataFrame(erows)
    st.dataframe(
        edf, use_container_width=True, hide_index=True,
        column_config={
            "Comp":       st.column_config.NumberColumn(format="%.0f"),
            "xwOBA":      st.column_config.NumberColumn(format="%.3f"),
            "Hard-Hit %": st.column_config.NumberColumn(format="%.1f"),
            "Barrel %":   st.column_config.NumberColumn(format="%.1f"),
        },
    )


# ---------- Footer ----------

st.markdown("---")
st.caption(
    "**Framework principles applied:** xwOBA / xSLG / Hard-Hit / Barrel% percentiles · "
    "Macro splits (not BvP individual matchup noise) · Cross-book dispersion as sharp money proxy.  \n"
    f"Cache TTL: 1 hour  ·  Source: Baseball Savant + The Odds API (5 sharp books)"
)
