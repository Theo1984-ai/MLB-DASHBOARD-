"""
📊 NFL Team Totals — FanDuel vs BetOnline

Side-by-side comparison of NFL team total lines from FanDuel (US sharp book)
and BetOnline (offshore book). Line discrepancies signal sharp action.
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
BOOKS   = "fanduel,betonlineag"

st.set_page_config(page_title="NFL Team Totals", page_icon="📊", layout="wide")
st.title("📊 NFL Team Totals — FanDuel vs BetOnline")
st.caption(
    "**FanDuel** (US sharp book) vs **BetOnline** (offshore) team total lines side by side.  \n"
    "🔴 = lines differ by 0.5+ · Hit **🔄 Refresh** to pull latest."
)


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


@st.cache_data(ttl=120, show_spinner="Fetching FanDuel & BetOnline team totals...")
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


def _parse_book(bookmakers, book_key):
    result = {}
    for bm in bookmakers:
        if bm.get("key") != book_key:
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") != MARKET:
                continue
            for o in mkt.get("outcomes", []):
                team  = o.get("description", "")
                side  = (o.get("name") or "").lower()
                point = o.get("point")
                price = o.get("price")
                if team not in result:
                    result[team] = {"line": None, "over": None, "under": None}
                if point is not None:
                    result[team]["line"] = point
                if side == "over"  and price is not None:
                    result[team]["over"] = int(price)
                if side == "under" and price is not None:
                    result[team]["under"] = int(price)
    return result


def _fmt_price(p):
    if p is None:
        return "—"
    return f"+{int(p)}" if p > 0 else str(int(p))


rows = []
for ev in data:
    away    = ev.get("away_team", "?")
    home    = ev.get("home_team", "?")
    ct_str  = ev.get("commence_time", "")
    kickoff = _fmt_time(ct_str)
    bms     = ev.get("bookmakers", [])

    fd  = _parse_book(bms, "fanduel")
    bol = _parse_book(bms, "betonlineag")

    for team in (away, home):
        f = fd.get(team, {})
        b = bol.get(team, {})
        f_line = f.get("line")
        b_line = b.get("line")
        diff   = round(abs(f_line - b_line), 1) if (f_line is not None and b_line is not None) else None
        rows.append({
            "Kickoff":   kickoff,
            "Game":      f"{away} @ {home}",
            "Team":      team,
            "FD Line":   f_line,
            "FD Over":   f.get("over"),
            "FD Under":  f.get("under"),
            "BOL Line":  b_line,
            "BOL Over":  b.get("over"),
            "BOL Under": b.get("under"),
            "Diff":      diff,
            "_flag":     (diff is not None and diff >= 0.5),
            "_has":      (f_line is not None or b_line is not None),
        })

df = pd.DataFrame(rows)
df_has = df[df["_has"]]

if df_has.empty:
    st.warning("No team total lines posted yet — check back closer to game time.")
    st.stop()

# ---------- Summary ----------

c1, c2, c3, c4 = st.columns(4)
c1.metric("Games",              df_has["Game"].nunique())
c2.metric("FanDuel lines",      int(df_has["FD Line"].notna().sum()))
c3.metric("BetOnline lines",    int(df_has["BOL Line"].notna().sum()))
c4.metric("🔴 Discrepancies",  int(df["_flag"].sum()))

st.divider()

# ---------- Combined table ----------

combined = df_has.copy()
combined["FD Over"]   = combined["FD Over"].apply(_fmt_price)
combined["FD Under"]  = combined["FD Under"].apply(_fmt_price)
combined["BOL Over"]  = combined["BOL Over"].apply(_fmt_price)
combined["BOL Under"] = combined["BOL Under"].apply(_fmt_price)
combined["Diff"]      = combined["Diff"].apply(lambda x: f"{x:.1f}" if x is not None else "—")

show_cols = ["Kickoff", "Game", "Team",
             "FD Line", "FD Over", "FD Under",
             "BOL Line", "BOL Over", "BOL Under",
             "Diff"]
st.dataframe(
    combined[show_cols],
    use_container_width=True,
    hide_index=True,
    column_config={
        "FD Line":  st.column_config.NumberColumn(format="%.1f"),
        "BOL Line": st.column_config.NumberColumn(format="%.1f"),
    },
)

# ---------- Discrepancies ----------

disc = combined[combined["_flag"]].sort_values("Diff", ascending=False)
if not disc.empty:
    st.divider()
    st.subheader("🔴 Line discrepancies (0.5+ difference)")
    st.caption("One book is behind the other — potential sharp signal.")
    show = disc[["Kickoff", "Game", "Team", "FD Line", "BOL Line", "Diff",
                 "FD Over", "FD Under", "BOL Over", "BOL Under"]]
    st.dataframe(show, use_container_width=True, hide_index=True,
                 column_config={
                     "FD Line":  st.column_config.NumberColumn(format="%.1f"),
                     "BOL Line": st.column_config.NumberColumn(format="%.1f"),
                 })

st.divider()
st.caption(
    f"Source: The Odds API · FanDuel (US) vs BetOnline (offshore) · "
    f"Last refresh: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')} · Cache TTL: 2 min"
)
