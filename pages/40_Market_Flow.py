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
This describes where the money went. It is not a prediction, and a big Δ with a high top-wallet % is one bettor.
"""
import os
import re
import sys
from datetime import datetime, timedelta
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
           "cash than of the orders, a few big bettors are on it (Δ = cash % minus orders %). It shows where money went, not who "
           "will win, and one big wallet can make a side look heavy, so read the top-wallet % too. Times are Eastern.")

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


def game_sort_key(title, markets, meta):
    """Soonest game first (games with no start time last), then the biggest heavy-side delta."""
    start = (meta.get(title) or {}).get("start")
    h = mf.heaviest(markets)
    return (start is None, start or "", -(h[2] if h else 0))


def market_table(m, heavy_side=None):
    rows = []
    for s in m["sides"]:
        d = s["delta"]
        rows.append({"": "🐋" if heavy_side == s["name"] else "", "Side": s["name"],
                     "Odds": f"{s['odds']:+d}" if s.get("odds") else "-",
                     "Cash": mf.money(s["cash"]), "Cash %": f"{s['cash_pct']:.1f}%",
                     "Orders": s["orders"], "Orders %": f"{s['orders_pct']:.1f}%",
                     "Δ": f"{'🟢' if d >= 15 else '🔴' if d <= -15 else '⚪'} {d:+.1f}%",
                     "Avg order": f"${s['avg']:,.0f}",
                     "Top wallet": f"{s['top_wallet_pct']}%" if s.get("top_wallet_pct") is not None else "-"})
    return pd.DataFrame(rows)


def render_game(title, markets, meta):
    h = mf.heaviest(markets)
    start = (meta.get(title) or {}).get("start")
    when = ""
    if start:
        try:
            when = datetime.fromisoformat(start).astimezone(EASTERN).strftime("%a %I:%M %p").replace(" 0", " ") + " ET"
        except ValueError:
            pass
    total = sum(m["volume"] for m in markets)
    with st.container(border=True):
        top = f"**{title}**" + (f" · {when}" if when else "") + f" · {mf.money(total)} traded"
        st.markdown(top)
        if h:
            st.markdown(f"🐋 **Heaviest side: {h[1]}** ({h[0]}) at Δ {h[2]:+.1f}%")
        else:
            st.caption("No side has enough money and trades yet to call a heaviest side.")
        for m in markets:
            st.markdown(f"**{m['kind']}** — {mf.money(m['volume'])} traded, {m['trades']} orders")
            st.dataframe(market_table(m, h[1] if h and h[0] == m["kind"] else None), hide_index=True, use_container_width=True)


c1, c2, c3 = st.columns([1, 1, 1])
min_total = c1.slider("Games with at least this much traded ($)", 0, 50_000, 1_000, step=500, key="mf_min")
only_heavy = c2.checkbox("Only games with a heaviest side", value=False, key="mf_heavy")
if c3.button("🔄 Reload", key="mf_reload", help="Reload the newest saved reports (they are saved about every 30 minutes)."):
    load_latest.clear()

today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
tabs = st.tabs([label for _, label in SPORTS])
for tab, (sport, _) in zip(tabs, SPORTS):
    with tab:
        try:
            report, path = load_latest(True, sport, today)
        except Exception as e:  # noqa: BLE001
            st.error(f"Couldn't load the {sport} report: {e}")
            continue
        if not report or not report.get("games"):
            st.info(f"No saved {sport} Market Flow report yet. Start the \"Market flow report\" job from the Actions tab "
                    "(it takes about a minute), then tap Reload.")
            continue
        try:
            cap = datetime.fromisoformat(report["captured_at"]).astimezone(EASTERN)
            age = int((datetime.now(tz=EASTERN) - cap).total_seconds() // 60)
            st.caption(f"Report taken {cap.strftime('%a %I:%M %p').replace(' 0', ' ')} ET ({age} min ago) · "
                       f"{len(report['games'])} games, soonest first.")
        except Exception:  # noqa: BLE001
            pass
        meta = report.get("meta") or {}
        shown = 0
        for title, markets in sorted(report["games"].items(), key=lambda kv: game_sort_key(kv[0], kv[1], meta)):
            if sum(m["volume"] for m in markets) < min_total:
                continue
            if only_heavy and not mf.heaviest(markets):
                continue
            render_game(title, markets, meta)
            shown += 1
        if not shown:
            st.info("No games match the filters. Lower the dollar slider or untick \"Only games with a heaviest side\".")
