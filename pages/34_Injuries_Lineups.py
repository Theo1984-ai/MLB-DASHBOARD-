"""
🩹 Injuries & Lineups — each team's injury list and starting lineup, for today's games.

Data comes from ESPN and is saved by the hourly auto snapshot (best_plays_log/injuries/<SPORT>.json).
Starters appear only once ESPN posts them (usually about 30 minutes before the game).
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import injuries as injm  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
OWNER, REPO = "Theo1984-ai", "MLB-DASHBOARD-"


def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


GH_TOKEN = _resolve_secret("GITHUB_TOKEN")

st.set_page_config(page_title="Injuries & Lineups", page_icon="🩹", layout="centered")
st.title("🩹 Injuries & Lineups")
st.caption("Today's games. Saved every hour from ESPN. Starters show up about 30 minutes before the game.")

if not GH_TOKEN:
    st.error("Missing `GITHUB_TOKEN` in secrets.")
    st.stop()


@st.cache_data(ttl=300, show_spinner=False)
def load(sport, _token_present):
    from data import github_storage as gh
    return gh.load_json(GH_TOKEN, OWNER, REPO, f"best_plays_log/injuries/{sport}.json") or {}


def injury_lines(rows):
    if not rows:
        return ["No injuries listed."]
    return [f"{injm.badge(r)} **{r['name']}** {r['pos']}" + (f" — {r['note']}" if r.get("note") else "")
            for r in rows]


def render(sport):
    data = load(sport, True)
    games = data.get("lineups") or []
    teams = data.get("teams") or {}
    if not data:
        st.info("Nothing saved yet. It fills in after the next hourly snapshot.")
        return
    saved = data.get("fetched", "")
    if saved:
        st.caption("Saved " + datetime.fromisoformat(saved).astimezone(EASTERN).strftime("%a %I:%M %p").lstrip("0") + " ET")
    if not games:
        st.info("No games found for the saved day.")
    for g in sorted(games, key=lambda g: g.get("start", "")):
        try:
            when = datetime.fromisoformat(g["start"].replace("Z", "+00:00")).astimezone(EASTERN)
            when_txt = when.strftime("%I:%M %p").lstrip("0") + " ET"
        except Exception:  # noqa: BLE001
            when_txt = ""
        with st.expander(f"{g['away']} @ {g['home']} · {when_txt}", expanded=False):
            for side in ("away", "home"):
                team = g[side]
                st.markdown(f"**{team}**")
                starters = (g.get("starters") or {}).get(team)
                if starters:
                    hurt = {n for n in starters if injm.find(teams, team, team, n)}
                    st.markdown("Starters: " + ", ".join(
                        n + (" ⚠️" if n in hurt else "") for n in starters))
                else:
                    pitcher = (g.get("probables") or {}).get(team)
                    st.caption("Lineup not posted yet (usually about 30 minutes before the game)."
                               + (f" Probable pitcher: {pitcher}." if pitcher else ""))
                for line in injury_lines(teams.get(team, [])):
                    st.markdown(line, unsafe_allow_html=True)


tabs = st.tabs([injm.LABELS[s] for s in injm.PATHS])
for tab, sport in zip(tabs, injm.PATHS):
    with tab:
        render(sport)
