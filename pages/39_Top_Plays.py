"""
⭐ Top Plays — the First Seen plays whose record has been best, in one list.

  1. Any play with 3+ books stale (pick the markets for each sport and the side; default NFL receiving yards Over)
  2. NHL shots on goal Over with 2+ books stale
  3. NBA threes Over

Plays come from the First Seen log (best_plays_log/first_seen/<date>.json): the current line, price and book, Pinnacle's
line, and how many soft books are stale. There is a tab for All sports and one for each sport; each has a Record
section showing how the rules have done on graded plays.
"""
import html
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import best_plays as bp  # noqa: E402
from scripts import first_seen as fs  # noqa: E402
from scripts import injuries as injm  # noqa: E402
from scripts import top_plays as tp  # noqa: E402

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

st.set_page_config(page_title="Top Plays", page_icon="⭐", layout="wide")
st.title("⭐ Top Plays")
st.caption("The plays with the best record so far, with a tab for each sport: 3+ books stale (you choose the markets "
           "for each sport and the side), NHL shots on goal Over with 2+ books stale, and NBA threes Over. \"Books stale\" = soft books whose "
           "line is off Pinnacle's. These rules were picked after seeing the results, so keep stakes small. "
           "Times are Eastern.")

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
    """({day: {pick id: record}}, [days that could not be loaded])."""
    from data import github_storage as gh
    out, failed = {}, []
    for item in _retry(lambda: gh.list_dir(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{fs.LOG_SUB}")):
        name = item["name"]
        if not name.endswith(".json"):
            continue
        try:
            out[name[:-5]] = _retry(lambda: gh.load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{fs.LOG_SUB}/{name}")) or {}
        except Exception:  # noqa: BLE001
            failed.append(name[:-5])
    return out, failed


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


if st.button("🔄 Refresh", key="tp_refresh",
             help="Reload the latest saved plays and injuries now. New snapshots are saved about every 30 minutes; "
                  "this doesn't use any odds credits."):
    load_all.clear()
    load_injuries.clear()

try:
    logs, failed_days = load_all(True)
except Exception as e:  # noqa: BLE001
    st.error(f"Couldn't load the First Seen logs: {e}")
    st.stop()
if failed_days:
    load_all.clear()
    st.warning("Couldn't load " + ", ".join(sorted(failed_days)) + " (GitHub had a temporary error). Reload to retry.")
if not logs:
    st.info("Nothing logged yet. It fills in after the next snapshot.")
    st.stop()
try:
    injuries = load_injuries(True)
except Exception:  # noqa: BLE001
    injuries = {}

now = datetime.now(tz=EASTERN)
all_recs = [r for d in sorted(logs) for r in logs[d].values()]
graded = [r for r in all_recs if (r.get("result") or {}).get("outcome") in ("win", "loss")]
pct = lambda a, b: f"{100 * a / (a + b):.0f}%" if a + b else "-"
try:
    _latest = max(fs._parse((r.get("last_seen") or r["first_seen"])["t"]) for r in all_recs).astimezone(EASTERN)
    st.caption(f"Newest snapshot in the log: {_latest.strftime('%a %I:%M %p').replace(' 0', ' ')} ET "
               f"({int((now - _latest).total_seconds() // 60)} min ago). Page loaded {now.strftime('%I:%M %p').lstrip('0')} ET.")
except Exception:  # noqa: BLE001
    pass


def _stale_txt(n):
    return f"{n} book{'s' if n != 1 else ''} stale"


# ── Rule 1 controls (markets per sport, one side for all) ───────────────────
SPORT_TABS = [("NFL", "NFL 🏈"), ("NHL", "NHL 🏒"), ("NBA", "NBA 🏀"), ("NCAAB", "NCAAB 🏀"), ("CFB", "CFB 🎓")]
DEFAULT_MARKETS = {"NFL": ["reception_yds"]}          # the one 3+ stale group that has been clearly positive
sports_seen = {r["sport"] for r in all_recs}
markets_by_sport = {}
with st.expander("⚙️ Rule 1: 3+ books stale — choose the markets for each sport and the side", expanded=False):
    side = st.radio("Side", ["Over", "Under", "Both"], index=0, horizontal=True, key="tp_side")
    for sp, label in SPORT_TABS:
        pool = sorted({r["market"] for r in all_recs if r["sport"] == sp and tp.peak(r) >= tp.STALE_MIN})
        markets_by_sport[sp] = st.multiselect(label, pool, default=[m for m in DEFAULT_MARKETS.get(sp, []) if m in pool],
                                              key=f"tp_markets_{sp}")
    st.caption("Default = NFL receiving yards Over, the only 3+ stale group that has been clearly positive. Other "
               "sports start empty (NHL shots and NBA threes have their own rules). Each tab's Record section shows "
               "every market with 3+ books stale so you can see where the rule has and hasn't worked.")


def passes(r):
    """Rules this record passes, using each sport's chosen markets."""
    return tp.matches(r, sports=(r["sport"],), markets=tuple(markets_by_sport.get(r["sport"], [])), side=side)


c1, c2 = st.columns(2)
show_started = c1.checkbox("Include games already started", value=False, key="tp_started")
sort_by = c2.radio("Sort", ["Tip-off time", "Biggest gap"], horizontal=True, key="tp_sort")

# Open plays (and, if asked, started ones) from yesterday's and later logs.
horizon = (now - timedelta(days=1)).strftime("%Y-%m-%d")
items = []
for d in sorted(logs):
    if d < horizon:
        continue
    for r in logs[d].values():
        rules = passes(r)
        if not rules:
            continue
        tip = fs._parse(r["commence_time"]).astimezone(EASTERN)
        if tip <= now and not show_started:
            continue
        items.append({"r": r, "rules": rules, "tip": tip, "l": r.get("last_seen") or r["first_seen"]})
if sort_by == "Biggest gap":
    items.sort(key=lambda x: (-x["l"]["gap"], x["tip"]))
else:
    items.sort(key=lambda x: (x["tip"], -x["l"]["gap"]))


def render_cards(sport):
    """Cards for `sport` (None = every sport)."""
    mine = [x for x in items if sport is None or x["r"]["sport"] == sport]
    counts = {name: sum(name in x["rules"] for x in mine) for name in tp.RULES}
    m1, m2, m3 = st.columns(3)
    m1.metric(tp.RULE_STALE, counts[tp.RULE_STALE])
    m2.metric(tp.RULE_NHL, counts[tp.RULE_NHL])
    m3.metric(tp.RULE_NBA, counts[tp.RULE_NBA])
    if not mine:
        st.info("No open plays match right now. The line splits close as game time gets near, and new ones show up "
                "after each snapshot (about every 30 minutes).")
    for x in mine:
        r, l, tip = x["r"], x["l"], x["tip"]
        res = r.get("result") or {}
        out = res.get("outcome")
        started = tip <= now
        if out in ("win", "loss"):
            rc = "#15803d" if out == "win" else "#b91c1c"
            badge = (f'<span style="background:{rc};color:#fff;font-size:10px;font-weight:700;padding:1px 6px;'
                     f'border-radius:3px;margin-right:6px;">{out.upper()}'
                     f'{" " + format(res["actual"], "g") if isinstance(res.get("actual"), (int, float)) else ""}</span>')
        elif started:
            badge = ('<span style="background:#9ca3af;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;'
                     'border-radius:3px;margin-right:6px;">STARTED</span>')
        else:
            badge = ""
        rule_html = "".join(f'<span style="background:#15803d;color:#fff;font-size:10px;font-weight:700;'
                            f'padding:1px 7px;border-radius:3px;">⭐ {html.escape(n)}</span>' for n in x["rules"])
        e = injm.play_injury(injuries, {**r, "line": l["line"]})
        inj_html = injm.badge(e)
        book = bp.BOOK_DISPLAY.get(l["book"], l["book"])
        st.markdown(
            f'<div style="background:#f0fdf4;border:1.5px solid #86efac;border-radius:8px;padding:10px 14px;'
            f'margin-bottom:6px;color:#111827;">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
            f'<span style="font-size:14px;font-weight:700;">{html.escape(r["who"])}{inj_html}'
            f'<span style="font-size:11px;color:#6b7280;font-weight:400;"> — {html.escape(r["game"])} · '
            f'{bp.SPORTS[r["sport"]]["label"]}</span></span>'
            f'<span style="font-size:11px;color:#6b7280;white-space:nowrap;">{badge}'
            f'{tip.strftime("%a %I:%M %p")} ET</span></div>'
            f'<div style="display:flex;align-items:baseline;gap:10px;margin:2px 0;flex-wrap:wrap;">'
            f'<span style="font-size:11px;color:#6b7280;">{html.escape(r["market"].replace("_", " "))}</span>'
            f'<span style="font-size:18px;font-weight:800;color:#15803d;">{r["bet"]} {l["line"]}</span>'
            f'<span style="font-size:13px;">@ <strong>{html.escape(book)}</strong> {l["price"]:+d}</span>'
            f'{rule_html}</div>'
            f'<div style="font-size:11px;color:#6b7280;">Pinnacle {l["pin_line"]} · gap {l["gap"]} · '
            f'{_stale_txt(tp.peak(r))} of {l["n_books"]} · check the price and lineups first</div></div>',
            unsafe_allow_html=True)


def render_record(sport):
    """Each rule's record on graded plays of `sport` (None = every sport), then the 3+ stale breakdown by market."""
    mine = [r for r in graded if sport is None or r["sport"] == sport]
    st.markdown(f"**{len(mine)} graded plays** across {len(logs)} days. Groups under 30 plays are too small to trust.")
    groups = defaultdict(list)
    for r in mine:
        for name in passes(r):
            groups[name].append(r)
    rows = []
    for name in tp.RULES:
        if sport is not None and ((name == tp.RULE_NHL and sport != "NHL") or (name == tp.RULE_NBA and sport != "NBA")
                                  or (name == tp.RULE_STALE and not markets_by_sport.get(sport))):
            continue
        s = tp.record(groups.get(name, []))
        rows.append({"Rule": name, "Plays": s["n"], "W-L": f"{s['w']}-{s['l']}", "Win %": pct(s["w"], s["l"]),
                     "Units": round(s["units"], 1), "ROI %": round(100 * s["units"] / s["n"], 1) if s["n"] else 0.0,
                     "Sample": "small" if s["n"] < 30 else ""})
    st.markdown("#### Each rule on graded plays (with your Rule 1 choices)")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    brow = []
    for (sp, mk), recs in sorted(tp.stale_breakdown(mine, side).items(), key=lambda kv: -len(kv[1])):
        s = tp.record(recs)
        if s["n"]:
            brow.append({"Sport": sp, "Market": mk, "Plays": s["n"], "W-L": f"{s['w']}-{s['l']}",
                         "Win %": pct(s["w"], s["l"]), "Units": round(s["units"], 1),
                         "ROI %": round(100 * s["units"] / s["n"], 1), "Sample": "small" if s["n"] < 30 else ""})
    st.markdown(f"#### 3+ books stale, {side.lower()} side: where it has and hasn't worked")
    if brow:
        st.dataframe(pd.DataFrame(brow), hide_index=True, use_container_width=True)
    else:
        st.info("No graded plays yet.")


n_open = lambda sp: sum(1 for x in items if sp is None or x["r"]["sport"] == sp)
tabs = st.tabs([f"All ({n_open(None)})"] + [f"{label} ({n_open(sp)})" for sp, label in SPORT_TABS])
for tab, sport in zip(tabs, [None] + [sp for sp, _ in SPORT_TABS]):
    with tab:
        if sport is not None and sport not in sports_seen:
            st.info(f"No {sport} plays logged yet.")
            continue
        render_cards(sport)
        with st.expander("📊 Record", expanded=False):
            render_record(sport)
