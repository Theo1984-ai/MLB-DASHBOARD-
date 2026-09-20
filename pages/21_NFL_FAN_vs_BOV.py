"""
🏈📊 NFL Team Totals — Fanatics vs Bovada

Live side-by-side comparison of every NFL team total line from
Fanatics and Bovada.  Highlights when the two books disagree.
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

EASTERN   = ZoneInfo("America/New_York")
_SSL      = _ssl._create_unverified_context()
SPORT     = "americanfootball_nfl"
MARKET    = "team_totals"
BOOKS     = "fanatics,bovada"

st.set_page_config(page_title="NFL FAN vs BOV", page_icon="📊", layout="wide")
st.title("📊 NFL Team Totals — Fanatics vs Bovada")
st.caption(
    "Live team total lines from **Fanatics** and **Bovada** side by side.  \n"
    "🔴 = lines differ by 0.5+  ·  Hit **🔄 Refresh** to pull latest."
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

@st.cache_data(ttl=120, show_spinner="Fetching Fanatics & Bovada team totals...")
def _fetch(api_key: str):
    url = (
        f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
        f"?apiKey={api_key}&markets={MARKET}"
        f"&bookmakers={BOOKS}&oddsFormat=american"
    )
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception as e:
        return {"_error": str(e)}


if st.button("🔄 Refresh", type="primary"):
    st.cache_data.clear()
    st.rerun()

with st.spinner("Loading..."):
    data = _fetch(ODDS_KEY)

if isinstance(data, dict) and "_error" in data:
    st.error(f"API error: {data['_error']}")
    st.stop()

if not data:
    st.warning("No NFL games found right now.")
    st.stop()


# ---------- Parse ----------

now_utc = datetime.now(tz=timezone.utc)

def _parse_book(bookmakers, book_key, away_team, home_team):
    """Return {team: {line, over_price, under_price}} for one book."""
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


def _fmt_time(iso):
    try:
        ct = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %b %d %#I:%M %p ET" if sys.platform == "win32" else "%a %b %d %-I:%M %p ET"
        return ct.strftime(fmt)
    except Exception:
        return iso


rows = []
for ev in data:
    ct_str = ev.get("commence_time", "")
    try:
        ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
        if (ct - now_utc).total_seconds() < -7200:  # skip games finished 2h+ ago
            continue
    except Exception:
        pass

    away = ev.get("away_team", "?")
    home = ev.get("home_team", "?")
    bms  = ev.get("bookmakers", [])

    fan = _parse_book(bms, "fanatics", away, home)
    bov = _parse_book(bms, "bovada",   away, home)

    for team in (away, home):
        f = fan.get(team, {})
        b = bov.get(team, {})
        f_line = f.get("line")
        b_line = b.get("line")
        diff   = round(abs(f_line - b_line), 1) if (f_line is not None and b_line is not None) else None
        rows.append({
            "Game":      f"{away} @ {home}",
            "Kickoff":   _fmt_time(ct_str),
            "Team":      team,
            # Fanatics
            "FAN Line":  f_line,
            "FAN Over":  f.get("over"),
            "FAN Under": f.get("under"),
            # Bovada
            "BOV Line":  b_line,
            "BOV Over":  b.get("over"),
            "BOV Under": b.get("under"),
            # Diff
            "Diff":      diff,
            "_flag":     (diff is not None and diff >= 0.5),
        })

if not rows:
    st.warning("No team total lines from Fanatics or Bovada right now — props may not be posted yet.")
    st.stop()

df = pd.DataFrame(rows)

# ---------- Summary ----------

total_teams  = len(df)
have_fan     = df["FAN Line"].notna().sum()
have_bov     = df["BOV Line"].notna().sum()
discrepancies = df["_flag"].sum()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Teams priced",    total_teams)
c2.metric("Fanatics lines",  int(have_fan))
c3.metric("Bovada lines",    int(have_bov))
c4.metric("🔴 Discrepancies (0.5+)", int(discrepancies))

st.divider()

# ---------- Two-column layout ----------

col_fan, col_bov = st.columns(2)

with col_fan:
    st.subheader("🟣 Fanatics")
    fan_df = df[df["FAN Line"].notna()][["Game", "Kickoff", "Team", "FAN Line", "FAN Over", "FAN Under", "Diff"]].copy()
    fan_df = fan_df.rename(columns={"FAN Line": "Line", "FAN Over": "Over", "FAN Under": "Under"})
    st.dataframe(
        fan_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Line":  st.column_config.NumberColumn(format="%.1f"),
            "Over":  st.column_config.NumberColumn(format="%+d"),
            "Under": st.column_config.NumberColumn(format="%+d"),
            "Diff":  st.column_config.NumberColumn(format="%.1f"),
        },
    )

with col_bov:
    st.subheader("🟠 Bovada")
    bov_df = df[df["BOV Line"].notna()][["Game", "Kickoff", "Team", "BOV Line", "BOV Over", "BOV Under", "Diff"]].copy()
    bov_df = bov_df.rename(columns={"BOV Line": "Line", "BOV Over": "Over", "BOV Under": "Under"})
    st.dataframe(
        bov_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Line":  st.column_config.NumberColumn(format="%.1f"),
            "Over":  st.column_config.NumberColumn(format="%+d"),
            "Under": st.column_config.NumberColumn(format="%+d"),
            "Diff":  st.column_config.NumberColumn(format="%.1f"),
        },
    )

# ---------- Discrepancies callout ----------

if discrepancies > 0:
    st.divider()
    st.subheader("🔴 Line discrepancies (0.5+ difference)")
    st.caption("These teams have a meaningful gap between Fanatics and Bovada — one book is behind the other.")
    disc_df = df[df["_flag"]][
        ["Game", "Kickoff", "Team", "FAN Line", "BOV Line", "Diff",
         "FAN Over", "FAN Under", "BOV Over", "BOV Under"]
    ].copy()
    disc_df = disc_df.sort_values("Diff", ascending=False)
    st.dataframe(
        disc_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "FAN Line": st.column_config.NumberColumn(format="%.1f"),
            "BOV Line": st.column_config.NumberColumn(format="%.1f"),
            "Diff":     st.column_config.NumberColumn(format="%.1f"),
            "FAN Over":  st.column_config.NumberColumn(format="%+d"),
            "FAN Under": st.column_config.NumberColumn(format="%+d"),
            "BOV Over":  st.column_config.NumberColumn(format="%+d"),
            "BOV Under": st.column_config.NumberColumn(format="%+d"),
        },
    )

st.divider()
st.caption(
    f"Source: The Odds API  ·  "
    f"Last refresh: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 2 min  ·  Market: NFL team totals"
)
