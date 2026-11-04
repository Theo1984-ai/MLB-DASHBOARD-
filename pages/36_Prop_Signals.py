"""
📈 Prop Signals — a record for the Player Props page's steam signals.

Pinnacle's Over price drops, a soft book follows, then a price crosses zero or the line drops. The hourly
auto snapshot logs each signal (best_plays_log/prop_signals/<date>.json) with an OVER bet at the best soft-book
number at that moment; the grader settles them after the game. This page shows how each tier does.
"""
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import first_seen as fs  # noqa: E402
from scripts import prop_signals as psig  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
OWNER, REPO = "Theo1984-ai", "MLB-DASHBOARD-"
LOG_DIR = "best_plays_log"


def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


GH_TOKEN = _resolve_secret("GITHUB_TOKEN")

st.set_page_config(page_title="Prop Signals", page_icon="📈", layout="wide")
st.title("📈 Prop Signals — does the steam sequence win?")
st.caption("Tracks the Player Props page's Over-steam signals. Every signal is logged with an OVER bet at the best "
           "soft-book line and price at that moment, then graded after the game. Times are Eastern.")

if not GH_TOKEN:
    st.error("Missing `GITHUB_TOKEN` in secrets.")
    st.stop()


def _retry(fn, tries=3):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


@st.cache_data(ttl=120, show_spinner=False)
def load_all(_token_present):
    from data import github_storage as gh
    out, failed = {}, []
    items = _retry(lambda: gh.list_dir(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{psig.LOG_SUB}"))
    for item in items:
        name = item["name"]
        if not name.endswith(".json"):
            continue
        try:
            out[name[:-5]] = _retry(lambda: gh.load_json(GH_TOKEN, OWNER, REPO,
                                                         f"{LOG_DIR}/{psig.LOG_SUB}/{name}")) or {}
        except Exception:  # noqa: BLE001
            failed.append(name[:-5])
    return out, failed


try:
    logs, failed_days = load_all(True)
except Exception as e:  # noqa: BLE001
    st.info(f"Nothing logged yet ({e}). It fills in after the next hourly snapshot.")
    st.stop()
if failed_days:
    load_all.clear()
    st.warning("Couldn't load " + ", ".join(sorted(failed_days)) + " (GitHub had a temporary error). Reload to retry.")
if not logs:
    st.info("Nothing logged yet. It fills in after the next hourly snapshot.")
    st.stop()

all_recs = [r for d in sorted(logs) for r in logs[d].values()]
graded = [r for r in all_recs if (r.get("result") or {}).get("outcome") in ("win", "loss", "push")]
now = datetime.now(tz=EASTERN)


def pct(w, l):
    return f"{100 * w / (w + l):.0f}%" if (w + l) else "-"


def table(title, keyf, order=None, rows=None):
    agg = fs.summarize(graded if rows is None else rows, keyf)
    if not agg:
        return
    keys = [k for k in (order or sorted(agg, key=str)) if k in agg]
    data = [{"Group": k, "Plays": agg[k]["n"], "W-L-P": f"{agg[k]['w']}-{agg[k]['l']}-{agg[k]['p']}",
             "Win %": pct(agg[k]["w"], agg[k]["l"]), "Units": round(agg[k]["units"], 1),
             "ROI %": round(agg[k]["units"] / agg[k]["n"] * 100, 1),
             "Sample": "small" if agg[k]["n"] < 30 else ""} for k in keys]
    st.markdown(f"#### {title}")
    st.dataframe(pd.DataFrame(data), hide_index=True, use_container_width=True)


tab_res, tab_today = st.tabs(["Results", "Signals by day"])
with tab_res:
    st.markdown(f"**{len(graded)} graded of {len(all_recs)} logged** across {len(logs)} days. "
                "Groups under 30 plays are too small to trust. A winning tier needs a positive ROI, not just a win rate.")
    if not graded:
        st.info("None graded yet. The auto snapshot grades them a few hours after each game ends.")
    else:
        names = psig.TIER_LABEL
        table("By tier", lambda r: names.get(r["tier"], r["tier"]), [names[t] for t in psig.TIER_ORDER])
        table("By sport and tier", lambda r: f"{r['sport']} · {names.get(r['tier'], r['tier'])}")
        table("By market", lambda r: f"{r['sport']} · {r['market']}")
        table("Full sequence by sport", lambda r: r["sport"],
              rows=[r for r in graded if r["tier"] == "full"])

with tab_today:
    day = st.selectbox("Date", sorted(logs, reverse=True), key="ps_day")
    sport = st.radio("Sport", sorted({r["sport"] for r in logs[day].values()}) or ["-"], horizontal=True, key="ps_sport")
    rows = []
    for r in logs[day].values():
        if r["sport"] != sport:
            continue
        f, res = r["first_seen"], (r.get("result") or {})
        tip = fs._parse(r["commence_time"]).astimezone(EASTERN)
        rows.append({"Tier": psig.TIER_LABEL.get(r["tier"], r["tier"]), "Game": r["game"],
                     "Tip": tip.strftime("%I:%M %p").lstrip("0"), "Player": r["who"], "Market": r["market"],
                     "Bet": f"Over {f['line']} ({f['price']:+d}) {f['book']}",
                     "Pinnacle Over": f"{f['pin_from']:+d} → {f['pin_to']:+d}" if f.get("pin_from") is not None else "",
                     "Seen": fs._parse(f["t"]).astimezone(EASTERN).strftime("%I:%M %p").lstrip("0"),
                     "Result": {"win": "✅ win", "loss": "❌ loss", "push": "➖ push"}.get(res.get("outcome"), "pending"),
                     "Actual": "" if res.get("actual") is None else str(res["actual"])})
    order = {psig.TIER_LABEL[t]: i for i, t in enumerate(psig.TIER_ORDER)}
    rows.sort(key=lambda x: (order.get(x["Tier"], 9), x["Game"], x["Player"]))
    st.caption(f"{len(rows)} signals")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
