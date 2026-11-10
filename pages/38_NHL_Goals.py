"""
🏒 NHL Goals — does a shots-on-goal Over plus shooting percentage predict a goal?

Only players with a shots-on-goal Over line split (the play on First Seen and Player Props) are listed. For each one the
tracker logs the expected shots (from Pinnacle's shots line), the player's own goals per shot, the resulting chance of a
goal and the best anytime-goal price (best_plays_log/goal_tracker/<date>.json). The hourly job grades them after the game.
"""
import html
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
from scripts import goal_tracker as gt  # noqa: E402

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

st.set_page_config(page_title="NHL Goals", page_icon="🏒", layout="wide")
st.title("🏒 NHL Goals — shots Over → chance of a goal")
st.caption("Only players with a shots-on-goal Over line split. Chance of a goal = 1 − e^(−expected shots × goals per "
           "shot). Expected shots come from Pinnacle's shots line; goals per shot is the player's own, pulled toward the "
           "league average when he has few shots. EV is at the best anytime-goal price. Times are Eastern.")

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
    for item in _retry(lambda: gh.list_dir(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{gt.LOG_SUB}")):
        name = item["name"]
        if not name.endswith(".json"):
            continue
        try:
            out[name[:-5]] = _retry(lambda: gh.load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/{gt.LOG_SUB}/{name}")) or {}
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
    st.info("Nothing logged yet. It fills in after the next hourly NHL snapshot, once the shooting data is saved "
            "(run the NHL shooting backfill job from the Actions tab first).")
    st.stop()

now = datetime.now(tz=EASTERN)
all_recs = [r for d in sorted(logs) for r in logs[d].values()]
graded = [r for r in all_recs if (r.get("result") or {}).get("outcome") in ("win", "loss")]
pct = lambda x: f"{100 * x:.0f}%"


def _stale_txt(n):
    return f"{n} book{'s' if n != 1 else ''} stale"


tab_res, tab_day = st.tabs(["Results", "Players by day"])
with tab_res:
    st.markdown(f"**{len(graded)} graded of {len(all_recs)} logged** across {len(logs)} days. "
                "Groups under 30 players are too small to trust.")
    if not graded:
        st.info("None graded yet. The hourly job grades them after each game ends.")
    else:
        n = len(graded)
        hit = sum(r["result"]["outcome"] == "win" for r in graded) / n
        exp = sum(r["first_seen"]["chance"] for r in graded) / n
        imp = sum(bp.implied(r["first_seen"]["price"]) for r in graded) / n
        st.markdown("#### Is the chance right? (our estimate vs what happened vs the price)")
        st.dataframe(pd.DataFrame([{"Players": n, "Our chance": pct(exp), "Actually scored": pct(hit),
                                    "Price implies": pct(imp), "Difference (scored - ours)": f"{100 * (hit - exp):+.0f} pts"}]),
                     hide_index=True, use_container_width=True)

        def ev_bucket(r):
            e = r["first_seen"]["ev"]
            return ("1. Our chance beats the price by 10%+" if e >= 0.10 else "2. Beats the price (0 to 10%)" if e >= 0
                    else "3. Below the price (0 to -10%)" if e >= -0.10 else "4. Well below the price (-10% or worse)")

        def sample_bucket(r):
            s = r["first_seen"].get("sample_shots") or 0
            return "1. 100+ shots of data" if s >= 100 else "2. 40 to 99 shots" if s >= 40 else "3. Under 40 shots"

        for title, keyf in (("By how our chance compares with the price", ev_bucket),
                            ("By books stale on the shots Over", lambda r: _stale_txt(r["first_seen"].get("stale") or 0)),
                            ("By how much shooting data the player has", sample_bucket)):
            agg = fs.summarize(graded, keyf)
            rows = [{"Group": k, "Players": agg[k]["n"], "Scored": agg[k]["w"],
                     "Win %": pct(agg[k]["w"] / agg[k]["n"]), "Units": round(agg[k]["units"], 1),
                     "ROI %": round(agg[k]["units"] / agg[k]["n"] * 100, 1),
                     "Sample": "small" if agg[k]["n"] < 30 else ""} for k in sorted(agg)]
            st.markdown(f"#### {title}")
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

with tab_day:
    day = st.selectbox("Date", sorted(logs, reverse=True), key="ng_day")
    items = []
    for r in logs[day].values():
        f, l, res = r["first_seen"], r.get("last_seen") or r["first_seen"], r.get("result") or {}
        items.append({"r": r, "f": f, "l": l, "res": res, "tip": fs._parse(r["commence_time"]).astimezone(EASTERN)})
    items.sort(key=lambda x: -x["l"]["ev"])
    st.caption(f"{len(items)} players with a shots Over split and an anytime-goal price. Sorted by EV.")
    view = st.radio("View", ["Cards", "Table"], horizontal=True, key="ng_view")
    if view == "Table":
        rows = [{"Player": x["r"]["who"], "Game": x["r"]["game"], "Tip": x["tip"].strftime("%I:%M %p").lstrip("0"),
                 "Our chance": pct(x["l"]["chance"]), "Price": f"{x['l']['price']:+d} {bp.BOOK_DISPLAY.get(x['l']['book'], x['l']['book'])}",
                 "EV": f"{100 * x['l']['ev']:+.0f}%", "Exp shots": x["l"]["exp_shots"],
                 "Goals/shot": f"{100 * x['l']['rate']:.1f}% ({x['l']['sample_shots']} shots)",
                 "Shots Over": f"{x['l']['shots_line']} · {_stale_txt(x['l'].get('stale') or 0)}",
                 "Result": {"win": "✅ scored", "loss": "❌ no goal"}.get(x["res"].get("outcome"), "pending")} for x in items]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        for x in items:
            r, l, res = x["r"], x["l"], x["res"]
            ev = l["ev"]
            if ev >= 0.10:
                bg, border, colour = "#f0fdf4", "#86efac", "#15803d"
            elif ev >= 0:
                bg, border, colour = "#f7fee7", "#bef264", "#4d7c0f"
            else:
                bg, border, colour = "#f9fafb", "#e5e7eb", "#6b7280"
            out = res.get("outcome")
            if out in ("win", "loss"):
                rc = "#15803d" if out == "win" else "#b91c1c"
                badge = (f'<span style="background:{rc};color:#fff;font-size:10px;font-weight:700;padding:1px 6px;'
                         f'border-radius:3px;margin-right:6px;">{"SCORED" if out == "win" else "NO GOAL"}</span>')
            elif x["tip"] <= now:
                badge = ('<span style="background:#9ca3af;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;'
                         'border-radius:3px;margin-right:6px;">STARTED</span>')
            else:
                badge = ""
            book = bp.BOOK_DISPLAY.get(l["book"], l["book"])
            fair_odds = (1 - l["chance"]) / l["chance"] * 100
            st.markdown(
                f'<div style="background:{bg};border:1.5px solid {border};border-radius:8px;padding:10px 14px;'
                f'margin-bottom:6px;color:#111827;">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
                f'<span style="font-size:14px;font-weight:700;">{html.escape(r["who"])}'
                f'<span style="font-size:11px;color:#6b7280;font-weight:400;"> — {html.escape(r["game"])}</span></span>'
                f'<span style="font-size:11px;color:#6b7280;white-space:nowrap;">{badge}'
                f'{x["tip"].strftime("%I:%M %p").lstrip("0")} ET</span></div>'
                f'<div style="display:flex;align-items:baseline;gap:10px;margin:2px 0;flex-wrap:wrap;">'
                f'<span style="font-size:11px;color:#6b7280;">ANYTIME GOAL</span>'
                f'<span style="font-size:18px;font-weight:800;color:{colour};">{l["price"]:+d}</span>'
                f'<span style="font-size:13px;">@ <strong>{html.escape(book)}</strong></span>'
                f'<span style="background:#fff;border:1px solid {border};color:{colour};font-size:11px;'
                f'font-weight:700;padding:1px 7px;border-radius:3px;">EV {100 * ev:+.0f}%</span></div>'
                f'<div style="font-size:11px;color:#6b7280;margin-bottom:4px;">Our chance {pct(l["chance"])} '
                f'(fair +{fair_odds:.0f}) = {l["exp_shots"]} expected shots × {100 * l["rate"]:.1f}% goals per shot '
                f'({l["sample_shots"]} shots, {l["sample_games"]} games)</div>'
                f'<div style="display:flex;gap:6px;flex-wrap:wrap;"><span style="background:#fff;border:1px solid {border};'
                f'color:{colour};font-size:10px;font-weight:700;padding:1px 7px;border-radius:3px;">Shots Over '
                f'{l["shots_line"]} · {_stale_txt(l.get("stale") or 0)}</span></div>'
                f'</div>', unsafe_allow_html=True)
