"""
NBA Soft-Price Scanner — cross-book disagreement scanner for NBA props.

Same logic as the MLB scanner, but for the NBA. Pulls every prop from
DraftKings / FanDuel / BetMGM / Caesars and finds props where one book
is significantly out of line with the consensus of the others.

This is the exact methodology that identified the Donovan Mitchell
Over 3.5 Assists @ +121 DK pick earlier tonight (7pp DK softer than
the other sharp books — paid off in Q2 when he got his 4th assist).

Markets scanned:
  - PTS          — player_points + alternate
  - REB          — player_rebounds + alternate
  - AST          — player_assists + alternate
  - 3PM          — player_threes + alternate
  - PRA          — player_points_rebounds_assists + alternate
  - PR           — player_points_rebounds
  - RA           — player_rebounds_assists
  - PA           — player_points_assists

NBA has way better multi-book coverage than MLB — usually 2-3 sharp
books pricing every popular prop. Scanner works any time the slate is
posted (not the 4-8h-before-tip limit MLB has).

Quota guard: results cached for 5 minutes.
"""
import os
import sys
import json
import ssl as _ssl_compat
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd

_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="NBA Soft Scanner", page_icon="🏀", layout="wide")
st.title("🏀 NBA Soft-Price Scanner")
st.caption(
    "Pulls every NBA prop from DraftKings / FanDuel / BetMGM / Caesars and finds "
    "props where one book is significantly out of line with the others. **This is the "
    "scanner that found Donovan Mitchell Over 3.5 ast +121 DK.** No NBA model needed.  \n"
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
# pricing is sharp consensus — we exclude offshore books with known stale
# longshot lines.
SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada"
SPORT_KEY = "basketball_nba"

# Hard price cap — picks outside this range are excluded.
# Wide longshots (+400/+500/+600) historically lose money even when EV looks
# positive on paper because variance overwhelms the edge. Mirrors the Tier A
# scanner cap.
MAX_PRICE_CAP = 300
MIN_PRICE_CAP = -300

MARKET_GROUPS = {
    "PTS":  ["player_points", "player_points_alternate"],
    "REB":  ["player_rebounds", "player_rebounds_alternate"],
    "AST":  ["player_assists", "player_assists_alternate"],
    "3PM":  ["player_threes", "player_threes_alternate"],
    "PRA":  ["player_points_rebounds_assists", "player_points_rebounds_assists_alternate"],
    "PR":   ["player_points_rebounds"],
    "RA":   ["player_rebounds_assists"],
    "PA":   ["player_points_assists"],
}
MARKET_LABELS = {
    "player_points":                              "PTS",
    "player_points_alternate":                    "PTS",
    "player_rebounds":                            "REB",
    "player_rebounds_alternate":                  "REB",
    "player_assists":                             "AST",
    "player_assists_alternate":                   "AST",
    "player_threes":                              "3PM",
    "player_threes_alternate":                    "3PM",
    "player_points_rebounds_assists":             "PRA",
    "player_points_rebounds_assists_alternate":   "PRA",
    "player_points_rebounds":                     "PR",
    "player_rebounds_assists":                    "RA",
    "player_points_assists":                      "PA",
}


def amer_to_imp(am):
    if am > 0: return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


# ---------- Cached API calls (quota protection) ----------

@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_events():
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/events?apiKey={ODDS_KEY}"
    return json.loads(urllib.request.urlopen(url, timeout=15, context=_UNVERIFIED_SSL).read())


@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_event_odds(event_id, markets_tuple):
    markets = ",".join(markets_tuple)
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/events/{event_id}/odds"
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
    """Returns {(market, player, side, point): [(book, american)]}."""
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

cc1, cc2, cc3, cc4 = st.columns([1.8, 1.4, 1.4, 1])
with cc1:
    sel_markets = st.multiselect(
        "Markets to scan",
        options=list(MARKET_GROUPS.keys()),
        default=["PTS", "REB", "AST", "3PM"],
        help="Pick which prop markets to include. Fewer = faster + less Odds API quota.",
    )
with cc2:
    min_edge_pp = st.slider("Minimum edge (pp)", 1.0, 15.0, 5.0, 0.5,
                            help="Only show props where best book is N+ pp better than worst",
                            key="nba_min_edge")
with cc3:
    min_implied = st.slider("Min consensus implied %", 0, 80, 0, 5,
                            help="Skip longshots — only show props with at least this implied probability",
                            key="nba_min_imp")
with cc4:
    min_books = st.selectbox("Min books", [2, 3, 4], index=0,
                             help="Require this many books to be pricing the prop",
                             key="nba_min_books")

cs1, cs2 = st.columns([1, 5])
with cs1:
    scan_btn = st.button("🏀 Run scan", type="primary", use_container_width=True)
with cs2:
    if scan_btn:
        st.caption("Scanning... results cached 5 min — repeat clicks won't burn quota.")
    else:
        st.caption("Click **Run scan**. Results cached 5 minutes (quota guard).")

if not scan_btn:
    st.info(
        "💡 **NBA has way better multi-book coverage than MLB.** Usually 2-3 sharp books "
        "price every popular prop right after lineups drop (~2-3h before tip). "
        "Run anytime — props typically stay posted up until tipoff."
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

with st.spinner("Fetching NBA events..."):
    try:
        events = cached_fetch_events()
    except Exception as e:
        st.error(f"Failed to fetch NBA events: {e}")
        st.stop()

now = datetime.now(timezone.utc)
upcoming = []
for e in events:
    ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
    hours_to_start = (ct - now).total_seconds() / 3600
    if hours_to_start > -3.0:   # include in-progress (NBA games can run 2.5h)
        upcoming.append((e, hours_to_start))
upcoming.sort(key=lambda x: x[1])

if not upcoming:
    st.warning("No upcoming NBA games on the board.")
    st.stop()

st.markdown(f"### Found **{len(upcoming)}** NBA game(s)")

coverage = []
all_disagreements = []
progress = st.progress(0)
for gi, (e, dh) in enumerate(upcoming):
    progress.progress((gi + 1) / max(1, len(upcoming)))
    away = e['away_team'].split()[-1]
    home = e['home_team'].split()[-1]
    game_label = f"{away} @ {home}"
    data = cached_fetch_event_odds(e["id"], markets_tuple)
    books = data.get("bookmakers", [])
    offers = collect_offers(books, market_filter)
    n_props = len(offers)
    n_multibook = sum(1 for v in offers.values() if len(v) >= min_books)
    n_books_present = len({b for v in offers.values() for b, _ in v})
    coverage.append({
        "Game":      game_label,
        "Start":     f"+{dh:.1f}h" if dh >= 0 else f"{dh:.1f}h (in-progress)",
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
        "⚠ **No multi-book coverage on any game right now.** "
        "Try widening the market selection or lowering the Min Books requirement."
    )
    st.stop()


# ---------- Top soft prices ----------

all_disagreements.sort(key=lambda r: (-r["edge_pp"], -r["ev_per_100"]))

st.markdown("---")
st.markdown(f"#### 🎯 Soft Prices — {len(all_disagreements)} found (edge ≥ {min_edge_pp}pp)")

if not all_disagreements:
    st.info(
        f"No props with cross-book edge ≥ {min_edge_pp}pp on the slate. "
        f"Try lowering the minimum edge slider (e.g. 3pp) to see smaller gaps."
    )
    st.stop()

display_rows = []
for r in all_disagreements:
    display_rows.append({
        "Player":     r["player"],
        "Game":       r["game"],
        "Market":     r["market"],
        "Side":       r["side"],
        "Line":       r["point"],
        "Best Price": r["best_price"],
        "Soft Book":  r["best_book"],
        "Best %":     r["best_imp_pct"],
        "Cons %":     r["consensus_pct"],
        "Edge pp":    r["edge_pp"],
        "EV/$100":    r["ev_per_100"],
        "# Books":    r["n_books"],
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
st.caption("The book offering the highest American price on each prop is marked SOFT.")

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
