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
# A job in OFF_BY_DEFAULT stays off until it is switched on.
OFF_BY_DEFAULT = {"daily_all_tracker"}
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
    ("daily_all_tracker", "Daily All Tracker",
     "Settles yesterday's MLB results, 4 tries from about 6 to 9 AM. MLB is off-season. Off by default."),
]


# Not a scheduled job: a switch the auto snapshot reads before sending its Telegram alerts.
ALERTS = [
    ("telegram_alerts", "📲 Telegram alerts",
     "A message for each new Tier A play (edge 2%+, price no worse than -130, not a football line under 10 "
     "yards), at most 8 a day, none from 11 PM to 8 AM. Needs the Telegram secrets in GitHub."),
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


@st.cache_data(ttl=10, show_spinner=False)
def _read(path, _token_present):
    return _gh().load_json(GH_TOKEN, OWNER, REPO, path) or {}


def _safe(path):
    try:
        return _read(path, True), None
    except Exception as e:  # noqa: BLE001
        return {}, f"Couldn't read {path}: {e}"


cfg, err_a = _safe(CONFIG_PATH)
bp_cfg, err_b = _safe(BEST_PLAYS_PATH)
jobs = cfg.get("jobs", {})
stamp, bp_stamp = cfg.get("updated", "none"), bp_cfg.get("updated", "none")
bp_on = bp_cfg.get("enabled", True) is not False


def saved_on(key):
    return (jobs.get(key) is True) if key in OFF_BY_DEFAULT else (jobs.get(key) is not False)


# Every switch shows what is saved on GitHub: its key includes the saved timestamp, so any change
# (from either page, or a stale tab) rebuilds it from the saved value.
def _job_changed(key, label, wkey):
    on = st.session_state[wkey]
    try:
        cur = _gh().load_json(GH_TOKEN, OWNER, REPO, CONFIG_PATH) or {}
        cur.setdefault("jobs", {})[key] = on
        cur["updated"] = datetime.now(tz=EASTERN).isoformat()
        _gh().save_json(GH_TOKEN, OWNER, REPO, CONFIG_PATH, cur,
                        commit_msg=f"Automation: {label} {'ON' if on else 'OFF'}")
        st.session_state.pop("au_err", None)
    except Exception as e:  # noqa: BLE001
        st.session_state["au_err"] = f"Couldn't save {label}: {e}"
    _read.clear()


def _bp_changed(wkey):
    on = st.session_state[wkey]
    try:
        _gh().save_json(GH_TOKEN, OWNER, REPO, BEST_PLAYS_PATH,
                        {"enabled": on, "updated": datetime.now(tz=EASTERN).isoformat()},
                        commit_msg=f"Auto snapshot {'ON' if on else 'OFF'}")
        st.session_state.pop("au_err", None)
    except Exception as e:  # noqa: BLE001
        st.session_state["au_err"] = f"Couldn't save Best Plays: {e}"
    _read.clear()


for e in (err_a, err_b, st.session_state.get("au_err")):
    if e:
        st.error(e)

st.subheader("Best Plays")
bp_key = f"bp_auto_{bp_stamp}"
st.toggle("🎯 Best Plays auto snapshot", value=bp_on, key=bp_key, on_change=_bp_changed, args=(bp_key,),
          help="Every hour, all day: snapshots every sport with a game today, logs picks and "
               "predictions, grades finished games. Same switch as on the Best Plays page.")
st.caption("Saved on GitHub: " + ("ON" if bp_on else "OFF") + ". About once an hour. GitHub wakes it every 10 minutes "
           "and it runs when the last snapshot is 45+ minutes old, so a skipped slot is caught up.")

st.subheader("Other scheduled jobs")
for key, label, when in JOBS:
    wkey = f"au_{key}_{stamp}"
    st.toggle(label, value=saved_on(key), key=wkey, on_change=_job_changed, args=(key, label, wkey))
    st.caption(when)

st.subheader("Alerts")
for key, label, when in ALERTS:
    wkey = f"au_{key}_{stamp}"
    st.toggle(label, value=saved_on(key), key=wkey, on_change=_job_changed, args=(key, label, wkey))
    st.caption(when)

st.divider()
st.caption("The daily picks and HR line movement jobs have no schedule, so they aren't listed here.")
