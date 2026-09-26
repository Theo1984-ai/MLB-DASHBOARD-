"""
🎯 True Probability — NFL (75%+ consensus plays) + CFB.

Shows only plays where the consensus of 5 sharp books implies 75%+ true probability.
Covers player props, alternate spreads/totals, mainline spreads/totals, and moneylines.
"""
import json
import os
import ssl as _ssl_compat
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.nfl_true_prob_scanner import scan as nfl_scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
_SSL = _ssl_compat._create_unverified_context()

st.set_page_config(page_title="True Probability", page_icon="🎯", layout="wide")
st.title("🎯 True Probability — 75%+ Plays")
st.caption(
    "All markets, all games — filtered to only show plays where the "
    "**consensus of 5 sharp books** implies a 75%+ true probability of hitting.  \n"
    "Covers player props (Pass Yds, Rush Yds, Rec Yds, Receptions, TDs, Kicking Pts), "
    "alternate spreads/totals, mainline spreads/totals, and moneylines."
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
    st.error("Missing `THE_ODDS_API_KEY` in secrets.")
    st.stop()


# ---------- NFL cached scan ----------

@st.cache_data(ttl=300, show_spinner=False)
def _nfl_cached_scan(include_alts):
    return nfl_scan(ODDS_KEY, include_alts=include_alts)


# ---------- CFB cached fetch functions ----------

_CFB_SPORT       = "americanfootball_ncaaf"
_CFB_SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada,pinnacle"
_CFB_PROP_MKTS   = (
    "player_pass_yards,player_pass_tds,"
    "player_rush_yards,player_rush_attempts,"
    "player_reception_yards,player_receptions,"
    "player_anytime_td,player_first_td"
)
_CFB_GAME_MKTS   = "h2h,spreads,totals"
_CFB_ALT_MKTS    = "alternate_spreads,alternate_totals"
_CFB_MARKET_LABELS = {
    "player_pass_yards":"Pass Yds","player_pass_tds":"Pass TDs",
    "player_rush_yards":"Rush Yds","player_rush_attempts":"Rush Att",
    "player_reception_yards":"Rec Yds","player_receptions":"Receptions",
    "player_anytime_td":"Anytime TD","player_first_td":"First TD",
    "h2h":"Moneyline","spreads":"Spread","totals":"Total",
    "alternate_spreads":"Spread alt","alternate_totals":"Total alt",
}
_CFB_MIN_BOOKS = 4
_CFB_MIN_TRUE_PROB = 0.75


@st.cache_data(ttl=300, show_spinner=False)
def _cfb_fetch_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/{_CFB_SPORT}/events?apiKey={api_key}"
    return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())


@st.cache_data(ttl=300, show_spinner=False)
def _cfb_fetch_game_lines(api_key):
    url = (f"https://api.the-odds-api.com/v4/sports/{_CFB_SPORT}/odds"
           f"?apiKey={api_key}&regions=us&markets={_CFB_GAME_MKTS}"
           f"&bookmakers={_CFB_SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception:
        return []


@st.cache_data(ttl=300, show_spinner=False)
def _cfb_fetch_event_props(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/{_CFB_SPORT}/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={_CFB_PROP_MKTS}"
           f"&bookmakers={_CFB_SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception:
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def _cfb_fetch_event_alts(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/{_CFB_SPORT}/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={_CFB_ALT_MKTS}"
           f"&bookmakers={_CFB_SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception:
        return {}


# ---------- Shared display helpers ----------

def _fp_label(iso_str):
    try:
        ct = datetime.fromisoformat(iso_str.replace("Z","+00:00")).astimezone(EASTERN)
        fmt = "%a %#I:%M %p ET" if sys.platform == "win32" else "%a %-I:%M %p ET"
        return ct.strftime(fmt)
    except Exception:
        return iso_str or "?"


PROP_MARKETS = {
    "Pass Yds","Pass TDs","Pass Att","Pass Comp",
    "Rush Yds","Rush Att","Rec Yds","Receptions",
    "Anytime TD","First TD","Kicking Pts",
}
GAME_MARKETS = {"Moneyline","Spread","Total","Spread alt","Total alt"}

COL_CFG = {
    "True Prob %": st.column_config.NumberColumn(format="%.1f%%"),
    "EV/$100":     st.column_config.NumberColumn(format="$%+.2f"),
    "Best Price":  st.column_config.NumberColumn(format="%+d"),
    "Line":        st.column_config.NumberColumn(format="%.1f"),
}


def _show_true_prob_tabs(df, plays, sport_label):
    for c in ("True Prob %","EV/$100","Best Price","Line"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    props_df = df[df["Market"].isin(PROP_MARKETS)].copy()
    games_df = df[df["Market"].isin(GAME_MARKETS)].copy()

    st.markdown(f"### 🎯 {len(df)} plays at 75%+ true probability")

    t1, t2, t3 = st.tabs([
        f"📋 All ({len(df)})",
        f"🏈 Player Props ({len(props_df)})",
        f"📊 Game Lines ({len(games_df)})",
    ])
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
    for p in plays[:5]:
        pt_str = f"{p.get('point',p.get('Line'))}" if p.get('point',p.get('Line')) is not None else "—"
        true_prob = p.get("true_prob_pct", p.get("True Prob %", 0))
        ev        = p.get("ev_per_100",   p.get("EV/$100",   0))
        best_p    = p.get("best_price",   p.get("Best Price", 0))
        best_bk   = p.get("best_book",    p.get("Best Book",  "?"))
        n_bks     = p.get("n_books",      p.get("# Books",    "?"))
        game      = p.get("game",         p.get("Game",       "?"))
        mkt       = p.get("market",       p.get("Market",     "?"))
        sel       = p.get("selection",    p.get("Selection",  "?"))
        side      = p.get("side",         p.get("Side",       ""))
        fp        = p.get("first_pitch",  p.get("Kickoff",    ""))
        with st.expander(
            f"**{sel}** • {mkt} {side} {pt_str} • {game} • {_fp_label(fp) if 'Z' in str(fp) or '+' in str(fp) else fp} • "
            f"TrueProb {true_prob:.1f}% • EV ${ev:+.2f}/$100",
            expanded=True,
        ):
            prices_str = "  ·  ".join(
                f"{bk}: {pr:+d}" for bk, pr in p.get("all_prices", [])
            ) if p.get("all_prices") else ""
            st.write(
                f"Best book: **{best_bk}** @ **{best_p:+d}**  "
                f"({n_bks} sharp books priced this)"
                + (f"  \n{prices_str}" if prices_str else "")
            )


# ═══════════════════════════════════════════════════════════════════
#  NFL render
# ═══════════════════════════════════════════════════════════════════

def _render_nfl_true_prob():
    cc1, cc2, cc3 = st.columns([1, 1, 3])
    with cc1:
        refresh_btn = st.button("🔄 Refresh Now", type="primary", use_container_width=True,
                                key="nfl_tp_refresh")
    with cc2:
        show_alts = st.toggle("Include alt lines", value=True, key="nfl_tp_alts",
                              help="Alternate spreads and totals per event.")
    with cc3:
        st.caption("Cache refreshes every 5 min automatically. Hit Refresh to force.")

    if refresh_btn:
        _nfl_cached_scan.clear()
        st.toast("Cache cleared. Pulling fresh NFL data...", icon="🔄")

    with st.spinner("Scanning NFL sharp books..."):
        try:
            plays = _nfl_cached_scan(include_alts=show_alts)
        except Exception as e:
            st.error(f"Scan failed: {e}")
            st.info("Hit **🔄 Refresh Now** to retry.")
            return

    if not plays:
        st.warning(
            "No NFL plays at 75%+ true probability right now. This is normal "
            "outside the week's game window — check back once lines are posted "
            "(typically Tuesday/Wednesday for the upcoming week)."
        )
        return

    all_rows = []
    for p in plays:
        all_rows.append({
            "Kickoff":     _fp_label(p.get("first_pitch") or ""),
            "Game":        p["game"],
            "Market":      p["market"],
            "Selection":   p["selection"],
            "Side":        p["side"],
            "Line":        p.get("point"),
            "Best Book":   p["best_book"],
            "Best Price":  p["best_price"],
            "True Prob %": p["true_prob_pct"],
            "EV/$100":     p["ev_per_100"],
            "# Books":     p["n_books"],
        })
    df = pd.DataFrame(all_rows)
    _show_true_prob_tabs(df, plays, "NFL")

    st.markdown("---")
    st.caption(
        f"Last refresh: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
        f"Min true prob: 75%  •  Min books: 4  •  Min EV: $3/$100  •  "
        f"Juice cap: -400  •  Cache TTL: 5 min"
    )


# ═══════════════════════════════════════════════════════════════════
#  CFB render (inline implementation — NCAAF sport)
# ═══════════════════════════════════════════════════════════════════

def _amer_to_imp(am):
    return 100.0 / (am + 100.0) if am > 0 else abs(am) / (abs(am) + 100.0)


def _ev_per_100(am, p):
    if am > 0: return p * am - (1 - p) * 100
    return p * 100 - (1 - p) * abs(am)


def _render_cfb_true_prob():
    cc1, cc2, cc3 = st.columns([1, 1, 3])
    with cc1:
        refresh_btn = st.button("🔄 Refresh Now", type="primary", use_container_width=True,
                                key="cfb_tp_refresh",
                                help="Clear cache and pull fresh data from the Odds API")
    with cc2:
        show_alts = st.toggle("Include alternates", value=True, key="cfb_tp_alts",
                              help="Alternate lines (alt spreads/totals).")
    with cc3:
        st.caption("Cache refreshes every 5 min automatically. Hit Refresh to force.")

    if refresh_btn:
        _cfb_fetch_events.clear()
        _cfb_fetch_game_lines.clear()
        _cfb_fetch_event_props.clear()
        _cfb_fetch_event_alts.clear()
        st.toast("Cache cleared. Pulling fresh CFB data...", icon="🔄")

    with st.spinner("Fetching upcoming CFB games..."):
        try:
            events = _cfb_fetch_events(ODDS_KEY)
        except Exception as e:
            st.error(f"Failed to fetch events: {e}")
            return

    if not isinstance(events, list):
        st.error("The Odds API returned an unexpected response — API key invalid or quota exhausted.")
        return

    now_utc = datetime.now(timezone.utc)
    upcoming = []
    for e in events:
        try:
            ct = datetime.fromisoformat(e["commence_time"].replace("Z","+00:00"))
        except (KeyError, ValueError, TypeError):
            continue
        hrs = (ct - now_utc).total_seconds() / 3600
        if hrs > -0.5:
            upcoming.append((e, hrs))
    upcoming.sort(key=lambda x: x[1])

    if not upcoming:
        st.warning("No upcoming CFB games on the board. Try again later.")
        return

    event_meta = {}
    for e, hrs in upcoming:
        away = e["away_team"].split()[-1]; home = e["home_team"].split()[-1]
        ct   = datetime.fromisoformat(e["commence_time"].replace("Z","+00:00")).astimezone(EASTERN)
        fmt  = "%#I:%M %p ET" if sys.platform == "win32" else "%-I:%M %p ET"
        event_meta[e["id"]] = {
            "label":          f"{away} @ {home}",
            "first_pitch":    ct.strftime(fmt),
            "hours_to_start": hrs,
            "away_team":      e["away_team"],
            "home_team":      e["home_team"],
        }

    st.markdown(f"### Scanning **{len(upcoming)}** upcoming CFB games")

    all_plays = []
    SPREAD_MKTS = {"spreads","alternate_spreads"}
    TOTAL_MKTS  = {"totals","alternate_totals"}

    def _fmt_sel(mkt_key, selection, side, point):
        if mkt_key in SPREAD_MKTS:
            if point is None: return selection or side
            sign = "+" if point > 0 else ""
            return f"{selection} {sign}{point}"
        if mkt_key in TOTAL_MKTS:
            return f"{side} {point}" if point is not None else str(side)
        return selection or side

    def _process_outcomes(mkt_key, outcomes_by_key, game_label, fp, hrs):
        for (side, point, player), book_prices in outcomes_by_key.items():
            if len(book_prices) < _CFB_MIN_BOOKS:
                continue
            best_book, best_price = max(book_prices, key=lambda x: x[1])
            imps = [_amer_to_imp(pr) for _, pr in book_prices]
            consensus = sum(imps) / len(imps)
            if consensus < _CFB_MIN_TRUE_PROB:
                continue
            ev = _ev_per_100(best_price, consensus)
            all_plays.append({
                "Kickoff":     fp,
                "Hours":       round(hrs, 1),
                "Game":        game_label,
                "Market":      _CFB_MARKET_LABELS.get(mkt_key, mkt_key),
                "Selection":   _fmt_sel(mkt_key, player, side, point),
                "Side":        side,
                "Line":        point,
                "Best Book":   best_book,
                "Best Price":  best_price,
                "True Prob %": round(consensus * 100, 2),
                "EV/$100":     round(ev, 2),
                "# Books":     len(book_prices),
            })

    # 1) Game lines
    with st.spinner("Pulling CFB game lines (moneyline / spread / total)..."):
        game_lines = _cfb_fetch_game_lines(ODDS_KEY)

    for g in game_lines:
        eid = g.get("id")
        if eid not in event_meta:
            continue
        meta = event_meta[eid]
        by_market = defaultdict(lambda: defaultdict(list))
        for b in g.get("bookmakers", []):
            for m in b.get("markets", []):
                mk = m["key"]
                for o in m.get("outcomes", []):
                    name = (o.get("name") or "").strip()
                    point = o.get("point"); price = o.get("price")
                    if price is None: continue
                    if mk == "h2h":
                        side = "Home" if name == meta["home_team"] else ("Away" if name == meta["away_team"] else name)
                        selection = name
                    elif mk == "spreads":
                        side = "Home" if name == meta["home_team"] else ("Away" if name == meta["away_team"] else name)
                        selection = name
                    elif mk == "totals":
                        side = name; selection = "Game Total"
                    else:
                        side = name; selection = name
                    by_market[mk][(side, point, selection)].append((b["key"], int(price)))
        for mk, obs in by_market.items():
            _process_outcomes(mk, obs, meta["label"], meta["first_pitch"], meta["hours_to_start"])

    # 2) Props per event
    prog = st.progress(0.0, text="Pulling CFB props per game...")
    total = max(1, len(upcoming))
    for idx, (e, hrs) in enumerate(upcoming):
        prog.progress((idx+1)/total, text=f"Pulling props: {idx+1}/{total}")
        eid = e["id"]; meta = event_meta[eid]
        data = _cfb_fetch_event_props(ODDS_KEY, eid)
        by_market = defaultdict(lambda: defaultdict(list))
        for b in data.get("bookmakers", []):
            for m in b.get("markets", []):
                mk = m["key"]
                for o in m.get("outcomes", []):
                    name = (o.get("name") or "").strip()
                    player = o.get("description") or name
                    if player in ("Over","Under"): continue
                    side = name if name in ("Over","Under") else "Yes"
                    point = o.get("point"); price = o.get("price")
                    if price is None: continue
                    by_market[mk][(side, point, player)].append((b["key"], int(price)))
        for mk, obs in by_market.items():
            _process_outcomes(mk, obs, meta["label"], meta["first_pitch"], meta["hours_to_start"])
    prog.empty()

    # 3) Alt lines (optional)
    if show_alts:
        prog = st.progress(0.0, text="Pulling CFB alternate spreads / totals...")
        for idx, (e, hrs) in enumerate(upcoming):
            prog.progress((idx+1)/total, text=f"Pulling alts: {idx+1}/{total}")
            eid = e["id"]; meta = event_meta[eid]
            data = _cfb_fetch_event_alts(ODDS_KEY, eid)
            by_market = defaultdict(lambda: defaultdict(list))
            for b in data.get("bookmakers", []):
                for m in b.get("markets", []):
                    mk = m["key"]
                    for o in m.get("outcomes", []):
                        name = (o.get("name") or "").strip()
                        point = o.get("point"); price = o.get("price")
                        if price is None: continue
                        if mk == "alternate_spreads":
                            side = "Home" if name == meta["home_team"] else ("Away" if name == meta["away_team"] else name)
                            selection = name
                        elif mk == "alternate_totals":
                            side = name; selection = "Game Total"
                        else:
                            side = name; selection = name
                        by_market[mk][(side, point, selection)].append((b["key"], int(price)))
            for mk, obs in by_market.items():
                _process_outcomes(mk, obs, meta["label"], meta["first_pitch"], meta["hours_to_start"])
        prog.empty()

    st.markdown("---")

    if not all_plays:
        st.warning(
            "No CFB plays at 75%+ true probability right now. "
            "CFB lines typically post Monday-Tuesday. Try refreshing later."
        )
        return

    all_plays.sort(key=lambda r: (-r["True Prob %"], -r["EV/$100"]))
    df = pd.DataFrame(all_plays)
    for c in ("True Prob %","EV/$100","Best Price","Line","Hours"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    _show_true_prob_tabs(df, all_plays, "CFB")

    # ---- Forward-Test Performance ----
    st.markdown("---")
    st.markdown("### 📈 Forward-Test Performance")
    st.caption(
        "Daily snapshots of the 75%+ picks taken at 3 PM ET, then settled the next "
        "morning vs actual results."
    )

    HISTORY_DIR = os.path.join(ROOT, "cfb_true_prob_history")

    @st.cache_data(ttl=600, show_spinner=False)
    def _load_cfb_tp_history():
        if not os.path.isdir(HISTORY_DIR):
            return []
        out = []
        for fn in sorted(os.listdir(HISTORY_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(HISTORY_DIR, fn)) as f:
                    out.append(json.load(f))
            except Exception:
                pass
        return out

    history = _load_cfb_tp_history()
    if not history:
        st.info("No history snapshots yet. The first snapshot lands at 3 PM ET today.")
        return

    all_picks = []
    for snap in history:
        for p in snap.get("picks", []):
            p2 = dict(p)
            p2["snapshot_date"] = snap.get("date")
            all_picks.append(p2)

    settled = [p for p in all_picks if p.get("result") in ("WIN","LOSS","PUSH")]
    wins   = sum(1 for p in settled if p["result"] == "WIN")
    losses = sum(1 for p in settled if p["result"] == "LOSS")
    pushes = sum(1 for p in settled if p["result"] == "PUSH")
    n_settled = wins + losses
    hit_rate  = (wins / n_settled * 100) if n_settled else 0

    risk_total = profit_total = 0.0
    for p in settled:
        if p["result"] == "PUSH": continue
        am = p.get("best_price", 0)
        risk   = 100 if am > 0 else abs(am)
        payout = am  if am > 0 else 100
        risk_total   += risk
        profit_total += payout if p["result"] == "WIN" else -risk
    roi = (profit_total / risk_total * 100) if risk_total else 0

    avg_pred = (sum(p.get("true_prob_pct",0) for p in settled) / len(settled)) if settled else 0
    cal_gap  = hit_rate - avg_pred if n_settled else 0

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Picks", f"{len(all_picks)}", f"{n_settled} settled")
    col2.metric("Hit Rate", f"{hit_rate:.1f}%",
                f"vs predicted {avg_pred:.1f}%" if n_settled else "no data")
    col3.metric("Calibration", f"{cal_gap:+.1f}pp",
                "Good" if abs(cal_gap) < 3 else ("Hot" if cal_gap > 0 else "Cold"))
    col4.metric("Net Profit", f"${profit_total:+.0f}", f"on ${risk_total:.0f}")
    col5.metric("ROI", f"{roi:+.1f}%")

    day_rows = []
    for snap in history:
        s = snap.get("summary") or {}
        if s:
            day_rows.append({
                "Date":"" + snap.get("date",""), "Picks": snap.get("n_picks"),
                "Settled": s.get("n_settled"),
                "W-L-P": f"{s.get('wins',0)}-{s.get('losses',0)}-{s.get('pushes',0)}",
                "Hit %": s.get("hit_rate"), "Net": s.get("profit_total"),
                "ROI %": s.get("roi_pct"),
            })
        else:
            day_rows.append({"Date": snap.get("date",""), "Picks": snap.get("n_picks"),
                             "Settled":"pending","W-L-P":"—","Hit %":None,"Net":None,"ROI %":None})
    if day_rows:
        st.markdown("#### Day-by-day")
        st.dataframe(pd.DataFrame(day_rows), use_container_width=True, hide_index=True,
            column_config={
                "Hit %": st.column_config.NumberColumn(format="%.1f%%"),
                "Net":   st.column_config.NumberColumn(format="$%+.0f"),
                "ROI %": st.column_config.NumberColumn(format="%+.1f%%"),
            })

    st.markdown("---")
    st.caption(
        f"Last refresh: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
        f"{len(upcoming)} games scanned  •  Min books: {_CFB_MIN_BOOKS}  •  "
        f"Min true prob: 75%  •  no price cap  •  Cache TTL: 5 min"
    )


# ═══════════════════════════════════════════════════════════════════
#  Sport tabs
# ═══════════════════════════════════════════════════════════════════

tab_nfl, tab_cfb = st.tabs(["🏈 NFL", "🎓 CFB"])

with tab_nfl:
    _render_nfl_true_prob()

with tab_cfb:
    _render_cfb_true_prob()
