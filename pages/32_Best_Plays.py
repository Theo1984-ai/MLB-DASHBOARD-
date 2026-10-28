"""
🎯 Best Plays — one snapshot across every sport with a game today.

Pulls player props + team totals for NHL / NBA / NFL / CFB (only sports with an
open game today), ranks the line splits against Pinnacle by estimated edge,
and logs every pick so we can see what actually works over time.
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

from scripts import best_plays as bp  # noqa: E402

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


ODDS_KEY = _resolve_secret("THE_ODDS_API_KEY")
GH_TOKEN = _resolve_secret("GITHUB_TOKEN")

st.set_page_config(page_title="Best Plays", page_icon="🎯", layout="wide")
st.title("🎯 Best Plays — every sport with a game today")
st.caption(
    "One click pulls props, team totals and Pinnacle's game lines, ranks line splits vs Pinnacle by "
    "estimated edge, checks each pick against the game market, and logs every pick to track what "
    "works. Edge is an estimate — it can't see injuries, lineups or minutes."
)

if not ODDS_KEY:
    st.error("Missing `THE_ODDS_API_KEY` in secrets.")
    st.stop()

# ── Thresholds ────────────────────────────────────────────────────────────────
with st.expander("⚙️ Thresholds (what counts as a play)"):
    c1, c2, c3, c4, c5 = st.columns(5)
    min_edge = c1.number_input("Min estimated edge %", 0.0, 20.0, bp.DEFAULT_TH["min_edge"] * 100, 0.5)
    min_stale = c2.number_input("Min books off Pinnacle", 1, 10, bp.DEFAULT_TH["min_stale"], 1)
    min_gap = c3.number_input("Min line gap", 0.5, 20.0, bp.DEFAULT_TH["min_gap"], 0.5)
    max_juice = c4.number_input("Worst price (-N)", 100, 400, bp.DEFAULT_TH["max_juice"], 5,
                                help="140 means nothing worse than -140")
    max_per_game = c5.number_input("Max picks per game", 0, 10, 2, 1,
                                   help="Picks in one game are tied together. Keeps the best by "
                                        "adjusted edge. 0 = no cap.")
TH = {"min_edge": min_edge / 100, "min_stale": int(min_stale),
      "min_gap": float(min_gap), "max_juice": int(max_juice), "max_per_game": int(max_per_game)}


# ── GitHub helpers ────────────────────────────────────────────────────────────

def _gh():
    from data import github_storage as gh
    return gh


def load_log(date):
    return _gh().load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/picks/{date}.json") or {}


def save_log(date, log, note):
    _gh().save_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/picks/{date}.json", log,
                    commit_msg=f"Best plays log {date}: {note}")


def grade_recent(now_et, days=7):
    """Grade finished picks from the last few days and save any that changed."""
    gh = _gh()
    totals = {"graded": 0, "pending": 0, "no_data": 0, "errors": []}
    have = {i["name"] for i in gh.list_dir(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/picks")}
    for i in range(days):
        d = (now_et - timedelta(days=i)).strftime("%Y-%m-%d")
        if f"{d}.json" not in have:
            continue
        log = load_log(d)
        counts = bp.grade_log(log, now_et)
        for k in ("graded", "pending", "no_data"):
            totals[k] += counts[k]
        totals["errors"] += counts["errors"]
        if counts["graded"] or counts["no_data"]:
            save_log(d, log, f"graded {counts['graded']} picks")
    return totals


@st.cache_data(ttl=120, show_spinner=False)
def load_saved_snaps(date, _token_present):
    """
    Per sport, for `date`: the latest non-empty saved snapshot (skips empty ones, e.g. when the
    credits ran out), Pinnacle's game lines from the same run, and the day's first non-empty
    totals snapshot (used to see how the game market has moved since).
    """
    gh = _gh()
    folder = f"{LOG_DIR}/snapshots/{date}"
    files = {}
    for item in gh.list_dir(GH_TOKEN, OWNER, REPO, folder):
        m = re.fullmatch(r"([A-Z]+)_(\d{4})_(props|totals|game)\.json", item["name"])
        if m:
            files.setdefault((m[1], m[2]), {})[m[3]] = item["name"]

    def load(sport, hhmm, kind):
        name = files.get((sport, hhmm), {}).get(kind)
        return (gh.load_json(GH_TOKEN, OWNER, REPO, f"{folder}/{name}") or {}) if name else None

    out = {}
    for sport in bp.SPORTS:
        times = sorted({h for (sp, h) in files if sp == sport})
        for hhmm in reversed(times):
            props, totals = load(sport, hhmm, "props"), load(sport, hhmm, "totals")
            if any((x or {}).get("games") for x in (props, totals)):
                out[sport] = {"hhmm": hhmm, "props_snap": props, "tt_snap": totals,
                              "game_snap": load(sport, hhmm, "game"), "first_tt": None}
                break
        if sport in out:
            for hhmm in times:                      # earliest non-empty totals snapshot of the day
                t = load(sport, hhmm, "totals")
                if (t or {}).get("games"):
                    out[sport]["first_tt"] = t
                    break
    return out


@st.cache_data(ttl=600, show_spinner=False)
def load_rosters(_token_present):
    gh = _gh()
    return {sport: gh.load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/rosters/{sport}.json") or {}
            for sport in bp.SPORTS}


# ── Snapshot run ──────────────────────────────────────────────────────────────

def run_snapshot():
    now_et = datetime.now(tz=EASTERN)
    date, hhmm = now_et.strftime("%Y-%m-%d"), now_et.strftime("%H%M")
    out = {"time": now_et, "sports": {}, "plays": [], "errors": [], "saved": [], "quota": "?",
           "graded": None, "snaps": {}}
    snaps = {}
    for sport in bp.SPORTS:
        try:
            res, quota = bp.snapshot_sport(ODDS_KEY, sport, now_et)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{sport}: {e}")
            continue
        out["quota"] = quota
        if not res:
            out["sports"][sport] = {"games": 0, "plays": 0}
            continue
        snaps[sport] = res
        # Player -> team (cached on GitHub) and the day's first totals snapshot, for the game-market check.
        roster_cache, first_tt = {}, None
        if GH_TOKEN:
            try:
                teams = {t for g in res["tt_snap"]["games"] + res["props_snap"]["games"]
                         for t in (g["away"], g["home"])}
                path = f"{LOG_DIR}/rosters/{sport}.json"
                roster_cache = _gh().load_json(GH_TOKEN, OWNER, REPO, path) or {}
                roster_cache, r_errs, changed = bp.espn_rosters(sport, teams, roster_cache)
                if changed:
                    _gh().save_json(GH_TOKEN, OWNER, REPO, path, roster_cache,
                                    commit_msg=f"Roster cache {sport}")
                out["errors"] += r_errs[:2]
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"{sport} rosters: {e}")
            try:
                first_tt = load_saved_snaps(date, True).get(sport, {}).get("first_tt")
            except Exception:  # noqa: BLE001
                first_tt = None
        bundle = {"props_snap": res["props_snap"], "tt_snap": res["tt_snap"],
                  "game_snap": res["game_snap"], "first_tt": first_tt or res["tt_snap"],
                  "roster": bp.roster_lookup(roster_cache)}
        out["snaps"][sport] = bundle
        plays = bp.annotate(bp.find_plays(sport, res["props_snap"], res["tt_snap"], TH), sport,
                            res["tt_snap"], bundle["first_tt"], res["game_snap"], bundle["roster"])
        out["sports"][sport] = {"games": res["n_games"], "plays": len(plays)}
        out["errors"] += res["errors"]
        out["plays"] += plays
    out["plays"].sort(key=lambda p: -p["adj"])

    if not GH_TOKEN:
        out["errors"].append("GITHUB_TOKEN not configured — snapshot and log were NOT saved.")
        return out
    gh = _gh()
    try:
        # One file per snapshot, so nothing is ever read-modified-overwritten.
        for sport, res in snaps.items():
            for kind, snap in (("props", res["props_snap"]), ("totals", res["tt_snap"]),
                               ("game", res["game_snap"])):
                path = f"{LOG_DIR}/snapshots/{date}/{sport}_{hhmm}_{kind}.json"
                gh.save_json(GH_TOKEN, OWNER, REPO, path, snap,
                             commit_msg=f"Best plays snapshot {sport} {kind} {date} {hhmm}")
                out["saved"].append(path)
        log = load_log(date)
        iso = now_et.isoformat()
        new = bp.update_log(log, out["plays"], iso, date)
        checked = sum(bp.add_checks(log, r["props_snap"], r["tt_snap"], iso) for r in snaps.values())
        save_log(date, log, f"{new} new picks, {checked} checks")
        out["saved"].append(f"{LOG_DIR}/picks/{date}.json")
        out["new_picks"], out["checked"] = new, checked
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"GitHub save failed: {e}")
    try:
        out["graded"] = grade_recent(now_et)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"Grading failed: {e}")
    return out


# ── Buttons ───────────────────────────────────────────────────────────────────
b1, b2, _ = st.columns([2, 2, 4])
snap_btn = b1.button("📸 Snapshot all sports & find plays", type="primary", use_container_width=True)
grade_btn = b2.button("✅ Grade finished picks", use_container_width=True,
                      disabled=not GH_TOKEN)

if snap_btn:
    with st.spinner("Pulling today's games and ranking plays…"):
        st.session_state["bp_run"] = run_snapshot()
        st.cache_data.clear()
if grade_btn:
    with st.spinner("Grading finished games from ESPN box scores…"):
        try:
            st.session_state["bp_grade"] = grade_recent(datetime.now(tz=EASTERN))
            st.cache_data.clear()
        except Exception as e:  # noqa: BLE001
            st.session_state["bp_grade"] = {"errors": [str(e)], "graded": 0, "pending": 0, "no_data": 0}

tab_today, tab_track = st.tabs(["🎯 Today's plays", "📈 Track record"])

# ── Today's plays ─────────────────────────────────────────────────────────────

def _open_games(snap, now_et):
    names = set()
    for g in (snap or {}).get("games", []):
        if datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")) > now_et:
            names.add(g["game"])
    return names


def build_view():
    """
    What to show: the fresh run if there is one, otherwise the latest saved snapshot per sport
    (so the plays are still there when you come back). Plays are re-ranked with the current
    thresholds, and games that have already started are hidden.
    """
    now_et = datetime.now(tz=EASTERN)
    run = st.session_state.get("bp_run")
    view = {"errors": [], "saved": [], "graded": None, "quota": None, "idle": [], "hidden": 0,
            "from_saved": run is None, "stamp": "", "sports": {}, "plays": []}
    if run:
        snaps = run["snaps"]
        view.update(errors=run["errors"], saved=run.get("saved", []), graded=run.get("graded"),
                    quota=run["quota"], stamp=f"Snapshot {run['time'].strftime('%I:%M %p ET')}",
                    idle=[s for s, v in run["sports"].items() if not v["games"]])
    elif GH_TOKEN:
        try:
            snaps = load_saved_snaps(now_et.strftime("%Y-%m-%d"), True)
        except Exception as e:  # noqa: BLE001
            snaps = {}
            view["errors"].append(f"Couldn't load the saved snapshot: {e}")
        stamps = [f"{s} {datetime.strptime(v['hhmm'], '%H%M').strftime('%I:%M %p')}"
                  for s, v in snaps.items()]
        view["stamp"] = "Latest saved snapshot (" + " · ".join(stamps) + " ET)" if stamps else ""
    else:
        snaps = {}
    rosters = {}
    if view["from_saved"] and GH_TOKEN and snaps:
        try:
            rosters = load_rosters(True)
        except Exception:  # noqa: BLE001
            rosters = {}
    for sport, s in snaps.items():
        roster = s.get("roster") if "roster" in s else bp.roster_lookup(rosters.get(sport))
        found = bp.annotate(bp.find_plays(sport, s["props_snap"], s["tt_snap"], TH), sport,
                            s["tt_snap"], s.get("first_tt") or s["tt_snap"], s.get("game_snap"), roster)
        open_plays = [p for p in found if datetime.fromisoformat(
            p["commence_time"].replace("Z", "+00:00")) > now_et]
        view["hidden"] += len(found) - len(open_plays)
        view["plays"] += open_plays
        games = _open_games(s["props_snap"], now_et) | _open_games(s["tt_snap"], now_et)
        view["sports"][sport] = {"games": len(games), "plays": len(open_plays)}
    capped = bp.cap_per_game(view["plays"], TH["max_per_game"])
    view["capped"] = len(view["plays"]) - len(capped)
    view["plays"] = capped
    return view


with tab_today:
    view = build_view()
    if not view["sports"] and not st.session_state.get("bp_run"):
        st.info("No saved snapshot for today yet. Press **📸 Snapshot all sports & find plays**. "
                "Only sports with an open game today are pulled (MLB is off).")
    else:
        playing = [bp.SPORTS[s]["label"] + f" ({v['games']} games · {v['plays']} plays)"
                   for s, v in view["sports"].items() if v["games"]]
        st.markdown(f"**{view['stamp']}** · {len(view['plays'])} plays"
                    + (f" · API quota left **{view['quota']}**" if view["quota"] is not None else ""))
        if view["from_saved"]:
            st.caption("Loaded from GitHub, so no API credits were used. Press 📸 for fresh lines.")
        if playing:
            st.caption("Pulled: " + " · ".join(playing))
        if view["idle"]:
            st.caption("No open game today (skipped): " + ", ".join(view["idle"]))
        if view.get("capped"):
            st.caption(f"{view['capped']} lower-ranked play(s) hidden by the per-game cap "
                       f"({TH['max_per_game']} per game).")
        if view["hidden"]:
            st.caption(f"{view['hidden']} play(s) hidden because the game has already started.")
        if view["saved"]:
            st.caption(f"Saved to GitHub: {len(view['saved'])} files (snapshots + pick log).")
        if view["graded"]:
            g = view["graded"]
            st.caption(f"Grading: {g['graded']} graded · {g['pending']} waiting on final scores · "
                       f"{g['no_data']} no data.")
        for e in view["errors"]:
            st.warning(e)

        if not view["plays"]:
            st.info("No plays clear the thresholds right now. Loosen them above, or snapshot "
                    "again closer to game time.")
        for p in view["plays"]:
            when = datetime.fromisoformat(p["commence_time"].replace("Z", "+00:00")).astimezone(EASTERN)
            is_over = p["bet"] == "OVER"
            bg, border, colour = ("#f0fdf4", "#86efac", "#15803d") if is_over else ("#eff6ff", "#93c5fd", "#1d4ed8")
            ctx = p.get("ctx") or {}
            tag_txt, tag_col = {"backed": ("✅ backed by game market", "#15803d"),
                                "conflicts": ("⚠️ conflicts with game market", "#b45309"),
                                "neutral": ("➖ game market neutral", "#6b7280")}.get(ctx.get("tag"), (None, None))
            bits = []
            if tag_txt:
                subject = ctx.get("team") if ctx.get("scope") == "team" else "game"
                moved = (f" · {subject} expected score {ctx['shift']:+.1f} since first snapshot"
                         if ctx.get("shift") is not None else "")
                bits.append(f'<span style="color:{tag_col};font-weight:700;">{tag_txt}</span>{moved}')
            if ctx.get("fav") and ctx.get("fav_spread") is not None:
                line = f"{ctx['fav']} {ctx['fav_spread']:+.1f}"
                if ctx.get("fav_win_prob"):
                    line += f" · win {ctx['fav_win_prob'] * 100:.0f}%"
                if ctx.get("total") is not None:
                    line += f" · total {ctx['total']}"
                bits.append(line)
            if ctx.get("blowout"):
                bits.append('<span style="color:#b45309;font-weight:700;">⚠️ blowout risk, starters may sit</span>')
            ctx_html = (f'<div style="font-size:11px;color:#6b7280;margin-top:2px;">{" · ".join(bits)}</div>'
                        if bits else "")
            st.markdown(
                f'<div style="background:{bg};border:1.5px solid {border};border-radius:8px;'
                f'padding:10px 14px;margin-bottom:6px;color:#111827;">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
                f'<span style="font-size:14px;font-weight:700;">{p["who"]}'
                f'<span style="font-size:11px;color:#6b7280;font-weight:400;"> — {p["game"]} · '
                f'{bp.SPORTS[p["sport"]]["label"]}</span></span>'
                f'<span style="font-size:11px;color:#6b7280;">{when.strftime("%a %I:%M %p")} ET</span></div>'
                f'<div style="display:flex;align-items:baseline;gap:10px;margin:2px 0;flex-wrap:wrap;">'
                f'<span style="font-size:11px;color:#6b7280;">{p["market"]}</span>'
                f'<span style="font-size:18px;font-weight:800;color:{colour};">{p["bet"]} {p["line"]}</span>'
                f'<span style="font-size:13px;">@ <strong>{bp.BOOK_DISPLAY.get(p["book"], p["book"])}</strong> '
                f'{p["price"]:+d}</span>'
                f'<span style="background:#fff;border:1px solid {border};color:{colour};font-size:11px;'
                f'font-weight:700;padding:1px 7px;border-radius:3px;">edge ~{p["edge"] * 100:+.1f}%</span>'
                f'<span style="font-size:11px;color:#6b7280;">EV {p["ev"] * 100:+.1f}%</span></div>'
                f'<div style="font-size:11px;color:#6b7280;">Pinnacle {p["pin_line"]} '
                f'({p["pin_over"]:+d}/{p["pin_under"]:+d}) · {p["stale"]} of {p["n_books"]} books off it '
                f'· gap {p["gap"]} · fair {p["fair"] * 100:.0f}% vs break-even '
                f'{bp.implied(p["price"]) * 100:.0f}%</div>{ctx_html}</div>',
                unsafe_allow_html=True,
            )

# ── Track record ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def load_all_logs(_token_present):
    gh = _gh()
    items = [i["name"] for i in gh.list_dir(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/picks")
             if i["name"].endswith(".json")]
    return [(n[:-5], gh.load_json(GH_TOKEN, OWNER, REPO, f"{LOG_DIR}/picks/{n}") or {})
            for n in sorted(items)[-30:]]


with tab_track:
    if "bp_grade" in st.session_state:
        g = st.session_state["bp_grade"]
        st.caption(f"Last grading run: {g['graded']} graded · {g['pending']} waiting on final scores "
                   f"· {g['no_data']} no data.")
        for e in g["errors"]:
            st.warning(e)
    if not GH_TOKEN:
        st.info("Add `GITHUB_TOKEN` to secrets to log and track picks.")
    else:
        try:
            logs = load_all_logs(True)
        except Exception as e:  # noqa: BLE001
            logs = []
            st.warning(f"Couldn't load the pick log: {e}")
        summary = bp.summarize([lg for _, lg in logs]) if logs else None
        if not summary or not summary["n_picks"]:
            st.info("No picks logged yet. Snapshot once, then come back after games finish.")
        else:
            m1, m2, m3 = st.columns(3)
            m1.metric("Picks logged", summary["n_picks"])
            m2.metric("Graded", summary["n_graded"])
            cu = summary["catch_up"]
            m3.metric("Books caught up to Pinnacle",
                      f"{cu['narrowed']}/{cu['n']}" if cu["n"] else "—",
                      help="Picks re-checked at a later snapshot where the gap to Pinnacle narrowed.")
            if summary["by_group"]:
                st.markdown("#### What's working")
                st.dataframe(pd.DataFrame(summary["by_group"]).rename(columns={
                    "group": "Group", "n": "Picks", "wins": "W", "losses": "L", "pushes": "Push",
                    "win_pct": "Win %", "roi_pct": "ROI % (1u flat)"}),
                    hide_index=True, use_container_width=True)
                st.caption("Small samples are noise. Wait for ~100+ graded picks before trusting any row.")
            if cu["n"]:
                st.caption(f"Average gap to Pinnacle: {cu['avg_gap_first']} when first flagged → "
                           f"{cu['avg_gap_last']} at the last re-check.")
            rows = []
            for date, lg in logs:
                for r in lg.values():
                    f, res = r["first_seen"], r.get("result") or {}
                    rows.append({"Date": date, "Sport": r["sport"], "Pick": r["who"],
                                 "Market": r["market"], "Bet": f"{r['bet']} {f['line']}",
                                 "Book": bp.BOOK_DISPLAY.get(f["book"], f["book"]),
                                 "Price": f["price"], "Edge %": round(f["edge"] * 100, 1),
                                 "Game market": (r.get("context") or {}).get("tag", "—"),
                                 "Result": res.get("outcome", "pending"), "Actual": res.get("actual"),
                                 "Game": r["game"]})
            st.markdown("#### All picks")
            st.dataframe(pd.DataFrame(rows).sort_values(["Date", "Edge %"], ascending=[False, False]),
                         hide_index=True, use_container_width=True)
