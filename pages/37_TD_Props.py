"""
🏈 TD Props — anytime-TD prices against Pinnacle, with a record.

For every player Pinnacle prices (NFL/CFB), the hourly snapshot logs Pinnacle's fair chance of a TD, the best
soft-book price and its expected value (best_plays_log/td_props/<date>.json). The grader settles them after the
game. Results show whether Pinnacle-priced players score as often as Pinnacle says, and whether betting the
best soft-book price makes money.
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

from scripts import best_plays as bp  # noqa: E402
from scripts import first_seen as fs  # noqa: E402
from scripts import td_props as tdp  # noqa: E402

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

st.set_page_config(page_title="TD Props", page_icon="🏈", layout="wide")
st.title("🏈 TD Props — anytime TD vs Pinnacle")
st.caption("Pinnacle prices only some players. For those, the fair chance comes from Pinnacle's price with its margin "
           "removed. EV is the expected profit per $1 at the best soft-book price. Times are Eastern.")

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
    items = _retry(lambda: gh.list_dir(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{tdp.LOG_SUB}"))
    for item in items:
        name = item["name"]
        if not name.endswith(".json"):
            continue
        try:
            out[name[:-5]] = _retry(lambda: gh.load_json(GH_TOKEN, OWNER, REPO,
                                                         f"{LOG_DIR}/{tdp.LOG_SUB}/{name}")) or {}
        except Exception:  # noqa: BLE001
            failed.append(name[:-5])
    return out, failed


try:
    logs, failed_days = load_all(True)
except Exception:  # noqa: BLE001
    logs, failed_days = {}, []
if failed_days:
    load_all.clear()
    st.warning("Couldn't load " + ", ".join(sorted(failed_days)) + " (GitHub had a temporary error). Reload to retry.")
if not logs:
    st.info("Nothing logged yet. It fills in after the next hourly NFL snapshot once Pinnacle posts TD prices "
            "(usually the day before the game).")
    st.stop()

all_recs = [r for d in sorted(logs) for r in logs[d].values()]
graded = [r for r in all_recs if (r.get("result") or {}).get("outcome") in ("win", "loss")]
now = datetime.now(tz=EASTERN)
pct = lambda x: f"{100 * x:.0f}%"

tab_res, tab_day = st.tabs(["Results", "Players by day"])
with tab_res:
    st.markdown(f"**{len(graded)} graded of {len(all_recs)} logged** across {len(logs)} days. "
                "Groups under 30 players are too small to trust.")
    if not graded:
        st.info("None graded yet. The hourly job grades them after each game ends.")
    else:
        n = len(graded)
        hit = sum(r["result"]["outcome"] == "win" for r in graded) / n
        exp = sum(r["first_seen"]["fair"] for r in graded) / n
        st.markdown("#### Do Pinnacle-priced players score as often as Pinnacle says?")
        st.dataframe(pd.DataFrame([{"Players": n, "Pinnacle's chance": pct(exp), "Actually scored": pct(hit),
                                    "Difference": f"{100 * (hit - exp):+.0f} pts"}]), hide_index=True,
                     use_container_width=True)

        def ev_bucket(r):
            e = r["first_seen"]["ev"]
            return ("1. Best price beats Pinnacle by 3%+" if e >= 0.03 else "2. Beats Pinnacle (0 to 3%)" if e >= 0
                    else "3. Below Pinnacle (0 to -5%)" if e >= -0.05 else "4. Well below Pinnacle (-5% or worse)")

        for title, keyf in (("Betting the best soft-book price, by how it compares with Pinnacle", ev_bucket),
                            ("By sport", lambda r: r["sport"])):
            agg = fs.summarize(graded, keyf)
            rows = [{"Group": k, "Players": agg[k]["n"], "Scored": agg[k]["w"],
                     "Win %": pct(agg[k]["w"] / agg[k]["n"]), "Units": round(agg[k]["units"], 1),
                     "ROI %": round(agg[k]["units"] / agg[k]["n"] * 100, 1),
                     "Sample": "small" if agg[k]["n"] < 30 else ""} for k in sorted(agg)]
            st.markdown(f"#### {title}")
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

with tab_day:
    day = st.selectbox("Date", sorted(logs, reverse=True), key="td_day")
    sports = sorted({r["sport"] for r in logs[day].values()}) or ["-"]
    sport = st.radio("Sport", sports, horizontal=True, key="td_sport")
    rows = []
    for r in logs[day].values():
        if r["sport"] != sport:
            continue
        f, l, res = r["first_seen"], r.get("last_seen") or r["first_seen"], r.get("result") or {}
        tip = fs._parse(r["commence_time"]).astimezone(EASTERN)
        rows.append({"EV": f"{100 * l['ev']:+.1f}%", "_ev": l["ev"], "Player": r["who"], "Game": r["game"],
                     "Tip": tip.strftime("%a %I:%M %p").replace(" 0", " "),
                     "Pinnacle": f"{l['pin_over']:+d} / {l['pin_under']:+d}", "Fair": pct(l["fair"]),
                     "Best price": f"{l['price']:+d} {bp.BOOK_DISPLAY.get(l['book'], l['book'])}",
                     "Result": {"win": "✅ scored", "loss": "❌ no TD"}.get(res.get("outcome"), "pending")})
    rows.sort(key=lambda x: -x["_ev"])
    st.caption(f"{len(rows)} players priced by Pinnacle. Sorted by EV at the best soft-book price (latest check).")
    st.dataframe(pd.DataFrame(rows).drop(columns=["_ev"], errors="ignore"), hide_index=True, use_container_width=True)
