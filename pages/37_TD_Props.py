"""
🏈 TD Props — anytime-TD prices against Pinnacle, with a record.

For every player Pinnacle prices (NFL/CFB), the hourly snapshot logs Pinnacle's fair chance of a TD, the best
soft-book price and its expected value (best_plays_log/td_props/<date>.json). The grader settles them after the
game. Results show whether Pinnacle-priced players score as often as Pinnacle says, and whether betting the
best soft-book price makes money.
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
ODDS_KEY = _resolve_secret("THE_ODDS_API_KEY")

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


def pull_live(sport):
    """Fetch live TD prices now and log them (same log the hourly job writes). Returns (new players, quota, errors)."""
    from data import github_storage as gh
    now_et = datetime.now(tz=EASTERN)
    snap, quota = tdp.fetch_live(ODDS_KEY, sport, now_et)
    date = now_et.strftime("%Y-%m-%d")
    path = f"{LOG_DIR}/{tdp.LOG_SUB}/{date}.json"
    log = gh.load_json(GH_TOKEN, OWNER, REPO, path) or {}
    new = tdp.update_log(log, tdp.detect_plays(sport, snap, now_et), now_et.isoformat(), date)
    gh.save_json(GH_TOKEN, OWNER, REPO, path, log, commit_msg=f"TD props {date}: {new} new (live refresh {sport})")
    return new, quota, snap.get("errors", [])


_c1, _c2, _c3 = st.columns([1, 1, 3])
with _c1:
    _sport = st.radio("Sport", ["NFL", "CFB"], horizontal=True, key="td_live_sport", label_visibility="collapsed")
with _c2:
    _go = st.button("🔄 Refresh live odds", type="primary", use_container_width=True, key="td_live_btn")
with _c3:
    st.caption("Pulls today's live anytime-TD prices from the odds service (about 2 credits per market per game) and "
               "adds them to the log below.")
if _go:
    if not ODDS_KEY:
        st.error("Missing `THE_ODDS_API_KEY` in secrets.")
    else:
        with st.spinner(f"Fetching live {_sport} TD odds…"):
            try:
                _new, _quota, _errs = pull_live(_sport)
                load_all.clear()
                st.success(f"Logged {_new} new players. Odds credits left: {_quota}.")
                for _e in _errs[:3]:
                    st.warning(_e)
            except Exception as e:  # noqa: BLE001
                st.error(f"Refresh failed: {e}")

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

def _prop(p):
    """'68.5 (-101/-125)' = Pinnacle's line and Over/Under prices; '[66.5-68.5]' = range of the soft books' lines."""
    if not p:
        return ""
    s = f"{p['line']:g}"
    if p.get("over") is not None and p.get("under") is not None:
        s += f" ({p['over']:+d}/{p['under']:+d})"
    if p.get("lo") is not None and (p["lo"] != p["line"] or p["hi"] != p["line"]):
        s += f" [{p['lo']:g}-{p['hi']:g}]"
    return s


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

        def split_bucket(r):
            ps = [(r.get("last_seen") or {}).get("props"), (r.get("first_seen") or {}).get("props")]
            n = max((tdp.over_split(p)[0] for p in ps if p), default=None)
            has = any(v.get("over_off") is not None for p in ps if p for v in p.values())
            if not has:
                return "4. No split data (older records)"
            return ("1. Over rec/rush yds, 3+ books stale" if n >= tdp.SPLIT_MIN else
                    "2. Over rec/rush yds, 1 to 2 books stale" if n >= 1 else "3. No Over split")

        for title, keyf in (("By Over line split on receiving or rushing yards", split_bucket),
                            ("Betting the best soft-book price, by how it compares with Pinnacle", ev_bucket),
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
    items = []
    for r in logs[day].values():
        if r["sport"] != sport:
            continue
        f, l, res = r["first_seen"], r.get("last_seen") or r["first_seen"], r.get("result") or {}
        items.append({"r": r, "f": f, "l": l, "res": res, "pr": l.get("props") or {},
                      "tip": fs._parse(r["commence_time"]).astimezone(EASTERN)})
    for x in items:
        x["split"], x["split_key"] = tdp.over_split(x["pr"])
    only_over = st.checkbox("Only players with an Over line split (receiving yds, rushing yds or receptions)",
                            value=True, key="td_only_over",
                            help="Shows a player only when at least one soft book's line is below Pinnacle's on one of "
                                 "those props. Older records (before this was logged) have no split data and are hidden "
                                 "while this is on.")
    total_priced = len(items)
    if only_over:
        items = [x for x in items if tdp.any_over(x["pr"]) >= 1]
        st.caption(f"Showing {len(items)} of {total_priced} players priced by Pinnacle.")
        if not items:
            st.info("No players with an Over line split right now. Turn the switch off to see everyone priced by Pinnacle.")
    items.sort(key=lambda x: (-int(x["split"] >= tdp.SPLIT_MIN), -x["split"], -x["l"]["fair"]))
    st.caption(f"{len(items)} players. Ranked by the strongest Over line split on receiving or "
               f"rushing yards (soft books below Pinnacle's line; {tdp.SPLIT_MIN}+ books stale is the group that has won), "
               "then by chance to score. The TD price and EV are info only: in week 1 the 'price beats Pinnacle' plays "
               "lost, because Pinnacle's chances were accurate. Rush yds, Rec yds and Receptions show Pinnacle's line "
               "with its Over/Under prices; the range in brackets is where the soft books' lines sit.")
    view = st.radio("View", ["Cards", "Table"], horizontal=True, key="td_view")
    if view == "Table":
        rows = [{"Over split": (f"{x['split']} book{'s' if x['split'] != 1 else ''} stale ({'Rec' if x['split_key'] == 'rec_yds' else 'Rush'} yds)"
                                if x["split"] else ""),
                 "Player": x["r"]["who"], "Game": x["r"]["game"], "EV (info)": f"{100 * x['l']['ev']:+.1f}%",
                 "Tip": x["tip"].strftime("%a %I:%M %p").replace(" 0", " "),
                 "Pinnacle": f"{x['l']['pin_over']:+d} / {x['l']['pin_under']:+d}", "Fair": pct(x["l"]["fair"]),
                 "Best price": f"{x['l']['price']:+d} {bp.BOOK_DISPLAY.get(x['l']['book'], x['l']['book'])}",
                 "Rush yds": _prop(x["pr"].get("rush_yds")), "Rec yds": _prop(x["pr"].get("rec_yds")),
                 "Receptions": _prop(x["pr"].get("recs")),
                 "Result": {"win": "✅ scored", "loss": "❌ no TD"}.get(x["res"].get("outcome"), "pending")}
                for x in items]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        for x in items:
            r, l, res = x["r"], x["l"], x["res"]
            ev = l["ev"]
            if x["split"] >= tdp.SPLIT_MIN:
                bg, border, colour = "#f0fdf4", "#86efac", "#15803d"       # Over split with 3+ books off
            elif x["split"] == 2:
                bg, border, colour = "#f7fee7", "#bef264", "#4d7c0f"
            else:
                bg, border, colour = "#f9fafb", "#e5e7eb", "#6b7280"
            split_html = (f'<span style="background:#15803d;color:#fff;font-size:10px;font-weight:700;padding:1px 7px;'
                          f'border-radius:3px;">⭐ Over {"rec" if x["split_key"] == "rec_yds" else "rush"} yds · '
                          f'{x["split"]} book{"s" if x["split"] != 1 else ""} stale</span>'
                          if x["split"] >= tdp.SPLIT_MIN else
                          f'<span style="background:#fff;border:1px solid {border};color:{colour};font-size:10px;'
                          f'font-weight:700;padding:1px 7px;border-radius:3px;">Over {"rec" if x["split_key"] == "rec_yds" else "rush"} yds · '
                          f'{x["split"]} book{"s" if x["split"] != 1 else ""} stale</span>' if x["split"] else "")
            out = res.get("outcome")
            if out in ("win", "loss"):
                rc = "#15803d" if out == "win" else "#b91c1c"
                badge = (f'<span style="background:{rc};color:#fff;font-size:10px;font-weight:700;padding:1px 6px;'
                         f'border-radius:3px;margin-right:6px;">{"SCORED" if out == "win" else "NO TD"}</span>')
            elif x["tip"] <= now:
                badge = ('<span style="background:#9ca3af;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;'
                         'border-radius:3px;margin-right:6px;">STARTED</span>')
            else:
                badge = ""
            chips = "".join(
                f'<span style="background:#fff;border:1px solid #e5e7eb;border-radius:3px;font-size:11px;'
                f'padding:1px 6px;color:#374151;">{label} {_prop(x["pr"].get(key))}</span>'
                for label, key in (("Rush", "rush_yds"), ("Rec yds", "rec_yds"), ("Recs", "recs"))
                if x["pr"].get(key))
            book = bp.BOOK_DISPLAY.get(l["book"], l["book"])
            fair_odds = (1 - l["fair"]) / l["fair"] * 100
            st.markdown(
                f'<div style="background:{bg};border:1.5px solid {border};border-radius:8px;padding:10px 14px;'
                f'margin-bottom:6px;color:#111827;">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
                f'<span style="font-size:14px;font-weight:700;">{html.escape(r["who"])}'
                f'<span style="font-size:11px;color:#6b7280;font-weight:400;"> — {html.escape(r["game"])}</span></span>'
                f'<span style="font-size:11px;color:#6b7280;white-space:nowrap;">{badge}'
                f'{x["tip"].strftime("%a %I:%M %p").replace(" 0", " ")} ET</span></div>'
                f'<div style="display:flex;align-items:baseline;gap:10px;margin:2px 0;flex-wrap:wrap;">'
                f'<span style="font-size:11px;color:#6b7280;">ANYTIME TD</span>'
                f'<span style="font-size:18px;font-weight:800;color:{colour};">{l["price"]:+d}</span>'
                f'<span style="font-size:13px;">@ <strong>{html.escape(book)}</strong></span>'
                f'<span style="font-size:11px;color:#6b7280;">EV {100 * ev:+.1f}% (info)</span></div>'
                f'<div style="font-size:11px;color:#6b7280;margin-bottom:4px;">Pinnacle {l["pin_over"]:+d} / '
                f'{l["pin_under"]:+d} · fair {pct(l["fair"])} (+{fair_odds:.0f})</div>'
                f'<div style="display:flex;gap:6px;flex-wrap:wrap;">{split_html}{chips}</div>'
                f'</div>', unsafe_allow_html=True)
