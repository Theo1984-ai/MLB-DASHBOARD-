"""
🕐 First Seen — when each player-prop line split first shows up, and whether early ones do better.

A line split is a prop where a soft book's line differs from Pinnacle's. The hourly auto snapshot logs the first
time each one appears (best_plays_log/first_seen/<date>.json) and the grader settles them after the game.
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


def _retry(fn, tries=3):
    """Call fn, retrying a failed GitHub request (a temporary 500 is common) before giving up."""
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
    """({day: first-wave records}, [days that could not be loaded]). One bad day no longer hides the rest."""
    from data import github_storage as gh
    out, failed = {}, []
    items = _retry(lambda: gh.list_dir(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{fs.LOG_SUB}"))
    for item in items:
        name = item["name"]
        if not name.endswith(".json"):
            continue
        try:
            day = _retry(lambda: gh.load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{fs.LOG_SUB}/{name}")) or {}
        except Exception:  # noqa: BLE001
            failed.append(name[:-5])
            continue
        out[name[:-5]] = fs.first_wave(day) if day else {}     # first wave only
    return out, failed


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
    logs, failed_days = load_all(True)
except Exception as e:  # noqa: BLE001
    st.error(f"Couldn't load the first-seen logs: {e}")
    st.stop()
if failed_days:
    load_all.clear()            # don't keep a failed day cached; the next refresh tries again
    st.warning("Couldn't load " + ", ".join(sorted(failed_days)) + " (GitHub had a temporary error). "
               "Showing the other days. Reload to try again.")

if not logs:
    st.info("Nothing logged yet. It fills in after the next hourly snapshot.")
    st.stop()

now = datetime.now(tz=EASTERN)
today = now.strftime("%Y-%m-%d")
all_recs = [r for d in sorted(logs) for r in logs[d].values()]
MIN_OFF = 4          # "4+ books off Pinnacle": the first week's clearest rule (41-23, +10u; fewer than 4 lost)
NO_PTS = "no points"  # 4+ off without points props: 34-14, +12.5u in the first week (picked after the fact, re-test)
SPORT_TABS = [("NBA", "NBA 🏀"), ("NCAAB", "NCAAB 🏀"), ("NFL", "NFL 🏈"), ("NHL", "NHL 🏒"), ("CFB", "CFB 🎓")]


MIN_OFF_2 = 2        # "2+ books off Pinnacle": NHL shots Overs went 18-7 (+3.5u) at 2+ vs 30-19 (-2.8u) at 1 book off


def peak(r):
    """Most soft books that were off Pinnacle for this play, at the first sighting or the latest check."""
    return max(r["first_seen"].get("stale") or 0, (r.get("last_seen") or r["first_seen"]).get("stale") or 0)


def _stale_txt(n):
    """'1 book stale' / '2 books stale'."""
    return f"{n} book{'s' if n != 1 else ''} stale"


def keep(r):
    """Does this play pass the 'Plays' switch at the top of the page?"""
    if two_plus:
        return peak(r) >= MIN_OFF_2
    if not (only4 or only4_np):
        return True
    if r["first_seen"]["stale"] < MIN_OFF:
        return False
    return not (only4_np and r.get("market") == "points")


def pct(w, l):
    return f"{w / (w + l) * 100:.0f}%" if w + l else "-"


def render_today(sport, recs_by_day, key, day):
    sort = st.radio("Sort by", ["Team: Overs then Unders", "Time and team", "Newest first", "Earliest first", "Biggest gap"],
                    horizontal=True, key=f"sort_{key}")
    inj = load_injuries(True)
    roster = load_roster(sport, True)
    rows = []
    past = day < today
    open_only = False
    if not past:
        open_only = st.radio("Show", ["All of today's plays", "Games not started"], horizontal=True,
                             key=f"show_{key}") == "Games not started"
    for r in recs_by_day.get(day, {}).values():
        if r["sport"] != sport:
            continue
        if not keep(r):
            continue
        tip = fs._parse(r["commence_time"]).astimezone(EASTERN)
        if tip <= now and open_only:
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
        started = sum(x["tip"] <= now for x in rows)
        done = [x["r"].get("result") or {} for x in rows]
        w = sum(d.get("outcome") == "win" for d in done)
        l = sum(d.get("outcome") == "loss" for d in done)
        units = sum(d.get("profit") or 0 for d in done if d.get("outcome") in ("win", "loss", "push"))
        st.markdown(f"**{len(rows)} line splits {'today' if day == today else 'on ' + day}** · "
                    f"{len(rows) - started} not started · {started} started"
                    + (f" · final so far **{w}-{l}** · {units:+.1f}u" if w + l else ""))
    if sort == "Team: Overs then Unders":
        rows.sort(key=lambda x: (x["tip"], x["r"]["game"], x["team"] == "", x["team"],
                                 x["r"]["bet"] != "OVER", x["r"]["who"]))
    elif sort == "Time and team":
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
        elif past or x["tip"] <= now:
            label = "PENDING" if past else "STARTED"
            res_html = ('<span style="background:#9ca3af;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;'
                        f'border-radius:3px;margin-right:6px;">{label}</span>')
        else:
            res_html = ""
        final_html = ""
        if out in ("win", "loss", "push") and isinstance(res.get("actual"), (int, float)):
            unit = {"win": f"+{res.get('profit') or 0:.2f}u", "loss": f"{res.get('profit') or -1.0:.2f}u", "push": "0.00u"}[out]
            final_html = (f'<div style="font-size:12px;font-weight:700;margin-top:3px;color:{rc};">'
                          f'FINAL: {res["actual"]:g} {r["market"].replace("_", " ")} (line {fsn["line"]}, {r["bet"]}) '
                          f'→ {out.upper()} {unit}</div>')
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
            + (f'<span style="background:#15803d;color:#fff;font-size:10px;font-weight:700;padding:1px 7px;'
               f'border-radius:3px;">⭐ {_stale_txt(peak(r) if two_plus else fsn["stale"])}</span>'
               if (peak(r) >= MIN_OFF_2 if two_plus else fsn["stale"] >= MIN_OFF) else "")
            +
            f'<span style="font-size:11px;color:#6b7280;">first seen {x["seen"].strftime("%I:%M %p").lstrip("0")} ET'
            f'{f" · {lead:.1f} h before tip" if lead is not None else ""}</span></div>'
            f'<div style="font-size:11px;color:#6b7280;">Pinnacle {fsn["pin_line"]} · '
            f'{_stale_txt(fsn["stale"])}</div>{final_html}</div>',
            unsafe_allow_html=True,
        )


def render_results(sport):
    graded_all = [r for r in all_recs if r["sport"] == sport
                  and (r.get("result") or {}).get("outcome") in ("win", "loss", "push")]
    mine = [r for r in all_recs if r["sport"] == sport and keep(r)]
    recs = [r for r in mine if (r.get("result") or {}).get("outcome") in ("win", "loss", "push")]
    st.markdown(f"**{len(recs)} graded of {len(mine)} logged** across {len(logs)} days"
                + (f" (only plays with {MIN_OFF_2}+ books stale)" if two_plus else "")
                + (f" (only plays with {MIN_OFF}+ books stale{', no points props' if only4_np else ''})" if only4 or only4_np else "")
                + ". Groups under 30 plays are too small to trust.")
    if not graded_all:
        st.info("None graded yet. The auto snapshot grades them a few hours after each game ends.")
        return

    def show(title, keyf, order=None, rows=None):
        agg = fs.summarize(recs if rows is None else rows, keyf)
        if not agg:
            return
        keys = [k for k in (order or sorted(agg, key=str)) if k in agg]
        table = [{"Group": k, "Plays": agg[k]["n"], "W-L-P": f"{agg[k]['w']}-{agg[k]['l']}-{agg[k]['p']}",
                  "Win %": pct(agg[k]["w"], agg[k]["l"]), "Units": round(agg[k]["units"], 1),
                  "ROI %": round(agg[k]["units"] / agg[k]["n"] * 100, 1),
                  "Sample": "small" if agg[k]["n"] < 30 else ""} for k in keys]
        st.markdown(f"#### {title}")
        st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)

    # Always compares all graded plays, whatever the filter above is set to.
    def _grp(r):
        if r["first_seen"]["stale"] < MIN_OFF:
            return f"fewer than {MIN_OFF}"
        return f"{MIN_OFF}+ off, points props" if r.get("market") == "points" else f"{MIN_OFF}+ off, no points"
    show(f"⭐ {MIN_OFF}+ books stale vs fewer", _grp,
         [f"{MIN_OFF}+ off, no points", f"{MIN_OFF}+ off, points props", f"fewer than {MIN_OFF}"], rows=graded_all)
    show(f"⭐ {MIN_OFF_2}+ books stale vs 1 book stale",
         lambda r: f"{MIN_OFF_2}+ books stale" if peak(r) >= MIN_OFF_2 else "1 book stale",
         [f"{MIN_OFF_2}+ books stale", "1 book stale"], rows=graded_all)
    if not recs:
        st.info(f"No graded plays with {MIN_OFF}+ books stale yet for {sport}.")
        return
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
    show("By books stale (still off Pinnacle)",
         lambda r: str(min(r["first_seen"]["stale"], 5)) + ("+" if r["first_seen"]["stale"] >= 5 else ""))


days = sorted(set(logs) | {today}, reverse=True)
def _day_label(d):
    txt = datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %d").replace(" 0", " ")
    return txt + (" (today)" if d == today else " (upcoming)" if d > today else "")


day = st.selectbox("Date", days, index=days.index(today), key="fs_day", format_func=_day_label)
_books = st.radio("Plays", ["All plays", f"⭐ {MIN_OFF_2}+ books stale only",
                            f"⭐ {MIN_OFF}+ books stale only",
                            f"⭐ {MIN_OFF}+ books stale, no points props"], horizontal=True, key="fs_books",
                  help=f"A book is stale when its line is off Pinnacle's (an Over line below it). In the first week, plays with {MIN_OFF}+ stale books went 41-23 (+10.2u, +16%); "
                       "plays with fewer than 4 lost money. 2+ is the filter for sports with few books (NHL has 3): NHL shots "
                       "Overs went 18-7 (+3.5u) at 2+ books stale vs 30-19 (-2.8u) at 1. The Results tab shows how this keeps going.")
two_plus = _books.startswith(f"⭐ {MIN_OFF_2}+")
only4_np = _books.endswith("no points props")
only4 = _books.startswith(f"⭐ {MIN_OFF}+") and not only4_np
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
