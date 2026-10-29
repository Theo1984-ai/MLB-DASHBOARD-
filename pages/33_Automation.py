"""
⏱️ Automation — on/off switches for every scheduled (GitHub Actions) job.

Each switch is saved to the repo (automation_config.json; Best Plays keeps its own
best_plays_log/auto_config.json). A scheduled job checks its switch first and does nothing
when it is off, so it uses no Odds API credits. Manual "Run workflow" runs ignore the switches.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")
OWNER, REPO = "Theo1984-ai", "MLB-DASHBOARD-"
CONFIG_PATH = "automation_config.json"           # {"jobs": {key: bool}}
BEST_PLAYS_PATH = "best_plays_log/auto_config.json"  # {"enabled": bool}, also on the Best Plays page

# (key, label, what it does / when it runs). Times are Eastern; the older jobs were written
# for standard time, so during daylight time they run an hour later than their names say.
JOBS = [
    ("nba_line_alert", "NBA line alert", "Team-total snapshot, 3x a day (about noon, 4 PM, 6 PM)"),
    ("nfl_line_alert", "NFL line alert", "Team-total snapshot, 3x a day (about noon, 4 PM, 6 PM)"),
    ("cfb_line_alert", "College football line alert", "Team-total snapshot, 3x a day (about noon, 4 PM, 6 PM)"),
    ("mlb_line_alert", "MLB line alert", "Team-total snapshot, 3x a day. MLB is off-season."),
    ("nhl_line_alert", "NHL line alert", "Team-total snapshot, noon, 4 PM, 6 PM"),
    ("nhl_props", "NHL props snapshot", "Player props, noon, 5 PM, 6 PM"),
    ("nhl_sharp_money", "NHL sharp money", "3x a day (about 7 AM, 11 AM, 1 PM)"),
    ("nhl_team_totals", "NHL team totals", "7 AM and noon"),
    ("nhl_true_prob", "NHL true probability", "Noon, 4 PM, 6 PM"),
    ("sharp_money", "Sharp money", "3x a day (about 7 AM, 11 AM, 1 PM)"),
    ("team_totals", "Team totals", "7 AM and noon"),
]


def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


GH_TOKEN = _resolve_secret("GITHUB_TOKEN")


def _gh():
    from data import github_storage as gh
    return gh


st.set_page_config(page_title="Automation", page_icon="⏱️", layout="centered")
st.title("⏱️ Automation — scheduled jobs")
st.caption(
    "Turn each scheduled job on or off. An off job still wakes up on its schedule but does nothing, "
    "so it uses no Odds API credits. Running a job by hand from GitHub's Actions tab ignores these switches. "
    "GitHub starts scheduled jobs late and sometimes skips them."
)

if not GH_TOKEN:
    st.error("Missing `GITHUB_TOKEN` in secrets, so the switches can't be saved.")
    st.stop()


def _load_config():
    try:
        jobs = (_gh().load_json(GH_TOKEN, OWNER, REPO, CONFIG_PATH) or {}).get("jobs", {})
    except Exception as e:  # noqa: BLE001
        st.session_state["au_err"] = f"Couldn't read the saved settings: {e}"
        jobs = {}
    try:
        bp_on = bool((_gh().load_json(GH_TOKEN, OWNER, REPO, BEST_PLAYS_PATH) or {}).get("enabled", True))
    except Exception:  # noqa: BLE001
        bp_on = True
    return jobs, bp_on


# Widget state can be dropped when you leave the page, so fill in whatever is missing from the saved settings.
need = [k for k, _, _ in JOBS if f"au_{k}" not in st.session_state]
if need or "bp_auto_on" not in st.session_state:
    jobs, bp_on = _load_config()
    for key in need:
        st.session_state[f"au_{key}"] = jobs.get(key) is not False   # missing = on
    if "bp_auto_on" not in st.session_state:
        st.session_state["bp_auto_on"] = bp_on                       # shared with the Best Plays page


def _job_changed(key, label):
    on = st.session_state[f"au_{key}"]
    try:
        cfg = _gh().load_json(GH_TOKEN, OWNER, REPO, CONFIG_PATH) or {}
        cfg.setdefault("jobs", {})[key] = on
        cfg["updated"] = datetime.now(tz=EASTERN).isoformat()
        _gh().save_json(GH_TOKEN, OWNER, REPO, CONFIG_PATH, cfg,
                        commit_msg=f"Automation: {label} {'ON' if on else 'OFF'}")
        st.session_state.pop("au_err", None)
    except Exception as e:  # noqa: BLE001
        st.session_state["au_err"] = f"Couldn't save {label}: {e}"


def _bp_changed():
    on = st.session_state["bp_auto_on"]
    try:
        _gh().save_json(GH_TOKEN, OWNER, REPO, BEST_PLAYS_PATH,
                        {"enabled": on, "updated": datetime.now(tz=EASTERN).isoformat()},
                        commit_msg=f"Auto snapshot {'ON' if on else 'OFF'}")
        st.session_state.pop("au_err", None)
    except Exception as e:  # noqa: BLE001
        st.session_state["au_err"] = f"Couldn't save Best Plays: {e}"


if st.session_state.get("au_err"):
    st.error(st.session_state["au_err"])

st.subheader("Best Plays")
st.toggle("🎯 Best Plays auto snapshot", key="bp_auto_on", on_change=_bp_changed,
          help="Every 2 hours, all day: snapshots every sport with a game today, logs picks and "
               "predictions, grades finished games. Same switch as on the Best Plays page.")
st.caption("Every 2 hours, at 17 minutes past the hour (plus a 3:05 PM test slot).")

st.subheader("Other scheduled jobs")
for key, label, when in JOBS:
    st.toggle(label, key=f"au_{key}", on_change=_job_changed, args=(key, label))
    st.caption(when)

st.divider()
st.caption(
    "The Daily All Tracker is turned off in its own workflow file, and the daily picks and HR line "
    "movement jobs have no schedule, so they aren't listed here."
)
