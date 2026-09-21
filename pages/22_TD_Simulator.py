"""
🎯 TD Scorer Simulator

Monte Carlo simulation of anytime TD scorer probabilities using
nflverse 2026 season stats + team total expected points.
Compared against current book prices to find value edges.
"""
import json
import os
import sys
import ssl as _ssl
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASTERN = ZoneInfo("America/New_York")
_SSL    = _ssl._create_unverified_context()

st.set_page_config(page_title="TD Simulator", page_icon="🎯", layout="wide")
st.title("🎯 TD Scorer Simulator")
st.caption(
    "Monte Carlo simulation (10,000 games) using 2026 season TD rates + team total expected points.  \n"
    "**Edge** = Model prob − Book implied prob. Positive = model sees more value than the book prices in."
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

sys.path.insert(0, ROOT)


# ---------- Fetch NFL events ----------

@st.cache_data(ttl=300, show_spinner=False)
def _get_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events?apiKey={api_key}"
    return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())


# ---------- Fetch team totals (FanDuel live) ----------

@st.cache_data(ttl=300, show_spinner=False)
def _get_team_total(api_key, event_id):
    url = (
        f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event_id}/odds"
        f"?apiKey={api_key}&markets=team_totals&bookmakers=fanduel&oddsFormat=american"
    )
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception:
        return {}


# ---------- Fetch TD props ----------

@st.cache_data(ttl=300, show_spinner=False)
def _get_td_props(api_key, event_id):
    from scripts.td_props_scanner import TD_SCORER_MARKETS, ALL_BOOKS
    results = {}
    for market in ["player_anytime_td"]:
        url = (
            f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event_id}/odds"
            f"?apiKey={api_key}&markets={market}"
            f"&bookmakers={','.join(ALL_BOOKS)}&oddsFormat=american"
        )
        try:
            data = json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
            for bm in data.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != market:
                        continue
                    for o in mkt.get("outcomes", []):
                        desc = o.get("description", "") or ""
                        name = o.get("name", "") or ""
                        player = desc if desc and desc.lower() not in ("yes","no","") else name
                        if not player or player.lower() in ("yes","no",""):
                            continue
                        side = name.lower()
                        if side != "yes" and side not in ("over", "under"):
                            # Some books use player name as outcome name
                            side = "yes"
                        price = o.get("price")
                        if price is None:
                            continue
                        pk = player.lower()
                        if pk not in results:
                            results[pk] = {"name": player, "prices": [], "best_price": None}
                        results[pk]["prices"].append(price)
        except Exception:
            pass

    # Compute implied prob and best price
    for pk, v in results.items():
        if v["prices"]:
            v["best_price"] = max(v["prices"])
            # Consensus: avg implied prob across books
            implied = []
            for pr in v["prices"]:
                if pr < 0:
                    implied.append(abs(pr) / (abs(pr) + 100))
                else:
                    implied.append(100 / (pr + 100))
            v["consensus_prob"] = sum(implied) / len(implied)
            v["best_implied"]   = (100 / (v["best_price"] + 100)
                                   if v["best_price"] > 0
                                   else abs(v["best_price"]) / (abs(v["best_price"]) + 100))
        else:
            v["consensus_prob"] = None
            v["best_implied"]   = None
    return results


# ---------- Name matching ----------

def _normalize(name):
    """Lowercase, strip punctuation for fuzzy matching."""
    import re
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def _match(model_name, props):
    """Find best matching prop entry for a model player name."""
    norm = _normalize(model_name)
    # Exact
    if norm in props:
        return props[norm]
    # Partial: last name match
    last = norm.split()[-1] if norm.split() else ""
    candidates = [v for k, v in props.items() if last in k.split()]
    if len(candidates) == 1:
        return candidates[0]
    return None


# ---------- Run simulation ----------

@st.cache_data(ttl=300, show_spinner="Running Monte Carlo simulation (10,000 games)...")
def _run_sim(team_totals_json):
    from scripts.td_simulator import simulate_td_probs
    team_totals = json.loads(team_totals_json)
    return simulate_td_probs(team_totals, n_sims=10000)


# ---------- UI ----------

if st.button("🔄 Refresh", type="primary"):
    st.cache_data.clear()
    st.rerun()

with st.spinner("Loading this week's games..."):
    events = _get_events(ODDS_KEY)

now_utc = datetime.now(tz=timezone.utc)
upcoming = []
for ev in events:
    try:
        ct = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if (ct - now_utc).total_seconds() > -7200:  # not finished
            upcoming.append(ev)
    except Exception:
        pass

if not upcoming:
    st.warning("No upcoming NFL games found.")
    st.stop()


# ---------- Game selector ----------

def _fmt_game(ev):
    ct_str = ev.get("commence_time","")
    try:
        ct = datetime.fromisoformat(ct_str.replace("Z","+00:00")).astimezone(EASTERN)
        fmt = "%a %b %d %#I:%M %p" if sys.platform == "win32" else "%a %b %d %-I:%M %p"
        t = ct.strftime(fmt)
    except Exception:
        t = ct_str
    return f"{ev['away_team']} @ {ev['home_team']} — {t} ET"

game_options = {_fmt_game(ev): ev for ev in upcoming}
selected_label = st.selectbox("Select game", list(game_options.keys()))
ev = game_options[selected_label]

away = ev["away_team"]
home = ev["home_team"]
eid  = ev["id"]

# Nflverse team abbreviation map (ESPN name → nflverse abbrev)
NAME_TO_ABBR = {
    "Arizona Cardinals":"ARI","Atlanta Falcons":"ATL","Baltimore Ravens":"BAL",
    "Buffalo Bills":"BUF","Carolina Panthers":"CAR","Chicago Bears":"CHI",
    "Cincinnati Bengals":"CIN","Cleveland Browns":"CLE","Dallas Cowboys":"DAL",
    "Denver Broncos":"DEN","Detroit Lions":"DET","Green Bay Packers":"GB",
    "Houston Texans":"HOU","Indianapolis Colts":"IND","Jacksonville Jaguars":"JAX",
    "Kansas City Chiefs":"KC","Los Angeles Rams":"LA","Los Angeles Chargers":"LAC",
    "Las Vegas Raiders":"LV","Miami Dolphins":"MIA","Minnesota Vikings":"MIN",
    "New England Patriots":"NE","New Orleans Saints":"NO","New York Giants":"NYG",
    "New York Jets":"NYJ","Philadelphia Eagles":"PHI","Pittsburgh Steelers":"PIT",
    "Seattle Seahawks":"SEA","San Francisco 49ers":"SF","Tampa Bay Buccaneers":"TB",
    "Tennessee Titans":"TEN","Washington Commanders":"WAS",
}

away_abbr = NAME_TO_ABBR.get(away, away[:3].upper())
home_abbr = NAME_TO_ABBR.get(home, home[:3].upper())


# ---------- Fetch team totals ----------

with st.spinner("Fetching FanDuel team totals..."):
    tt_data = _get_team_total(ODDS_KEY, eid)

team_totals = {}
for bm in tt_data.get("bookmakers", []):
    if bm.get("key") != "fanduel":
        continue
    for mkt in bm.get("markets", []):
        if mkt.get("key") != "team_totals":
            continue
        for o in mkt.get("outcomes", []):
            team = o.get("description","")
            side = (o.get("name") or "").lower()
            if side == "over" and o.get("point") is not None:
                if team == away:
                    team_totals[away_abbr] = o["point"]
                elif team == home:
                    team_totals[home_abbr] = o["point"]

# Fallback to NFL average if no team total available
if away_abbr not in team_totals:
    team_totals[away_abbr] = 23.0
if home_abbr not in team_totals:
    team_totals[home_abbr] = 23.0

c1, c2 = st.columns(2)
c1.metric(f"{away} expected pts (FD)", team_totals.get(away_abbr, "—"))
c2.metric(f"{home} expected pts (FD)", team_totals.get(home_abbr, "—"))


# ---------- Simulation ----------

with st.spinner("Running simulation..."):
    sim_results = _run_sim(json.dumps(team_totals))

# Filter to players from this game's teams only
game_sim = {
    k: v for k, v in sim_results.items()
    if v["team"] in (away_abbr, home_abbr)
}

if not game_sim:
    st.warning("No player stats found for these teams in nflverse 2026 data.")
    st.stop()


# ---------- Fetch book props ----------

with st.spinner("Fetching book TD props..."):
    props = _get_td_props(ODDS_KEY, eid)


# ---------- Build merged table ----------

rows = []
for pk, sim in game_sim.items():
    model_prob = sim["model_prob"]
    prop = _match(sim["name"], props)

    book_consensus = prop["consensus_prob"] if prop else None
    best_price     = prop["best_price"]     if prop else None
    best_implied   = prop["best_implied"]   if prop else None
    edge = round(model_prob - best_implied, 3) if best_implied is not None else None

    rows.append({
        "Team":        sim["team"],
        "Player":      sim["name"],
        "Pos":         sim["position"],
        "Games":       int(sim["games"]),
        "Season TDs":  sim["total_tds"],
        "TD/Game":     sim["td_per_game"],
        "Model %":     round(model_prob * 100, 1),
        "Book %":      round(best_implied * 100, 1) if best_implied else None,
        "Edge %":      round(edge * 100, 1) if edge is not None else None,
        "Best Price":  best_price,
        "_edge":       edge if edge is not None else -99,
    })

df = pd.DataFrame(rows).sort_values("_edge", ascending=False)

# ---------- Filters ----------

with st.sidebar:
    st.header("Filters")
    pos_filter = st.multiselect("Position", ["QB","RB","WR","TE"], default=["RB","WR","TE"])
    min_model  = st.slider("Min model prob %", 0, 80, 10)
    value_only = st.toggle("Value only (Edge > 0)", value=False)

if pos_filter:
    df = df[df["Pos"].isin(pos_filter)]
df = df[df["Model %"] >= min_model]
if value_only:
    df = df[df["_edge"] > 0]

# ---------- Display ----------

st.divider()
st.subheader(f"📊 {away} @ {home} — Anytime TD Scorer")

n_with_props = df["Best Price"].notna().sum()
n_value      = (df["_edge"] > 0).sum()
co1, co2, co3 = st.columns(3)
co1.metric("Players simulated", len(df))
co2.metric("With book price",   int(n_with_props))
co3.metric("Model value (Edge > 0)", int(n_value))

st.divider()

display = df.drop(columns=["_edge"]).copy()
display["Best Price"] = display["Best Price"].apply(
    lambda x: (f"+{int(x)}" if x > 0 else str(int(x))) if x is not None and x == x else "—"
)
display["Book %"]  = display["Book %"].apply(lambda x: f"{x:.1f}" if x is not None and x == x else "—")
display["Edge %"]  = display["Edge %"].apply(lambda x: f"+{x:.1f}" if x and x > 0
                                              else (f"{x:.1f}" if x is not None and x == x else "—"))
display["Season TDs"] = display["Season TDs"].apply(lambda x: f"{x:.0f}")

st.dataframe(
    display,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Model %":    st.column_config.NumberColumn(format="%.1f%%"),
    }
)

st.divider()
st.caption(
    "**Model**: Monte Carlo 10,000 sims · Poisson(team_total÷10) TDs per game · "
    "Player share weighted by blended 2026 TD rate (regressed to position mean).  \n"
    "**Edge** = Model prob − Best book implied prob. Positive = model thinks player is underpriced.  \n"
    f"Stats source: nflverse 2026 regular season · Page loaded: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}"
)
