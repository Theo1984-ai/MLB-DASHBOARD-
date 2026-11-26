"""
🐋 Market Flow — where the real money is going on each game's moneyline, spread and total (Polymarket trades).

Read from the newest saved report of each sport (best_plays_log/market_flow/<date>/<SPORT>_<hhmm>.json, written by the
"Market flow report" GitHub Action: python -m scripts.market_flow). For every market and side it shows:

  Cash %    share of the dollars traded on the market that went to this side
  Orders %  share of the trades (orders) that went to this side
  Δ         cash % minus orders %: positive = this side's orders are bigger than average = a few large bettors
  Avg order dollars per trade on this side
  Top wallet  the biggest single wallet's share of the side's dollars (high = one bettor, not "the crowd")

The "heaviest side" of a game is the side with the biggest positive Δ that has at least $5,000 and 20 trades behind it.
A side is "playable" (✅) when its Δ is +15 or more, it has at least $5,000 and 20 trades, no wallet holds 40% or more of it, and
its price is no worse than -250. The first tab lists every playable side that has not started yet, soonest first, in green.
This describes where the money went. It is not a prediction or a tested edge.
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import market_flow as mf  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
OWNER, REPO = "Theo1984-ai", "MLB-DASHBOARD-"
LOG_DIR = "best_plays_log"
SPORTS = [("NFL", "NFL 🏈"), ("NBA", "NBA 🏀"), ("NHL", "NHL 🏒"), ("CFB", "CFB 🎓"), ("NCAAB", "NCAAB 🏀")]
STALE_MIN = 90                 # a report older than this gets a warning on the Playable tab


def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


GH_TOKEN = _resolve_secret("GITHUB_TOKEN")

st.set_page_config(page_title="Market Flow", page_icon="🐋", layout="wide")
st.title("🐋 Market Flow")
st.caption("Where the real money is going on each game (Polymarket trades). Cash % vs orders %: when one side has far more of the "
           "cash than of the orders, a few big bettors are on it (Δ = cash % minus orders %). ✅ PLAY marks the sides with a big Δ, "
           "real money behind them, no single wallet running the show and a sensible price. It shows where money went, not who "
           "will win, and it is not a tested edge, so keep stakes small. Times are Eastern.")

if not GH_TOKEN:
    st.error("Missing `GITHUB_TOKEN` in secrets.")
    st.stop()


@st.cache_data(ttl=120, show_spinner=False)
def load_latest(_token_present, sport, today):
    """(report dict or None, file path) for the newest saved Market Flow report of `sport` from the last two days."""
    from data import github_storage as gh
    best = None
    for back in (0, 1):
        day = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=back)).strftime("%Y-%m-%d")
        try:
            items = gh.list_dir(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/market_flow/{day}")
        except Exception:  # noqa: BLE001
            continue
        names = sorted(i["name"] for i in items if re.fullmatch(rf"{sport}_\d{{4}}\.json", i["name"]))
        if names:
            best = f"{LOG_DIR}/market_flow/{day}/{names[-1]}"
            break
    if not best:
        return None, None
    return gh.load_json(GH_TOKEN, OWNER, REPO, best), best


def start_of(meta, title):
    """The game's start as an aware datetime, or None."""
    s = (meta.get(title) or {}).get("start")
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_when(d):
    return d.astimezone(EASTERN).strftime("%a %I:%M %p").replace(" 0", " ") + " ET" if d else ""


def game_sort_key(title, markets, meta):
    """Soonest game first (games with no start time last), then the biggest heavy-side delta."""
    start = (meta.get(title) or {}).get("start")
    h = mf.heaviest(markets)
    return (start is None, start or "", -(h[2] if h else 0))


def market_table(m, heavy_side=None, play_sides=()):
    rows = []
    for s in m["sides"]:
        d = s["delta"]
        mark = "✅ PLAY" if s["name"] in play_sides else "🐋" if heavy_side == s["name"] else ""
        rows.append({"": mark, "Side": s["name"],
                     "Odds": f"{s['odds']:+d}" if s.get("odds") else "-",
                     "Cash": mf.money(s["cash"]), "Cash %": f"{s['cash_pct']:.1f}%",
                     "Orders": s["orders"], "Orders %": f"{s['orders_pct']:.1f}%",
                     "Δ": f"{'🟢' if d >= 15 else '🔴' if d <= -15 else '⚪'} {d:+.1f}%",
                     "Avg order": f"${s['avg']:,.0f}",
                     "Top wallet": f"{s['top_wallet_pct']}%" if s.get("top_wallet_pct") is not None else "-"})
    return pd.DataFrame(rows)


def play_text(p, game, when):
    """One line for a playable side, e.g. 'Bills moneyline -141 · Chiefs vs. Bills · Thu 8:20 PM ET'."""
    odds = f" {p['odds']:+d}" if p.get("odds") else ""
    tw = f", top wallet {p['top_wallet_pct']}%" if p.get("top_wallet_pct") is not None else ""
    return (f"**{p['side']} {p['kind'].lower()}{odds}** · {game}" + (f" · {when}" if when else "")
            + f"  \nΔ {p['delta']:+.1f}% · {mf.money(p['cash'])} on it · {p['orders']} orders · average order ${p['avg']:,.0f}{tw}"
            + ("  \n✅ also playable in another market for the same team" if p["agree"] else ""))


def render_game(title, markets, meta, now):
    h = mf.heaviest(markets)
    start = start_of(meta, title)
    started = bool(start and start <= now)
    plays = [] if started else mf.playable(markets)
    when = fmt_when(start)
    total = sum(m["volume"] for m in markets)
    with st.container(border=True):
        top = (f"{'✅ ' if plays else ''}**{title}**" + (f" · {when}" if when else "") + f" · {mf.money(total)} traded"
               + (" · STARTED" if started else ""))
        st.markdown(top)
        for p in plays:
            st.success(play_text(p, title, ""), icon="✅")
        if h:
            st.markdown(f"🐋 **Heaviest side: {h[1]}** ({h[0]}) at Δ {h[2]:+.1f}%")
        else:
            st.caption("No side has enough money and trades yet to call a heaviest side.")
        for m in markets:
            st.markdown(f"**{m['kind']}** — {mf.money(m['volume'])} traded, {m['trades']} orders")
            play_sides = {p["side"] for p in plays if p["kind"] == m["kind"]}
            st.dataframe(market_table(m, h[1] if h and h[0] == m["kind"] else None, play_sides),
                         hide_index=True, use_container_width=True)


c1, c2, c3 = st.columns([1, 1, 1])
min_total = c1.slider("Games with at least this much traded ($)", 0, 50_000, 1_000, step=500, key="mf_min")
only_play = c2.checkbox("Only games with a ✅ playable side", value=False, key="mf_play")
if c3.button("🔄 Reload", key="mf_reload", help="Reload the newest saved reports (they are saved about every 30 minutes)."):
    load_latest.clear()

now = datetime.now(tz=EASTERN)
today = now.strftime("%Y-%m-%d")
reports = {}
for sport, _ in SPORTS:
    try:
        reports[sport] = load_latest(True, sport, today)[0]
    except Exception as e:  # noqa: BLE001
        reports[sport] = None
        st.warning(f"Couldn't load the {sport} report: {e}")

# Every playable side that has not started, across all sports, soonest first.
all_plays = []
for sport, _ in SPORTS:
    rep = reports.get(sport)
    if not rep or not rep.get("games"):
        continue
    meta = rep.get("meta") or {}
    try:
        cap = datetime.fromisoformat(rep["captured_at"]).astimezone(EASTERN)
    except Exception:  # noqa: BLE001
        cap = None
    for title, markets in rep["games"].items():
        start = start_of(meta, title)
        if start and start <= now:
            continue
        if sum(m["volume"] for m in markets) < min_total:
            continue
        for p in mf.playable(markets):
            all_plays.append((start, sport, title, p, cap))
all_plays.sort(key=lambda x: (x[0] is None, x[0] or now, not x[3]["agree"], -x[3]["delta"]))

tabs = st.tabs([f"✅ What to play ({len(all_plays)})"] + [label for _, label in SPORTS])
with tabs[0]:
    if not all_plays:
        st.info("Nothing playable right now: no side has a big enough Δ with real money, a spread-out crowd and a sensible price. "
                "Check back as game time gets closer, when the money comes in (reports are saved about every 30 minutes).")
    else:
        st.caption("Sides that pass the checks, soonest game first (agreeing sides first within a game). Stakes small and flat; "
                   "this is where money went, not a sure thing. Check the price at your book first.")
        stale = sorted({c for *_, c in all_plays if c and (now - c).total_seconds() > STALE_MIN * 60})
        if stale:
            st.warning("Some of these come from a report more than "
                       f"{STALE_MIN} minutes old (oldest taken {fmt_when(stale[0])}). Reload, or check the numbers on the sport's tab.")
        for start, sport, title, p, cap in all_plays:
            st.success(f"{dict(SPORTS)[sport]} · " + play_text(p, title, fmt_when(start)), icon="✅")
    st.caption("Checks: Δ +15 or more · at least $5,000 and 20 trades on the side · no wallet holds 40% or more · price no worse than -250.")

for tab, (sport, _) in zip(tabs[1:], SPORTS):
    with tab:
        report = reports.get(sport)
        if not report or not report.get("games"):
            st.info(f"No saved {sport} Market Flow report yet. Start the \"Market flow report\" job from the Actions tab "
                    "(it takes about a minute), then tap Reload.")
            continue
        try:
            cap = datetime.fromisoformat(report["captured_at"]).astimezone(EASTERN)
            age = int((now - cap).total_seconds() // 60)
            st.caption(f"Report taken {cap.strftime('%a %I:%M %p').replace(' 0', ' ')} ET ({age} min ago) · "
                       f"{len(report['games'])} games, soonest first.")
        except Exception:  # noqa: BLE001
            pass
        meta = report.get("meta") or {}
        shown = 0
        for title, markets in sorted(report["games"].items(), key=lambda kv: game_sort_key(kv[0], kv[1], meta)):
            if sum(m["volume"] for m in markets) < min_total:
                continue
            start = start_of(meta, title)
            if only_play and ((start and start <= now) or not mf.playable(markets)):
                continue
            render_game(title, markets, meta, now)
            shown += 1
        if not shown:
            st.info("No games match the filters. Lower the dollar slider or untick \"Only games with a ✅ playable side\".")
