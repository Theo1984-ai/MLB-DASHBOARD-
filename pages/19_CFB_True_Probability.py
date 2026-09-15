"""
🏈🎯 CFB True Probability — All CFB markets filtered to 75%+ consensus true probability.

Exact mirror of pages/6_True_Probability.py for americanfootball_ncaaf.
Scans every market on every game this week: props (Pass Yds, Rush Yds, Rec Yds, TDs),
alternate spreads/totals, mainline spreads/totals, and moneylines. Shows only
plays where the consensus of 5 sharp books implies 75%+ true probability.
"""
import os
import sys
import json
import ssl as _ssl_compat
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

st.set_page_config(page_title="CFB True Probability", page_icon="🏈", layout="wide")
st.title("🏈🎯 CFB True Probability — 75%+ Plays")
st.caption(
    "All markets, all games — filtered to only show plays where the **consensus of 5 "
    "sharp books** implies a 75%+ true probability of hitting. Covers player props "
    "(Pass Yds, Rush Yds, Rec Yds, TDs), alternate spreads / totals, mainline "
    "spreads / totals, and moneylines.  \n"
    "**No price cap** — every play that clears the 75% probability filter is shown, "
    "including heavy chalk."
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
    st.error("Missing `THE_ODDS_API_KEY` in secrets.")
    st.stop()


SPORT = "americanfootball_ncaaf"
SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada,pinnacle"

MIN_TRUE_PROB = 0.75
MAX_PRICE_CAP = None
MIN_PRICE_CAP = None
MIN_BOOKS = 4

PROP_MARKETS = (
    "player_pass_yards,player_pass_tds,"
    "player_rush_yards,player_rush_attempts,"
    "player_reception_yards,player_receptions,"
    "player_anytime_td,player_first_td"
)

GAME_LINE_MARKETS = "h2h,spreads,totals"
ALT_MARKETS = "alternate_spreads,alternate_totals"

MARKET_LABELS = {
    "player_pass_yards":      "Pass Yds",
    "player_pass_tds":        "Pass TDs",
    "player_rush_yards":      "Rush Yds",
    "player_rush_attempts":   "Rush Att",
    "player_reception_yards": "Rec Yds",
    "player_receptions":      "Receptions",
    "player_anytime_td":      "Anytime TD",
    "player_first_td":        "First TD",
    "h2h":                    "Moneyline",
    "spreads":                "Spread",
    "totals":                 "Total",
    "alternate_spreads":      "Spread alt",
    "alternate_totals":       "Total alt",
}


def amer_to_imp(am):
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def ev_per_100(am, p):
    if am > 0:
        return p * am - (1 - p) * 100
    return p * 100 - (1 - p) * abs(am)


@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_events():
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/events?apiKey={ODDS_KEY}"
    return json.loads(urllib.request.urlopen(url, timeout=15, context=_UNVERIFIED_SSL).read())


@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_game_lines():
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets={GAME_LINE_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_UNVERIFIED_SSL).read())
    except Exception:
        return []


@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_event_props(event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{event_id}/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets={PROP_MARKETS}"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_UNVERIFIED_SSL).read())
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def cached_fetch_event_alts(event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{event_id}/odds"
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


cc1, cc2, cc3 = st.columns([1, 1, 3])
with cc1:
    refresh_btn = st.button("🔄 Refresh Now", type="primary", use_container_width=True,
                            help="Clear cache and pull fresh data from the Odds API")
with cc2:
    show_alts = st.toggle("Include alternates", value=True,
                          help="Alternate lines (alt spreads/totals).")
with cc3:
    st.caption("Cache refreshes every 5 min automatically. Hit Refresh to force.")

if refresh_btn:
    clear_all_caches()
    st.toast("Cache cleared. Pulling fresh CFB data...", icon="🔄")


with st.spinner("Fetching upcoming CFB games..."):
    try:
        events = cached_fetch_events()
    except Exception as e:
        st.error(f"Failed to fetch events: {e}")
        st.info("This is usually a temporary API issue. Hit **🔄 Refresh Now** to retry.")
        st.stop()

if not isinstance(events, list):
    st.error(
        "The Odds API returned an unexpected response. This usually means the API "
        "key is invalid or daily quota is exhausted."
    )
    st.stop()

now_utc = datetime.now(timezone.utc)
upcoming = []
for e in events:
    try:
        ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        continue
    hours_to_start = (ct - now_utc).total_seconds() / 3600
    if hours_to_start > -0.5:
        upcoming.append((e, hours_to_start))
upcoming.sort(key=lambda x: x[1])

if not upcoming:
    st.warning(
        "No upcoming CFB games on the board. This is normal between game days, "
        "or very early before any games are scheduled. Try again later."
    )
    st.stop()

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

st.markdown(f"### Scanning **{len(upcoming)}** upcoming CFB games")


all_plays = []

SPREAD_MARKETS = {"spreads", "alternate_spreads"}
TOTAL_MARKETS = {"totals", "alternate_totals"}


def format_selection(market_key, selection, side, point):
    if market_key in SPREAD_MARKETS:
        if point is None:
            return selection or side
        sign = "+" if point > 0 else ""
        return f"{selection} {sign}{point}"
    if market_key in TOTAL_MARKETS:
        if point is None:
            return f"{side}"
        return f"{side} {point}"
    return selection or side


def process_outcomes(market_key, outcomes_by_key, game_label, first_pitch, hrs_to_start):
    for (side, point, player), book_prices in outcomes_by_key.items():
        if len(book_prices) < MIN_BOOKS:
            continue
        best_book, best_price = max(book_prices, key=lambda x: x[1])
        if MAX_PRICE_CAP is not None and best_price > MAX_PRICE_CAP:
            continue
        if MIN_PRICE_CAP is not None and best_price < MIN_PRICE_CAP:
            continue
        imps = [amer_to_imp(pr) for _, pr in book_prices]
        consensus = sum(imps) / len(imps)
        if consensus < MIN_TRUE_PROB:
            continue
        ev = ev_per_100(best_price, consensus)
        all_plays.append({
            "Kickoff":     first_pitch,
            "Hours":       round(hrs_to_start, 1),
            "Game":        game_label,
            "Market":      MARKET_LABELS.get(market_key, market_key),
            "Selection":   format_selection(market_key, player, side, point),
            "Side":        side,
            "Line":        point,
            "Best Book":   best_book,
            "Best Price":  best_price,
            "True Prob %": round(consensus * 100, 2),
            "EV/$100":     round(ev, 2),
            "# Books":     len(book_prices),
        })


# 1) Game lines — single call covers all games
with st.spinner("Pulling game lines (moneyline / spread / total)..."):
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
                if mk == "h2h":
                    side = "Home" if name == home_team else ("Away" if name == away_team else name)
                    selection = name
                elif mk == "spreads":
                    side = "Home" if name == home_team else ("Away" if name == away_team else name)
                    selection = name
                elif mk == "totals":
                    side = name
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
total_events = max(1, len(upcoming))
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
    prog = st.progress(0.0, text="Pulling alternate spreads / totals...")
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


st.markdown("---")

if not all_plays:
    st.warning(
        f"No plays at {int(MIN_TRUE_PROB*100)}%+ true probability right now. "
        "This is normal earlier in the week before all sharp books post lines. "
        "CFB lines typically post Monday-Tuesday. Try refreshing later."
    )
    st.stop()

all_plays.sort(key=lambda r: (-r["True Prob %"], -r["EV/$100"]))

df = pd.DataFrame(all_plays)
for c in ("True Prob %", "EV/$100", "Best Price", "Line", "Hours"):
    if c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")

TAB_PROPS = ["Pass Yds", "Pass TDs", "Rush Yds", "Rush Att",
             "Rec Yds", "Receptions", "Anytime TD", "First TD"]
TAB_GAME  = ["Moneyline", "Spread", "Total", "Spread alt", "Total alt"]

props_df = df[df["Market"].isin(TAB_PROPS)].copy()
games_df = df[df["Market"].isin(TAB_GAME)].copy()

st.markdown(f"### 🎯 {len(df)} plays at {int(MIN_TRUE_PROB*100)}%+ true probability")

t1, t2, t3 = st.tabs([
    f"📋 All ({len(df)})",
    f"🏈 Player Props ({len(props_df)})",
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
        st.info("No qualifying game lines right now.")
    else:
        st.dataframe(games_df, use_container_width=True, hide_index=True, column_config=COL_CFG)


st.markdown("---")
st.markdown("#### 🔍 Top 5 Highest True Probability — every book's price side by side")

for r in all_plays[:5]:
    pt_str = f"{r['Line']}" if r['Line'] is not None else "—"
    with st.expander(
        f"**{r['Selection']}** • {r['Market']} {r['Side']} {pt_str} • "
        f"{r['Game']} • {r['Kickoff']} • TrueProb {r['True Prob %']:.1f}% • EV ${r['EV/$100']:+.2f}/$100",
        expanded=True,
    ):
        st.write(
            f"Best book: **{r['Best Book']}** @ **{r['Best Price']:+d}** • "
            f"{r['# Books']} sharp books pricing this prop"
        )


# ---------- Forward-Test Performance Tracking ----------

st.markdown("---")
st.markdown("### 📈 Forward-Test Performance")
st.caption(
    "Daily snapshots of the 75%+ picks taken at 3 PM ET, then settled the next "
    "morning vs actual results. This is the *real* track record of the True "
    "Probability filter."
)

HISTORY_DIR = os.path.join(ROOT, "cfb_true_prob_history")


@st.cache_data(ttl=600, show_spinner=False)
def load_history():
    if not os.path.isdir(HISTORY_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(HISTORY_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(HISTORY_DIR, fn)) as f:
                d = json.load(f)
                out.append(d)
        except Exception:
            pass
    return out


history = load_history()

if not history:
    st.info(
        "No history snapshots yet. The first snapshot lands at 3 PM ET today; "
        "results settle the next morning."
    )
else:
    all_picks = []
    for snap in history:
        for p in snap.get("picks", []):
            p2 = dict(p)
            p2["snapshot_date"] = snap.get("date")
            all_picks.append(p2)

    settled = [p for p in all_picks if p.get("result") in ("WIN", "LOSS", "PUSH")]
    wins = sum(1 for p in settled if p["result"] == "WIN")
    losses = sum(1 for p in settled if p["result"] == "LOSS")
    pushes = sum(1 for p in settled if p["result"] == "PUSH")
    settled_count = wins + losses
    hit_rate = (wins / settled_count * 100) if settled_count else 0

    risk_total = 0.0
    profit_total = 0.0
    for p in settled:
        if p["result"] == "PUSH":
            continue
        am = p.get("best_price", 0)
        if am > 0:
            risk = 100; payout = am
        else:
            risk = abs(am); payout = 100
        risk_total += risk
        profit_total += payout if p["result"] == "WIN" else -risk
    roi = (profit_total / risk_total * 100) if risk_total else 0

    avg_predicted = (
        sum(p.get("true_prob_pct", 0) for p in settled) / len(settled)
        if settled else 0
    )
    cal_gap = hit_rate - avg_predicted if settled_count else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Picks", f"{len(all_picks)}", f"{settled_count} settled")
    c2.metric("Hit Rate", f"{hit_rate:.1f}%",
              f"vs predicted {avg_predicted:.1f}%" if settled_count else "no data")
    c3.metric("Calibration", f"{cal_gap:+.1f}pp",
              "Good" if abs(cal_gap) < 3 else ("Hot" if cal_gap > 0 else "Cold"))
    c4.metric("Net Profit", f"${profit_total:+.0f}", f"on ${risk_total:.0f}")
    c5.metric("ROI", f"{roi:+.1f}%", "per $ risked")

    st.markdown("#### Day-by-day")
    day_rows = []
    for snap in history:
        s = snap.get("summary") or {}
        if s:
            day_rows.append({
                "Date":    snap.get("date"),
                "Picks":   snap.get("n_picks"),
                "Settled": s.get("n_settled"),
                "W-L-P":   f"{s.get('wins',0)}-{s.get('losses',0)}-{s.get('pushes',0)}",
                "Hit %":   s.get("hit_rate"),
                "Net":     s.get("profit_total"),
                "ROI %":   s.get("roi_pct"),
            })
        else:
            day_rows.append({
                "Date":    snap.get("date"),
                "Picks":   snap.get("n_picks"),
                "Settled": "pending",
                "W-L-P":   "—",
                "Hit %":   None,
                "Net":     None,
                "ROI %":   None,
            })
    day_df = pd.DataFrame(day_rows)
    if not day_df.empty:
        st.dataframe(
            day_df, use_container_width=True, hide_index=True,
            column_config={
                "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
                "Net":    st.column_config.NumberColumn(format="$%+.0f"),
                "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
            },
        )

    if settled_count > 0:
        st.markdown("#### Performance by market")
        from collections import defaultdict as _dd
        by_mkt = _dd(lambda: {"w": 0, "l": 0, "p": 0, "risk": 0.0, "profit": 0.0})
        for p in settled:
            r = p["result"]
            am = p.get("best_price", 0)
            risk = 100 if am > 0 else abs(am)
            payout = am if am > 0 else 100
            mkt = p.get("market", "?")
            if r == "WIN":
                by_mkt[mkt]["w"] += 1
                by_mkt[mkt]["risk"] += risk
                by_mkt[mkt]["profit"] += payout
            elif r == "LOSS":
                by_mkt[mkt]["l"] += 1
                by_mkt[mkt]["risk"] += risk
                by_mkt[mkt]["profit"] -= risk
            else:
                by_mkt[mkt]["p"] += 1
        mkt_rows = []
        for mkt, stats in sorted(by_mkt.items(), key=lambda x: -(x[1]["w"]+x[1]["l"])):
            n = stats["w"] + stats["l"]
            if n == 0:
                continue
            mkt_rows.append({
                "Market":  mkt,
                "W-L":     f"{stats['w']}-{stats['l']}",
                "Hit %":   round(stats["w"] / n * 100, 1),
                "Net":     round(stats["profit"], 2),
                "ROI %":   round(stats["profit"] / stats["risk"] * 100, 1) if stats["risk"] else 0,
            })
        if mkt_rows:
            mkt_df = pd.DataFrame(mkt_rows)
            st.dataframe(
                mkt_df, use_container_width=True, hide_index=True,
                column_config={
                    "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
                    "Net":    st.column_config.NumberColumn(format="$%+.0f"),
                    "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
                },
            )

st.markdown("---")
price_band_str = "no price cap"
if MIN_PRICE_CAP is not None or MAX_PRICE_CAP is not None:
    lo = MIN_PRICE_CAP if MIN_PRICE_CAP is not None else "-inf"
    hi = MAX_PRICE_CAP if MAX_PRICE_CAP is not None else "+inf"
    price_band_str = f"price band: {lo} to {hi}"
st.caption(
    f"Last refresh: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
    f"{len(upcoming)} games scanned  •  "
    f"Min books: {MIN_BOOKS}  •  Min true prob: {int(MIN_TRUE_PROB*100)}%  •  "
    f"{price_band_str}  •  "
    f"Cache TTL: 5 min (or hit Refresh)"
)
