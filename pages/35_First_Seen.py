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
st.caption("A line split = a soft book's line differs from Pinnacle's. Shows only the first wave: the splits that "
           "appeared in the first snapshot of the day that had any for that sport. Later arrivals are left out. "
           "Times are Eastern.")

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
            day = gh.load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{fs.LOG_SUB}/{name}") or {}
            out[name[:-5]] = fs.first_wave(day) if day else {}     # first wave only
    return out


@st.cache_data(ttl=600, show_spinner=False)
def load_roster(sport, _token_present):
    from data import github_storage as gh
    try:
        return bp.roster_lookup(gh.load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/rosters/{sport}.json") or {})
    except Exception:  # noqa: BLE001
        return {}


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
SPORT_TABS = [("NBA", "NBA 🏀"), ("NFL", "NFL 🏈"), ("NHL", "NHL 🏒"), ("CFB", "CFB 🎓")]


def pct(w, l):
    return f"{w / (w + l) * 100:.0f}%" if w + l else "-"


def render_today(sport, recs_by_day, key, day):
    sort = st.radio("Sort by", ["Time and team", "Newest first", "Earliest first", "Biggest gap"],
                    horizontal=True, key=f"sort_{key}")
    inj = load_injuries(True)
    roster = load_roster(sport, True)
    rows = []
    past = day != today
    for r in recs_by_day.get(day, {}).values():
        if r["sport"] != sport:
            continue
        tip = fs._parse(r["commence_time"]).astimezone(EASTERN)
        if tip <= now and not past:
            continue
        seen = fs._parse(r["first_seen"]["t"]).astimezone(EASTERN)
        away, _, home = r["game"].partition(" @ ")
        e = injm.find((inj.get(r["sport"]) or {}).get("teams"), away, home, r["who"])
        rows.append({"r": r, "seen": seen, "tip": tip, "team": bp.player_team(roster, away, home, r["who"]) or "",
                     "inj": e if e and injm.is_real(e["status"]) else None})
    if past:
        done = [x["r"].get("result") or {} for x in rows]
        w = sum(d.get("outcome") == "win" for d in done)
        l = sum(d.get("outcome") == "loss" for d in done)
        units = sum(d.get("profit") or 0 for d in done if d.get("outcome") in ("win", "loss", "push"))
        st.markdown(f"**{len(rows)} line splits on {day}** · graded {w + l + sum(d.get('outcome') == 'push' for d in done)}"
                    f" · **{w}-{l}** · {units:+.1f}u")
    else:
        st.markdown(f"**{len(rows)} open line splits today**")
    if sort == "Time and team":
        rows.sort(key=lambda x: (x["tip"], x["r"]["game"], x["team"], x["r"]["who"]))
    elif sort == "Biggest gap":
        rows.sort(key=lambda x: (-x["r"]["first_seen"]["gap"], -x["r"]["first_seen"]["stale"]))
    else:
        rows.sort(key=lambda x: x["seen"], reverse=(sort == "Newest first"))
    for x in rows:
        r, fsn = x["r"], x["r"]["first_seen"]
        is_over = r["bet"] == "OVER"
        bg, border, colour = ("#f0fdf4", "#86efac", "#15803d") if is_over else ("#eff6ff", "#93c5fd", "#1d4ed8")
        team_html = (f'<span style="background:#e5e7eb;color:#111827;font-size:10px;font-weight:700;'
                     f'padding:1px 6px;border-radius:3px;margin-left:6px;">{x["team"]}</span>' if x["team"] else "")
        inj_html = injm.badge(x["inj"])
        book = bp.BOOK_DISPLAY.get(fsn["book"], fsn["book"])
        lead = fs.hours_before_tip(r)
        res = r.get("result") or {}
        out = res.get("outcome")
        if out in ("win", "loss", "push"):
            rc = {"win": "#15803d", "loss": "#b91c1c", "push": "#6b7280"}[out]
            actual = res.get("actual")
            res_html = (f'<span style="background:{rc};color:#fff;font-size:10px;font-weight:700;padding:1px 6px;'
                        f'border-radius:3px;margin-right:6px;">{out.upper()}'
                        f'{f" · {actual:g}" if isinstance(actual, (int, float)) else ""}</span>')
        elif past:
            res_html = ('<span style="background:#9ca3af;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;'
                        'border-radius:3px;margin-right:6px;">PENDING</span>')
        else:
            res_html = ""
        st.markdown(
            f'<div style="background:{bg};border:1.5px solid {border};border-radius:8px;'
            f'padding:10px 14px;margin-bottom:6px;color:#111827;">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
            f'<span style="font-size:14px;font-weight:700;">{r["who"]}{team_html}{inj_html}'
            f'<span style="font-size:11px;color:#6b7280;font-weight:400;"> — {r["game"]} · '
            f'{bp.SPORTS[r["sport"]]["label"]}</span></span>'
            f'<span style="font-size:11px;color:#6b7280;">{res_html}{x["tip"].strftime("%a %I:%M %p")} ET</span></div>'
            f'<div style="display:flex;align-items:baseline;gap:10px;margin:2px 0;flex-wrap:wrap;">'
            f'<span style="font-size:11px;color:#6b7280;">{r["market"]}</span>'
            f'<span style="font-size:18px;font-weight:800;color:{colour};">{r["bet"]} {fsn["line"]}</span>'
            f'<span style="font-size:13px;">@ <strong>{book}</strong> {fsn["price"]:+d}</span>'
            f'<span style="background:#fff;border:1px solid {border};color:{colour};font-size:11px;'
            f'font-weight:700;padding:1px 7px;border-radius:3px;">gap {fsn["gap"]}</span>'
            f'<span style="font-size:11px;color:#6b7280;">first seen {x["seen"].strftime("%I:%M %p").lstrip("0")} ET'
            f'{f" · {lead:.1f} h before tip" if lead is not None else ""}</span></div>'
            f'<div style="font-size:11px;color:#6b7280;">Pinnacle {fsn["pin_line"]} · '
            f'{fsn["stale"]} of {fsn["n_books"]} books off it</div></div>',
            unsafe_allow_html=True,
        )


def render_results(sport):
    mine = [r for r in all_recs if r["sport"] == sport]
    recs = [r for r in mine if (r.get("result") or {}).get("outcome") in ("win", "loss", "push")]
    st.markdown(f"**{len(recs)} graded of {len(mine)} logged** across {len(logs)} days. "
                "Groups under 30 plays are too small to trust.")
    if not recs:
        st.info("None graded yet. The auto snapshot grades them a few hours after each game ends.")
        return

    def show(title, keyf, order=None):
        agg = fs.summarize(recs, keyf)
        if not agg:
            return
        keys = [k for k in (order or sorted(agg, key=str)) if k in agg]
        table = [{"Group": k, "Plays": agg[k]["n"], "W-L-P": f"{agg[k]['w']}-{agg[k]['l']}-{agg[k]['p']}",
                  "Win %": pct(agg[k]["w"], agg[k]["l"]), "Units": round(agg[k]["units"], 1),
                  "ROI %": round(agg[k]["units"] / agg[k]["n"] * 100, 1),
                  "Sample": "small" if agg[k]["n"] < 30 else ""} for k in keys]
        st.markdown(f"#### {title}")
        st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)

    hour = lambda r: f"{(fs.first_seen_hour(r) % 12) or 12} {'AM' if fs.first_seen_hour(r) < 12 else 'PM'}"
    show("By how long before tip-off it first appeared", lambda r: fs.bucket_lead(fs.hours_before_tip(r)),
         fs.LEAD_ORDER)
    show("By the hour it first appeared (ET)", hour,
         [f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}" for h in range(24)])
    show("Over vs Under", lambda r: r["bet"])
    show("By line gap to Pinnacle", lambda r: ("3+" if r["first_seen"]["gap"] >= 3 else
                                                "1.5-2.5" if r["first_seen"]["gap"] >= 1.5 else
                                                "1" if r["first_seen"]["gap"] >= 1 else "0.5"),
         ["0.5", "1", "1.5-2.5", "3+"])
    show("By books still off Pinnacle",
         lambda r: str(min(r["first_seen"]["stale"], 5)) + ("+" if r["first_seen"]["stale"] >= 5 else ""))


days = sorted(set(logs) | {today}, reverse=True)
day = st.selectbox("Date", days, key="fs_day",
                   format_func=lambda d: datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %d").replace(" 0", " ")
                   + (" (today)" if d == today else ""))
tabs = st.tabs([label for _, label in SPORT_TABS])
for tab, (sport, _) in zip(tabs, SPORT_TABS):
    with tab:
        n_all = sum(1 for r in all_recs if r["sport"] == sport)
        if not n_all:
            st.info(f"No {sport} line splits logged yet (no {sport} games on the snapshots).")
            continue
        view = st.radio("View", ["Today", "Results"], horizontal=True, key=f"view_{sport}")
        if view == "Today":
            render_today(sport, logs, sport, day)
        else:
            render_results(sport)
