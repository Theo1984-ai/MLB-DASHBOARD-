"""
🕐 First Seen — when each player-prop line split first shows up, and whether early ones do better.

A line split is a prop where a soft book's line differs from Pinnacle's. The hourly auto snapshot logs the first
time each one appears (best_plays_log/first_seen/<date>.json) and the grader settles them after the game.
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

from scripts import best_plays as bp  # noqa: E402
from scripts import first_seen as fs  # noqa: E402
from scripts import injuries as injm  # noqa: E402

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

st.set_page_config(page_title="First Seen", page_icon="🕐", layout="wide")
st.title("🕐 First Seen — when line splits first show up")
st.caption("A line split = a soft book's line differs from Pinnacle's. Logged by the hourly auto snapshot "
           "the first time each one appears. Times are Eastern.")

if not GH_TOKEN:
    st.error("Missing `GITHUB_TOKEN` in secrets.")
    st.stop()


@st.cache_data(ttl=300, show_spinner=False)
def load_all(_token_present):
    from data import github_storage as gh
    out = {}
    for item in gh.list_dir(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{fs.LOG_SUB}"):
        name = item["name"]
        if name.endswith(".json"):
            out[name[:-5]] = gh.load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{fs.LOG_SUB}/{name}") or {}
    return out


@st.cache_data(ttl=600, show_spinner=False)
def load_injuries(_token_present):
    from data import github_storage as gh
    out = {}
    for sport in injm.PATHS:
        try:
            out[sport] = gh.load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/injuries/{sport}.json") or {}
        except Exception:  # noqa: BLE001
            out[sport] = {}
    return out


try:
    logs = load_all(True)
except Exception as e:  # noqa: BLE001
    st.error(f"Couldn't load the first-seen logs: {e}")
    st.stop()

if not logs:
    st.info("Nothing logged yet. It fills in after the next hourly snapshot.")
    st.stop()

now = datetime.now(tz=EASTERN)
today = now.strftime("%Y-%m-%d")
all_recs = [r for d in sorted(logs) for r in logs[d].values()]
sports = sorted({r["sport"] for r in all_recs})
tab_today, tab_results = st.tabs(["Today", "Results"])


def pct(w, l):
    return f"{w / (w + l) * 100:.0f}%" if w + l else "-"


with tab_today:
    pick = st.multiselect("Sports", sports, default=sports)
    sort = st.radio("Sort by", ["Newest first", "Earliest first", "Biggest gap"], horizontal=True)
    inj = load_injuries(True)
    rows = []
    for r in logs.get(today, {}).values():
        if r["sport"] not in pick:
            continue
        tip = fs._parse(r["commence_time"]).astimezone(EASTERN)
        if tip <= now:
            continue
        seen = fs._parse(r["first_seen"]["t"]).astimezone(EASTERN)
        away, _, home = r["game"].partition(" @ ")
        e = injm.find((inj.get(r["sport"]) or {}).get("teams"), away, home, r["who"])
        rows.append({"_seen": seen, "First seen": seen.strftime("%I:%M %p").lstrip("0"),
                     "Tip-off": tip.strftime("%I:%M %p").lstrip("0"),
                     "Lead (h)": round(fs.hours_before_tip(r), 1), "Sport": r["sport"], "Player": r["who"],
                     "Market": r["market"], "Bet": r["bet"], "Line": r["first_seen"]["line"],
                     "Book": bp.BOOK_DISPLAY.get(r["first_seen"]["book"], r["first_seen"]["book"]),
                     "Price": r["first_seen"]["price"], "Pinnacle": r["first_seen"]["pin_line"],
                     "Gap": r["first_seen"]["gap"], "Books off": r["first_seen"]["stale"],
                     "Injury": e["status"] if e and injm.is_real(e["status"]) else ""})
    st.markdown(f"**{len(rows)} open line splits today**")
    if rows:
        df = pd.DataFrame(rows)
        if sort == "Biggest gap":
            df = df.sort_values(["Gap", "Books off"], ascending=False)
        else:
            df = df.sort_values("_seen", ascending=(sort == "Earliest first"))
        st.dataframe(df.drop(columns="_seen"), hide_index=True, use_container_width=True)
        by_hour = pd.Series([r["_seen"].hour for r in rows]).value_counts().sort_index()
        st.markdown("**Plays by the hour they first appeared**")
        st.bar_chart(by_hour.rename(lambda h: f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"))

with tab_results:
    graded = [r for r in all_recs if (r.get("result") or {}).get("outcome") in ("win", "loss", "push")]
    st.markdown(f"**{len(graded)} graded of {len(all_recs)} logged** across {len(logs)} days. "
                "Groups under 30 plays are too small to trust.")
    if not graded:
        st.info("None graded yet. The auto snapshot grades them a few hours after each game ends.")
    sport_pick = st.multiselect("Sports ", sports, default=sports, key="res_sports")
    recs = [r for r in graded if r["sport"] in sport_pick]

    def show(title, key, order=None):
        agg = fs.summarize(recs, key)
        if not agg:
            return
        keys = [k for k in (order or sorted(agg, key=str)) if k in agg]
        table = [{"Group": k, "Plays": agg[k]["n"], "W-L-P": f"{agg[k]['w']}-{agg[k]['l']}-{agg[k]['p']}",
                  "Win %": pct(agg[k]["w"], agg[k]["l"]), "Units": round(agg[k]["units"], 1),
                  "ROI %": round(agg[k]["units"] / agg[k]["n"] * 100, 1),
                  "Sample": "small" if agg[k]["n"] < 30 else ""} for k in keys]
        st.markdown(f"#### {title}")
        st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)

    show("By how long before tip-off it first appeared", lambda r: fs.bucket_lead(fs.hours_before_tip(r)),
         fs.LEAD_ORDER)
    show("By the hour it first appeared (ET)",
         lambda r: f"{(fs.first_seen_hour(r) % 12) or 12} {'AM' if fs.first_seen_hour(r) < 12 else 'PM'}",
         [f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}" for h in range(24)])
    show("By sport", lambda r: r["sport"])
    show("Over vs Under", lambda r: r["bet"])
    show("By line gap to Pinnacle", lambda r: ("3+" if r["first_seen"]["gap"] >= 3 else
                                                "1.5-2.5" if r["first_seen"]["gap"] >= 1.5 else
                                                "1" if r["first_seen"]["gap"] >= 1 else "0.5"),
         ["0.5", "1", "1.5-2.5", "3+"])
    show("By books still off Pinnacle", lambda r: str(min(r["first_seen"]["stale"], 5)) + ("+" if r["first_seen"]["stale"] >= 5 else ""))
