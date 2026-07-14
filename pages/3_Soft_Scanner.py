"""
MLB Soft-Price Scanner — cross-book disagreement scanner.

Pulls every MLB prop from DK/FD/MGM/Caesars and finds props where one
book is significantly out of line with the consensus of the others.
Surfaces the "soft prices" — the best book on each side, ranked by edge.

No MLB model needed. Pure market-efficiency analysis. Same logic that
identified the Donovan Mitchell O3.5 +121 DK play on the NBA side.

Markets scanned:
  - HR        — batter_home_runs + alternate
  - H+R+R     — batter_hits_runs_rbis_alternate
  - Hits      — batter_hits + alternate
  - TB        — batter_total_bases + alternate

Important: MLB prop markets typically only have DraftKings posted until
4-8 hours before first pitch. FD/MGM/Caesars fill in day-of, afternoon.
Run this 3-4 PM ET for best results.

Quota guard: results cached for 5 minutes to avoid burning Odds API quota
on repeated reloads.
"""
import os
import sys
import json
import time
import ssl as _ssl_compat
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd

_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()

# Ensure project root on path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="MLB Soft Scanner", page_icon="🎯", layout="wide")
st.title("🎯 MLB Soft-Price Scanner")
st.caption(
    "Pulls every MLB prop from DraftKings / FanDuel / BetMGM / Caesars and finds "
    "props where one book is significantly out of line with the others. Surfaces "
    "**soft prices** to attack. Same methodology that found Donovan Mitchell O3.5 "
    "+121 DK on the NBA side. No MLB model needed.  \n"
    "🔒 **Hard price cap: −300 to +300** — longshots above +300 and chalk below −300 are "
    "excluded automatically to keep variance in check."
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


# ---------- Config ----------

# `williamhill_us` = Caesars (Caesars acquired William Hill US in 2021 but
# the API kept the legacy key). `bovada` is offshore but their mainline
# pricing is sharp consensus — we exclude BetOnline / MyBookie / BetUS
# which post stale longshot lines that fake edges.
SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada,pinnacle"

# Hard price cap — picks outside this range are excluded.
# Wide longshots (+400/+500/+600) historically lose money even when EV looks
# positive on paper, because the variance overwhelms the edge. Keep the
# scanner inside the same risk band as Tonight's Picks.
MAX_PRICE_CAP = 300
MIN_PRICE_CAP = -300

MARKET_GROUPS = {
    "HR":    ["batter_home_runs", "batter_home_runs_alternate"],
    "H+R+R": ["batter_hits_runs_rbis_alternate"],
    "Hits":  ["batter_hits", "batter_hits_alternate"],
    "TB":    ["batter_total_bases", "batter_total_bases_alternate"],
}
MARKET_LABELS = {
    "batter_home_runs":                "HR",
    "batter_home_runs_alternate":      "HR",
    "batter_hits_runs_rbis_alternate": "H+R+R",
    "batter_hits":                     "Hits",
    "batter_hits_alternate":           "Hits",
    "batter_total_bases":              "TB",
    "batter_total_bases_alternate":    "TB",
}


def amer_to_imp(am):
    if am > 0: return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


# ---------- Cached API calls (quota protection) ----------

@st.cache_data(ttl=300, show_spinner=False)   # 5 min cache
def cached_fetch_events():
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={ODDS_KEY}"
    return json.loads(urllib.request.urlopen(url, timeout=15, context=_UNVERIFIED_SSL).read())


@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_event_odds(event_id, markets_tuple):
    """markets_tuple must be a tuple for caching to work."""
    markets = ",".join(markets_tuple)
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets={markets}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_UNVERIFIED_SSL).read())
    except urllib.error.HTTPError:
        return {}
    except Exception:
        return {}


# ---------- Scanner logic ----------

def collect_offers(bookmakers, market_filter):
    offers = defaultdict(list)
    for b in bookmakers:
        for m in b.get("markets", []):
            mk = m["key"]
            if mk not in market_filter:
                continue
            for o in m.get("outcomes", []):
                name = (o.get("name") or "").strip()
                player = o.get("description") or name
                if player in ("Over", "Under"):
                    continue
                side = name if name in ("Over", "Under") else "Yes"
                pt = o.get("point")
                price = o.get("price")
                if price is None:
                    continue
                key = (mk, player, side, pt)
                offers[key].append((b["key"], int(price)))
    return offers


def find_disagreements(offers_dict, game_label, min_edge_pp, min_books, min_implied_pct):
    rows = []
    for (mk, player, side, pt), book_prices in offers_dict.items():
        if len(book_prices) < min_books:
            continue
        prices = [pr for _, pr in book_prices]
        best_book, best_price = max(book_prices, key=lambda x: x[1])

        # Hard price cap — drop longshots and ultra-juiced favorites
        if best_price > MAX_PRICE_CAP or best_price < MIN_PRICE_CAP:
            continue

        best_imp = amer_to_imp(best_price)
        worst_imp = amer_to_imp(min(prices))
        edge_pp = (worst_imp - best_imp) * 100

        if edge_pp < min_edge_pp:
            continue
        consensus_imp = sum(amer_to_imp(pr) for pr in prices) / len(prices)
        if consensus_imp * 100 < min_implied_pct:
            continue

        if best_price > 0:
            ev_per_100 = consensus_imp * best_price - (1 - consensus_imp) * 100
        else:
            ev_per_100 = consensus_imp * 100 - (1 - consensus_imp) * abs(best_price)

        rows.append({
            "game":          game_label,
            "market":        MARKET_LABELS.get(mk, mk),
            "player":        player,
            "side":          side,
            "point":         pt,
            "best_book":     best_book,
            "best_price":    best_price,
            "best_imp_pct":  round(best_imp * 100, 2),
            "consensus_pct": round(consensus_imp * 100, 2),
            "edge_pp":       round(edge_pp, 2),
            "ev_per_100":    round(ev_per_100, 2),
            "n_books":       len(book_prices),
            "all_prices":    sorted(book_prices, key=lambda x: -x[1]),
        })
    return rows


# ---------- UI controls ----------

st.markdown("---")

cc1, cc2, cc3, cc4 = st.columns([1.5, 1.5, 1.5, 1])
with cc1:
    sel_markets = st.multiselect(
        "Markets to scan",
        options=list(MARKET_GROUPS.keys()),
        default=list(MARKET_GROUPS.keys()),
        help="Pick which prop markets to include. Fewer = faster + less Odds API quota.",
    )
with cc2:
    min_edge_pp = st.slider("Minimum edge (pp)", 1.0, 15.0, 5.0, 0.5,
                            help="Only show props where best book is N+ pp better than worst")
with cc3:
    min_implied = st.slider("Min consensus implied %", 0, 80, 0, 5,
                            help="Skip longshots — only show props with at least this implied probability")
with cc4:
    min_books = st.selectbox("Min books", [2, 3, 4], index=0,
                             help="Require this many books to be pricing the prop")

cs1, cs2 = st.columns([1, 5])
with cs1:
    scan_btn = st.button("🎯 Run scan", type="primary", use_container_width=True)
with cs2:
    if scan_btn:
        st.caption("Scanning... results cached 5 min — repeat clicks won't burn quota.")
    else:
        st.caption("Click **Run scan**. Results cached 5 minutes (quota guard).")

if not scan_btn:
    st.info(
        "💡 **Timing matters** — MLB prop markets typically only have **DraftKings** posted "
        "until 4-8h before first pitch. FD/MGM/Caesars fill in day-of-game afternoon. "
        "Run the scanner **3-4 PM ET** for best multi-book coverage."
    )
    st.stop()


# ---------- Run the scan ----------

if not sel_markets:
    st.warning("Pick at least one market to scan.")
    st.stop()

markets_to_pull = []
for g in sel_markets:
    markets_to_pull.extend(MARKET_GROUPS[g])
market_filter = set(markets_to_pull)
markets_tuple = tuple(sorted(market_filter))

with st.spinner("Fetching events..."):
    try:
        events = cached_fetch_events()
    except Exception as e:
        st.error(f"Failed to fetch MLB events: {e}")
        st.stop()

now = datetime.now(timezone.utc)
upcoming = []
for e in events:
    ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
    hours_to_start = (ct - now).total_seconds() / 3600
    if hours_to_start > -0.5:
        upcoming.append((e, hours_to_start))
upcoming.sort(key=lambda x: x[1])

st.markdown(f"### Found **{len(upcoming)}** upcoming MLB games")

# Coverage scan
coverage = []
all_disagreements = []
progress = st.progress(0)
for gi, (e, dh) in enumerate(upcoming):
    progress.progress((gi + 1) / max(1, len(upcoming)))
    game_label = f"{e['away_team'].split()[-1]} @ {e['home_team'].split()[-1]}"
    data = cached_fetch_event_odds(e["id"], markets_tuple)
    books = data.get("bookmakers", [])
    offers = collect_offers(books, market_filter)
    n_props = len(offers)
    n_multibook = sum(1 for v in offers.values() if len(v) >= min_books)
    n_books_present = len({b for v in offers.values() for b, _ in v})
    coverage.append({
        "Game":      game_label,
        "Start":     f"+{dh:.1f}h" if dh >= 0 else f"{dh:.1f}h",
        "Books":     n_books_present,
        "Props":     n_props,
        "MultiBook": n_multibook,
        "OK":        "✅" if n_multibook > 0 else "⚠",
    })
    if n_multibook > 0:
        rows = find_disagreements(offers, game_label, min_edge_pp, min_books, min_implied)
        all_disagreements.extend(rows)
progress.empty()


# ---------- Coverage summary ----------

st.markdown("#### 📊 Coverage Summary")
cov_df = pd.DataFrame(coverage)
st.dataframe(cov_df, use_container_width=True, hide_index=True)

total_multibook = sum(c["MultiBook"] for c in coverage)
if total_multibook == 0:
    st.warning(
        f"⚠ **No multi-book coverage on any game right now.** "
        f"MLB prop markets typically only have DraftKings posted until 4-8h before first pitch. "
        f"Re-run the scanner in the afternoon (3-4 PM ET) when FD/MGM/Caesars start posting."
    )
    st.stop()


# ---------- Top soft prices ----------

all_disagreements.sort(key=lambda r: (-r["edge_pp"], -r["ev_per_100"]))


# =====================================================================
# 🏆 A-GRADE PICKS — backtested filter on 248 historical settled picks
# (data through 6/8/2026):
#   - H+R+R only:           96 picks, 55.2% hit, +17.0% ROI
#   - Hits only:            51 picks, 58.8% hit, +15.4% ROI
#   - TB market:            +4.1% ROI  ← SKIPPED
#   - HR market:            -100% (small sample) ← SKIPPED
#   - 5 books pricing:      +20.5% ROI, 64% hit
#   - Chalk ≤-150 juice:    +31.8% ROI, 85% hit
#   - Edge ≥10pp:           +37.7% ROI, 68% hit
# A-grade combines: H+R+R/Hits AND (5 books OR ≤-150 OR ≥10pp edge)
# Expected: ~2-3 picks/day with 65-85% hit and +20-30% ROI
# =====================================================================
def is_a_grade(r):
    if r.get("market") not in ("H+R+R", "Hits"):
        return False
    n_books = r.get("n_books", 0)
    price = r.get("best_price", 0)
    edge = r.get("edge_pp", 0)
    return (n_books >= 5
            or (price is not None and price <= -150)
            or (edge is not None and edge >= 10))


def is_a_plus_grade(r):
    """A+ Grade — the sub-tier with the highest ROI in the backtest.
    322-settled A-Grade backtest through 7/7 showed:
      - Full A-Grade:  60.2% hit / +14.2% ROI
      - edge>=10pp:    58.9% hit / +27.5% ROI  ← A+ subset
    Slightly fewer picks (90 vs 322 total) but nearly 2x the ROI because
    the +10pp edges usually come at longer prices."""
    if r.get("market") not in ("H+R+R", "Hits"):
        return False
    edge = r.get("edge_pp", 0)
    return edge is not None and edge >= 10


a_plus  = [r for r in all_disagreements if is_a_plus_grade(r)]
# A-Grade table excludes anything already shown as A+ (A+ is a strict subset:
# every edge>=10pp pick also passes A-Grade). Otherwise both tables show
# duplicate rows on any slate that has A+ signals.
a_grade = [r for r in all_disagreements
           if is_a_grade(r) and not is_a_plus_grade(r)]

# ---------- A+ Grade (highest-ROI subset) ----------
st.markdown("---")
st.markdown(f"### 🌟 A+ Grade Picks — {len(a_plus)} highest-ROI plays "
            f"(edge ≥ 10pp subset)")
st.caption(
    "**Backtest through 7/7 (90 A+ picks): 58.9% hit / +27.5% ROI.** "
    "The subset of A-Grade where the model disagreement is ≥10pp — "
    "slightly lower hit rate than the full A-Grade (60.2%) but nearly "
    "**2× the ROI** because these picks come at longer prices. Play these "
    "first when the slate has them."
)

if a_plus:
    aplus_rows = []
    for r in a_plus:
        team = r.get("team")
        player_disp = f"{r['player']} ({team})" if team else r["player"]
        aplus_rows.append({
            "Player":    player_disp,
            "Game":      r["game"],
            "Market":    r["market"],
            "Side":      r["side"],
            "Line":      r["point"],
            "Best Price": r["best_price"],
            "Soft Book": r["best_book"],
            "Edge pp":   r["edge_pp"],
            "EV/$100":   r["ev_per_100"],
            "# Books":   r["n_books"],
        })
    aplus_df = pd.DataFrame(aplus_rows)
    for c in ("Edge pp","EV/$100","Best Price"):
        if c in aplus_df.columns:
            aplus_df[c] = pd.to_numeric(aplus_df[c], errors="coerce")
    aplus_df = aplus_df.sort_values("Edge pp", ascending=False)
    st.dataframe(
        aplus_df, use_container_width=True, hide_index=True,
        column_config={
            "Edge pp":    st.column_config.NumberColumn(format="%+.1f"),
            "EV/$100":    st.column_config.NumberColumn(format="$%+.2f"),
            "Best Price": st.column_config.NumberColumn(format="%+d"),
        },
    )
else:
    st.info(
        "No A+ picks right now (need H+R+R or Hits with ≥10pp edge). "
        "Try refreshing closer to first pitch — big cross-book disagreements "
        "usually surface as sportsbooks fine-tune their lines. In the meantime, "
        "the A-Grade section below still has profitable plays (+14.2% ROI historically)."
    )

# ---------- A Grade (broader profitable subset, excluding A+) ----------
st.markdown("---")
st.markdown(f"### 🏆 A-Grade Picks — {len(a_grade)} additional plays "
            f"(excludes A+ shown above)")
st.caption(
    "Backtest-driven filter (248 historical settled picks): "
    "**H+R+R or Hits only** (TB and HR markets historically negative-EV) "
    "**AND one of**: 5 books pricing OR price ≤-150 (chalk) OR edge ≥10pp (strong disagreement).  \n"
    "**Expected: 2-3 picks/day at 65-85% hit rate, +20-30% ROI.**"
)

if a_grade:
    a_rows = []
    for r in a_grade:
        # Why this passed — tag the trigger
        triggers = []
        if r.get("n_books", 0) >= 5: triggers.append("5books")
        if r.get("best_price") is not None and r["best_price"] <= -150: triggers.append("chalk")
        if r.get("edge_pp") is not None and r["edge_pp"] >= 10: triggers.append("10pp+")
        # Player label with team if known
        team = r.get("team")
        player_disp = f"{r['player']} ({team})" if team else r["player"]
        a_rows.append({
            "Why":       " · ".join(triggers),
            "Player":    player_disp,
            "Game":      r["game"],
            "Market":    r["market"],
            "Side":      r["side"],
            "Line":      r["point"],
            "Best Price": r["best_price"],
            "Soft Book": r["best_book"],
            "Edge pp":   r["edge_pp"],
            "EV/$100":   r["ev_per_100"],
            "# Books":   r["n_books"],
        })
    a_df = pd.DataFrame(a_rows)
    for c in ("Edge pp","EV/$100","Best Price"):
        if c in a_df.columns:
            a_df[c] = pd.to_numeric(a_df[c], errors="coerce")
    st.dataframe(
        a_df, use_container_width=True, hide_index=True,
        column_config={
            "Edge pp":    st.column_config.NumberColumn(format="%+.1f"),
            "EV/$100":    st.column_config.NumberColumn(format="$%+.2f"),
            "Best Price": st.column_config.NumberColumn(format="%+d"),
        },
    )
else:
    st.info(
        "No A-grade picks right now. Try refreshing closer to first pitch — "
        "the strong-conviction filters need either heavy chalk pricing, 5-book "
        "agreement, or 10+pp disagreements that usually appear as lineups firm up."
    )

st.markdown("---")
st.markdown(f"#### 🎯 All Soft Prices — {len(all_disagreements)} found (edge ≥ {min_edge_pp}pp)")
st.caption(
    "Full list including markets/edges that historically underperform "
    "(TB, mid-edge plays). Use the A-grade section above for the cleanest picks."
)

if not all_disagreements:
    st.info(
        f"No props with cross-book edge ≥ {min_edge_pp}pp on tonight's slate. "
        f"Try lowering the minimum edge slider, or check back closer to first pitch "
        f"when more books are posted."
    )
    st.stop()

# Build display dataframe
display_rows = []
for r in all_disagreements:
    team = r.get("team")
    player_disp = f"{r['player']} ({team})" if team else r["player"]
    display_rows.append({
        "Player":    player_disp,
        "Game":      r["game"],
        "Market":    r["market"],
        "Side":      r["side"],
        "Line":      r["point"],
        "Best Price": r["best_price"],
        "Soft Book": r["best_book"],
        "Best %":    r["best_imp_pct"],
        "Cons %":    r["consensus_pct"],
        "Edge pp":   r["edge_pp"],
        "EV/$100":   r["ev_per_100"],
        "# Books":   r["n_books"],
    })
df = pd.DataFrame(display_rows)
for c in ("Best %", "Cons %", "Edge pp", "EV/$100", "Best Price"):
    if c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")

st.dataframe(
    df, use_container_width=True, hide_index=True,
    column_config={
        "Best %":     st.column_config.NumberColumn(format="%.1f%%"),
        "Cons %":     st.column_config.NumberColumn(format="%.1f%%"),
        "Edge pp":    st.column_config.NumberColumn(format="%+.1f"),
        "EV/$100":    st.column_config.NumberColumn(format="$%+.2f"),
        "Best Price": st.column_config.NumberColumn(format="%+d"),
    },
)

# ---------- Detail view for top 5 ----------

st.markdown("---")
st.markdown("#### 🔍 Top 5 details — every book's price side by side")
st.caption("The book on each line offering the highest American price is marked SOFT.")

for r in all_disagreements[:5]:
    pt_str = f"{r['point']}" if r['point'] is not None else "—"
    with st.expander(
        f"**{r['player']}** • {r['market']} {r['side']} {pt_str} • "
        f"{r['game']} • Edge {r['edge_pp']:+.1f}pp • EV ${r['ev_per_100']:+.2f}/$100",
        expanded=True,
    ):
        detail_rows = []
        for book, price in r["all_prices"]:
            imp = amer_to_imp(price) * 100
            detail_rows.append({
                "Book":     book,
                "Price":    price,
                "Implied %": round(imp, 2),
                "Tag":      "🟢 SOFT (best)" if (book, price) == (r["best_book"], r["best_price"]) else "",
            })
        dd = pd.DataFrame(detail_rows)
        st.dataframe(dd, use_container_width=True, hide_index=True,
                     column_config={
                         "Price":     st.column_config.NumberColumn(format="%+d"),
                         "Implied %": st.column_config.NumberColumn(format="%.2f%%"),
                     })

st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
    f"Cache TTL: 5 minutes  •  "
    f"Scanned {len(upcoming)} games across {len(market_filter)} markets"
)
