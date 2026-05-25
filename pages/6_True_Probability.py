"""
True Probability — All MLB markets filtered to 75%+ consensus true probability.

Scans every market on every game today: props (HR, Hits, TB, H+R+R, K's, etc.),
alternate spreads/totals, mainline spreads/totals, and moneylines. Shows only
plays where the consensus of 5 sharp books implies 75%+ true probability.

Refresh button clears the cache and forces a fresh API pull.
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
EASTERN = ZoneInfo("America/New_York")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

st.set_page_config(page_title="True Probability", page_icon="🎯", layout="wide")
st.title("🎯 True Probability — 75%+ Plays")
st.caption(
    "All markets, all games — filtered to only show plays where the **consensus of 5 "
    "sharp books** implies a 75%+ true probability of hitting. Covers batter props "
    "(HR, Hits, TB, H+R+R), pitcher props (K's), alternate spreads / totals, mainline "
    "run lines / totals, and moneylines.  \n"
    "Price band capped at **−300 to +300** to keep juice manageable."
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

SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada"

# 75%+ true probability is the locked threshold for this page.
MIN_TRUE_PROB = 0.75

# Hard price cap — same as Tonight's Picks + Soft Scanners.
MAX_PRICE_CAP = 300
MIN_PRICE_CAP = -300

# Minimum sharp books required to trust the consensus.
MIN_BOOKS = 4

# Prop markets (one fetch per event).
PROP_MARKETS = (
    "batter_home_runs,batter_home_runs_alternate,"
    "batter_hits,batter_hits_alternate,"
    "batter_total_bases,batter_total_bases_alternate,"
    "batter_hits_runs_rbis_alternate,"
    "batter_rbis,batter_rbis_alternate,"
    "batter_runs_scored,batter_runs_scored_alternate,"
    "batter_walks,"
    "pitcher_strikeouts,pitcher_strikeouts_alternate,"
    "pitcher_record_a_win"
)

# Game-line markets (one fetch covers all games).
GAME_LINE_MARKETS = "h2h,spreads,totals"

# Alternate game lines (per event).
ALT_MARKETS = "alternate_spreads,alternate_totals"

# Friendly labels.
MARKET_LABELS = {
    "batter_home_runs":                "HR",
    "batter_home_runs_alternate":      "HR alt",
    "batter_hits":                     "Hits",
    "batter_hits_alternate":           "Hits alt",
    "batter_total_bases":              "TB",
    "batter_total_bases_alternate":    "TB alt",
    "batter_hits_runs_rbis_alternate": "H+R+R",
    "batter_rbis":                     "RBIs",
    "batter_rbis_alternate":           "RBIs alt",
    "batter_runs_scored":              "Runs",
    "batter_runs_scored_alternate":    "Runs alt",
    "batter_walks":                    "Walks",
    "pitcher_strikeouts":              "K's",
    "pitcher_strikeouts_alternate":    "K's alt",
    "pitcher_record_a_win":            "Pitcher W",
    "h2h":                             "Moneyline",
    "spreads":                         "Run Line",
    "totals":                          "Total",
    "alternate_spreads":               "Run Line alt",
    "alternate_totals":                "Total alt",
}


def amer_to_imp(am):
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def ev_per_100(am, p):
    if am > 0:
        return p * am - (1 - p) * 100
    return p * 100 - (1 - p) * abs(am)


# ---------- Cached API calls ----------

@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_events():
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={ODDS_KEY}"
    return json.loads(urllib.request.urlopen(url, timeout=15, context=_UNVERIFIED_SSL).read())


@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_game_lines():
    """All games' h2h/spreads/totals in one call."""
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets={GAME_LINE_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_UNVERIFIED_SSL).read())
    except Exception:
        return []


@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_event_props(event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets={PROP_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_UNVERIFIED_SSL).read())
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_event_alts(event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets={ALT_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_UNVERIFIED_SSL).read())
    except Exception:
        return {}


def clear_all_caches():
    cached_fetch_events.clear()
    cached_fetch_game_lines.clear()
    cached_fetch_event_props.clear()
    cached_fetch_event_alts.clear()


# ---------- Refresh button ----------

cc1, cc2, cc3 = st.columns([1, 1, 3])
with cc1:
    refresh_btn = st.button("🔄 Refresh Now", type="primary", use_container_width=True,
                            help="Clear cache and pull fresh data from the Odds API")
with cc2:
    show_alts = st.toggle("Include alternates", value=True,
                          help="Alternate lines (e.g. Over 1.5 Hits, alt run lines).")
with cc3:
    st.caption("Cache refreshes every 5 min automatically. Hit Refresh to force.")

if refresh_btn:
    clear_all_caches()
    st.toast("Cache cleared. Pulling fresh data...", icon="🔄")


# ---------- Pull data ----------

with st.spinner("Fetching upcoming MLB games..."):
    try:
        events = cached_fetch_events()
    except Exception as e:
        st.error(f"Failed to fetch events: {e}")
        st.stop()

now_utc = datetime.now(timezone.utc)
upcoming = []
for e in events:
    ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
    hours_to_start = (ct - now_utc).total_seconds() / 3600
    if hours_to_start > -0.5:  # exclude games already 30+ min in
        upcoming.append((e, hours_to_start))
upcoming.sort(key=lambda x: x[1])

if not upcoming:
    st.warning("No upcoming MLB games on the board.")
    st.stop()

# Game labels by event id.
event_meta = {}
for e, hrs in upcoming:
    away = e["away_team"].split()[-1]
    home = e["home_team"].split()[-1]
    ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")).astimezone(EASTERN)
    fp = ct.strftime("%#I:%M %p ET") if sys.platform == "win32" else ct.strftime("%-I:%M %p ET")
    event_meta[e["id"]] = {
        "label": f"{away} @ {home}",
        "first_pitch": fp,
        "hours_to_start": hrs,
        "away_team": e["away_team"],
        "home_team": e["home_team"],
    }

st.markdown(f"### Scanning **{len(upcoming)}** upcoming MLB games")


# ---------- Process all markets ----------

all_plays = []

def process_outcomes(market_key, outcomes_by_key, game_label, first_pitch, hrs_to_start):
    """outcomes_by_key: {(side, point, player): [(book, price)]}."""
    for (side, point, player), book_prices in outcomes_by_key.items():
        if len(book_prices) < MIN_BOOKS:
            continue
        best_book, best_price = max(book_prices, key=lambda x: x[1])
        if best_price > MAX_PRICE_CAP or best_price < MIN_PRICE_CAP:
            continue
        imps = [amer_to_imp(pr) for _, pr in book_prices]
        consensus = sum(imps) / len(imps)
        if consensus < MIN_TRUE_PROB:
            continue
        ev = ev_per_100(best_price, consensus)
        all_plays.append({
            "First Pitch": first_pitch,
            "Hours":       round(hrs_to_start, 1),
            "Game":        game_label,
            "Market":      MARKET_LABELS.get(market_key, market_key),
            "Selection":   player or side,
            "Side":        side,
            "Line":        point,
            "Best Book":   best_book,
            "Best Price":  best_price,
            "True Prob %": round(consensus * 100, 2),
            "EV/$100":     round(ev, 2),
            "# Books":     len(book_prices),
        })


# 1) Game lines (h2h, spreads, totals) — single call covers all games
with st.spinner("Pulling game lines (moneyline / run line / total)..."):
    game_lines = cached_fetch_game_lines()

for g in game_lines:
    eid = g.get("id")
    if eid not in event_meta:
        continue
    meta = event_meta[eid]
    away_team = meta["away_team"]
    home_team = meta["home_team"]
    by_market = defaultdict(lambda: defaultdict(list))
    for b in g.get("bookmakers", []):
        for m in b.get("markets", []):
            mk = m["key"]
            for o in m.get("outcomes", []):
                name = (o.get("name") or "").strip()
                point = o.get("point")
                price = o.get("price")
                if price is None:
                    continue
                # For h2h: side = team name
                # For spreads/totals: side = "Over"/"Under" or team name
                if mk == "h2h":
                    side = "Home" if name == home_team else ("Away" if name == away_team else name)
                    selection = name  # the team
                elif mk == "spreads":
                    side = "Home" if name == home_team else ("Away" if name == away_team else name)
                    selection = name
                elif mk == "totals":
                    side = name  # Over / Under
                    selection = "Game Total"
                else:
                    side = name
                    selection = name
                key = (side, point, selection)
                by_market[mk][key].append((b["key"], int(price)))
    for mk, outcomes_by_key in by_market.items():
        process_outcomes(mk, outcomes_by_key, meta["label"], meta["first_pitch"], meta["hours_to_start"])


# 2) Props per event
prog = st.progress(0.0, text="Pulling props per game...")
total_events = len(upcoming)
for idx, (e, hrs) in enumerate(upcoming):
    prog.progress((idx + 1) / total_events, text=f"Pulling props: {idx+1}/{total_events}")
    eid = e["id"]
    meta = event_meta[eid]
    data = cached_fetch_event_props(eid)
    by_market = defaultdict(lambda: defaultdict(list))
    for b in data.get("bookmakers", []):
        for m in b.get("markets", []):
            mk = m["key"]
            for o in m.get("outcomes", []):
                name = (o.get("name") or "").strip()
                player = o.get("description") or name
                if player in ("Over", "Under"):
                    continue
                side = name if name in ("Over", "Under") else "Yes"
                point = o.get("point")
                price = o.get("price")
                if price is None:
                    continue
                key = (side, point, player)
                by_market[mk][key].append((b["key"], int(price)))
    for mk, outcomes_by_key in by_market.items():
        process_outcomes(mk, outcomes_by_key, meta["label"], meta["first_pitch"], meta["hours_to_start"])
prog.empty()


# 3) Alternate spreads/totals per event (optional toggle)
if show_alts:
    prog = st.progress(0.0, text="Pulling alternate run lines / totals...")
    for idx, (e, hrs) in enumerate(upcoming):
        prog.progress((idx + 1) / total_events, text=f"Pulling alts: {idx+1}/{total_events}")
        eid = e["id"]
        meta = event_meta[eid]
        away_team = meta["away_team"]
        home_team = meta["home_team"]
        data = cached_fetch_event_alts(eid)
        by_market = defaultdict(lambda: defaultdict(list))
        for b in data.get("bookmakers", []):
            for m in b.get("markets", []):
                mk = m["key"]
                for o in m.get("outcomes", []):
                    name = (o.get("name") or "").strip()
                    point = o.get("point")
                    price = o.get("price")
                    if price is None:
                        continue
                    if mk == "alternate_spreads":
                        side = "Home" if name == home_team else ("Away" if name == away_team else name)
                        selection = name
                    elif mk == "alternate_totals":
                        side = name
                        selection = "Game Total"
                    else:
                        side = name
                        selection = name
                    key = (side, point, selection)
                    by_market[mk][key].append((b["key"], int(price)))
        for mk, outcomes_by_key in by_market.items():
            process_outcomes(mk, outcomes_by_key, meta["label"], meta["first_pitch"], meta["hours_to_start"])
    prog.empty()


# ---------- Results ----------

st.markdown("---")

if not all_plays:
    st.warning(
        f"No plays at {int(MIN_TRUE_PROB*100)}%+ true probability right now. "
        "This is normal earlier in the day before all sharp books post. "
        "Try refreshing in an hour or two."
    )
    st.stop()

# Sort by true prob desc, then EV desc
all_plays.sort(key=lambda r: (-r["True Prob %"], -r["EV/$100"]))

df = pd.DataFrame(all_plays)
for c in ("True Prob %", "EV/$100", "Best Price", "Line", "Hours"):
    if c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")

# Tabs split by category
TAB_PROPS = ["HR", "HR alt", "Hits", "Hits alt", "TB", "TB alt", "H+R+R",
             "RBIs", "RBIs alt", "Runs", "Runs alt", "Walks",
             "K's", "K's alt", "Pitcher W"]
TAB_GAME = ["Moneyline", "Run Line", "Total", "Run Line alt", "Total alt"]

props_df = df[df["Market"].isin(TAB_PROPS)].copy()
games_df = df[df["Market"].isin(TAB_GAME)].copy()

st.markdown(f"### 🎯 {len(df)} plays at {int(MIN_TRUE_PROB*100)}%+ true probability")

t1, t2, t3 = st.tabs([
    f"📋 All ({len(df)})",
    f"⚾ Player Props ({len(props_df)})",
    f"📊 Game Lines ({len(games_df)})",
])

COL_CFG = {
    "True Prob %": st.column_config.NumberColumn(format="%.1f%%"),
    "EV/$100":     st.column_config.NumberColumn(format="$%+.2f"),
    "Best Price":  st.column_config.NumberColumn(format="%+d"),
    "Line":        st.column_config.NumberColumn(format="%.1f"),
    "Hours":       st.column_config.NumberColumn(format="%.1fh"),
}

with t1:
    st.dataframe(df, use_container_width=True, hide_index=True, column_config=COL_CFG)
with t2:
    if len(props_df) == 0:
        st.info("No qualifying player props right now.")
    else:
        st.dataframe(props_df, use_container_width=True, hide_index=True, column_config=COL_CFG)
with t3:
    if len(games_df) == 0:
        st.info("No qualifying game lines right now. "
                "Most game lines won't clear 75% unless the spread/total is well off the mainline.")
    else:
        st.dataframe(games_df, use_container_width=True, hide_index=True, column_config=COL_CFG)


# ---------- Top 5 details ----------

st.markdown("---")
st.markdown("#### 🔍 Top 5 Highest True Probability — every book's price side by side")

for r in all_plays[:5]:
    pt_str = f"{r['Line']}" if r['Line'] is not None else "—"
    with st.expander(
        f"**{r['Selection']}** • {r['Market']} {r['Side']} {pt_str} • "
        f"{r['Game']} • {r['First Pitch']} • TrueProb {r['True Prob %']:.1f}% • EV ${r['EV/$100']:+.2f}/$100",
        expanded=True,
    ):
        st.write(
            f"Best book: **{r['Best Book']}** @ **{r['Best Price']:+d}** • "
            f"{r['# Books']} sharp books pricing this prop"
        )

st.markdown("---")
st.caption(
    f"Last refresh: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
    f"{len(upcoming)} games scanned  •  "
    f"Min books: {MIN_BOOKS}  •  Price band: {MIN_PRICE_CAP} to +{MAX_PRICE_CAP}  •  "
    f"Cache TTL: 5 min (or hit Refresh)"
)
