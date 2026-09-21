"""
📊 NFL Team Totals — FanDuel

Live FanDuel team total lines for every NFL game this week.
FanDuel is the only book posting team totals via The Odds API.
"""
import json
import os
import ssl as _ssl
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

EASTERN = ZoneInfo("America/New_York")
_SSL    = _ssl._create_unverified_context()
SPORT   = "americanfootball_nfl"
MARKET  = "team_totals"
BOOKS   = "fanduel"

st.set_page_config(page_title="NFL Team Totals", page_icon="📊", layout="wide")
st.title("📊 NFL Team Totals — FanDuel")
st.caption("FanDuel team total lines for every NFL game this week. Hit **🔄 Refresh** to pull latest.")


def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


ODDS_KEY = _resolve_secret("THE_ODDS_API_KEY")
if not ODDS_KEY:
    st.error("Missing `THE_ODDS_API_KEY` in secrets.")
    st.stop()


# ---------- Fetch ----------

def _get_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/events?apiKey={api_key}"
    return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())


def _get_event_odds(api_key, event_id):
    url = (
        f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{event_id}/odds"
        f"?apiKey={api_key}&markets={MARKET}&bookmakers={BOOKS}&oddsFormat=american"
    )
    return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())


@st.cache_data(ttl=120, show_spinner="Fetching FanDuel team totals...")
def _fetch(api_key):
    try:
        events = _get_events(api_key)
    except Exception as e:
        return {"_error": str(e)}

    now_utc = datetime.now(tz=timezone.utc)
    results = []
    for ev in events:
        try:
            ct = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            if (ct - now_utc).total_seconds() < -7200:
                continue
        except Exception:
            pass
        try:
            results.append(_get_event_odds(api_key, ev["id"]))
        except Exception:
            pass
    return results


if st.button("🔄 Refresh", type="primary"):
    st.cache_data.clear()
    st.rerun()

with st.spinner("Loading..."):
    data = _fetch(ODDS_KEY)

if isinstance(data, dict) and "_error" in data:
    st.error(f"API error: {data['_error']}")
    st.stop()

if not data:
    st.warning("No NFL games found.")
    st.stop()


# ---------- Parse ----------

def _fmt_time(iso):
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %b %d %#I:%M %p ET" if sys.platform == "win32" else "%a %b %d %-I:%M %p ET"
        return ct.strftime(fmt)
    except Exception:
        return iso


def _fmt_price(p):
    if p is None:
        return "—"
    return f"+{int(p)}" if p > 0 else str(int(p))


rows = []
for ev in data:
    away   = ev.get("away_team", "?")
    home   = ev.get("home_team", "?")
    ct_str = ev.get("commence_time", "")
    kickoff = _fmt_time(ct_str)

    team_data = {}
    for bm in ev.get("bookmakers", []):
        if bm.get("key") != "fanduel":
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") != MARKET:
                continue
            for o in mkt.get("outcomes", []):
                team  = o.get("description", "")
                side  = (o.get("name") or "").lower()
                point = o.get("point")
                price = o.get("price")
                if team not in team_data:
                    team_data[team] = {"line": None, "over": None, "under": None}
                if point is not None:
                    team_data[team]["line"] = point
                if side == "over"  and price is not None:
                    team_data[team]["over"] = price
                if side == "under" and price is not None:
                    team_data[team]["under"] = price

    for team in (away, home):
        td = team_data.get(team, {})
        rows.append({
            "Kickoff": kickoff,
            "Game":    f"{away} @ {home}",
            "Team":    team,
            "Line":    td.get("line"),
            "Over":    td.get("over"),
            "Under":   td.get("under"),
            "_has":    td.get("line") is not None,
        })

df = pd.DataFrame(rows)
has_data = df[df["_has"]]

if has_data.empty:
    st.warning("FanDuel hasn't posted team totals yet — check back closer to game time.")
    st.stop()

# ---------- Display ----------

games_with_lines = has_data["Game"].nunique()
teams_with_lines = len(has_data)

c1, c2 = st.columns(2)
c1.metric("Games with team totals", games_with_lines)
c2.metric("Team lines posted",       teams_with_lines)

st.divider()

display = has_data[["Kickoff", "Game", "Team", "Line", "Over", "Under"]].copy()
display["Over"]  = display["Over"].apply(_fmt_price)
display["Under"] = display["Under"].apply(_fmt_price)

st.dataframe(
    display,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Line": st.column_config.NumberColumn(format="%.1f"),
    },
)

st.divider()
st.caption(
    f"Source: The Odds API · FanDuel only · "
    f"Last refresh: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')} · Cache TTL: 2 min"
)
