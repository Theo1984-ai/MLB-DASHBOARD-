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
    "Monte Carlo simulation (10,000 games) · Players ranked by **Edge** (model prob − book implied prob)  \n"
    "🟢 Positive edge = model thinks player is underpriced · 🔴 Negative = book has them overpriced"
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


# ---------- API helpers ----------

@st.cache_data(ttl=300, show_spinner=False)
def _get_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events?apiKey={api_key}"
    return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())


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


@st.cache_data(ttl=300, show_spinner=False)
def _get_td_props(api_key, event_id):
    from scripts.td_props_scanner import ALL_BOOKS
    results = {}
    url = (
        f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event_id}/odds"
        f"?apiKey={api_key}&markets=player_anytime_td"
        f"&bookmakers={','.join(ALL_BOOKS)}&oddsFormat=american"
    )
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
        for bm in data.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "player_anytime_td":
                    continue
                for o in mkt.get("outcomes", []):
                    desc = o.get("description", "") or ""
                    name = o.get("name", "") or ""
                    player = desc if desc and desc.lower() not in ("yes", "no", "") else name
                    if not player or player.lower() in ("yes", "no", ""):
                        continue
                    price = o.get("price")
                    if price is None:
                        continue
                    pk = player.lower()
                    if pk not in results:
                        results[pk] = {"name": player, "prices": []}
                    results[pk]["prices"].append(price)
    except Exception:
        pass

    for pk, v in results.items():
        if v["prices"]:
            v["best_price"] = max(v["prices"])
            implied = []
            for pr in v["prices"]:
                implied.append(abs(pr) / (abs(pr) + 100) if pr < 0 else 100 / (pr + 100))
            v["consensus_prob"] = sum(implied) / len(implied)
            bp = v["best_price"]
            v["best_implied"] = abs(bp) / (abs(bp) + 100) if bp < 0 else 100 / (bp + 100)
        else:
            v["best_price"] = v["consensus_prob"] = v["best_implied"] = None
    return results


# ---------- Name matching ----------

def _normalize(name):
    import re
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def _match(model_name, props):
    norm = _normalize(model_name)
    if norm in props:
        return props[norm]
    last = norm.split()[-1] if norm.split() else ""
    candidates = [v for k, v in props.items() if last in k.split()]
    if len(candidates) == 1:
        return candidates[0]
    return None


# ---------- Simulation ----------

@st.cache_data(ttl=300, show_spinner="Running Monte Carlo simulation...")
def _run_sim(team_totals_json):
    from scripts.td_simulator import simulate_td_probs
    return simulate_td_probs(json.loads(team_totals_json), n_sims=10000)


# ---------- Team abbreviation map ----------

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


# ---------- UI ----------

col_btn, col_space = st.columns([1, 6])
with col_btn:
    if st.button("🔄 Refresh", type="primary"):
        st.cache_data.clear()
        st.rerun()

with st.spinner("Loading games..."):
    events = _get_events(ODDS_KEY)

now_utc = datetime.now(tz=timezone.utc)
upcoming = [
    ev for ev in events
    if (datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) - now_utc).total_seconds() > -7200
]

if not upcoming:
    st.warning("No upcoming NFL games found.")
    st.stop()


def _fmt_game(ev):
    ct_str = ev.get("commence_time", "")
    try:
        ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %b %d %#I:%M %p" if sys.platform == "win32" else "%a %b %d %-I:%M %p"
        return f"{ev['away_team']} @ {ev['home_team']} — {ct.strftime(fmt)} ET"
    except Exception:
        return f"{ev['away_team']} @ {ev['home_team']}"


game_options = {_fmt_game(ev): ev for ev in upcoming}
selected_label = st.selectbox("Select game", list(game_options.keys()))
ev   = game_options[selected_label]
away = ev["away_team"]
home = ev["home_team"]
eid  = ev["id"]
away_abbr = NAME_TO_ABBR.get(away, away[:3].upper())
home_abbr = NAME_TO_ABBR.get(home, home[:3].upper())

# ---------- Team totals ----------

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
            team = o.get("description", "")
            if (o.get("name") or "").lower() == "over" and o.get("point") is not None:
                if team == away:
                    team_totals[away_abbr] = o["point"]
                elif team == home:
                    team_totals[home_abbr] = o["point"]

team_totals.setdefault(away_abbr, 23.0)
team_totals.setdefault(home_abbr, 23.0)

c1, c2 = st.columns(2)
away_pts = team_totals.get(away_abbr, "—")
home_pts = team_totals.get(home_abbr, "—")
c1.metric(f"{away}", f"{away_pts} pts expected")
c2.metric(f"{home}", f"{home_pts} pts expected")

# ---------- Run simulation ----------

with st.spinner("Simulating 10,000 games..."):
    sim_results = _run_sim(json.dumps(team_totals))

game_sim = {k: v for k, v in sim_results.items() if v["team"] in (away_abbr, home_abbr)}

if not game_sim:
    st.warning("No player stats found for these teams in nflverse 2026 data.")
    st.stop()

# ---------- Fetch book props ----------

with st.spinner("Fetching book TD odds..."):
    props = _get_td_props(ODDS_KEY, eid)

# ---------- Build table ----------

rows = []
for pk, sim in game_sim.items():
    prop       = _match(sim["name"], props)
    best_price = prop["best_price"]   if prop else None
    best_impl  = prop["best_implied"] if prop else None
    model_prob = sim["model_prob"]
    edge       = round(model_prob - best_impl, 4) if best_impl is not None else None

    rows.append({
        "Player":      sim["name"],
        "Team":        sim["team"],
        "Pos":         sim["position"],
        "Model %":     round(model_prob * 100, 1),
        "Book %":      round(best_impl * 100, 1) if best_impl is not None else None,
        "Edge %":      round(edge * 100, 1)       if edge      is not None else None,
        "Best Price":  best_price,
        "Season TDs":  int(sim["total_tds"]),
        "Games":       int(sim["games"]),
        "_edge":       edge if edge is not None else -999,
    })

df = pd.DataFrame(rows).sort_values("_edge", ascending=False).reset_index(drop=True)

# ---------- Sidebar filters ----------

with st.sidebar:
    st.header("Filters")
    all_pos    = sorted(df["Pos"].unique().tolist())
    pos_filter = st.multiselect("Position", all_pos, default=all_pos)
    min_model  = st.slider("Min model prob %", 0, 60, 5)
    value_only = st.toggle("Value bets only (Edge > 0)", value=False)

if pos_filter:
    df = df[df["Pos"].isin(pos_filter)]
df = df[df["Model %"] >= min_model]
if value_only:
    df = df[df["_edge"] > 0]

# ---------- Summary metrics ----------

st.divider()
st.subheader(f"📊 {away} @ {home} — All Players by Edge")

n_total  = len(df)
n_priced = int(df["Best Price"].notna().sum())
n_value  = int((df["_edge"] > 0).sum())

m1, m2, m3 = st.columns(3)
m1.metric("Players",         n_total)
m2.metric("With book price", n_priced)
m3.metric("Edge > 0 (value)", n_value)

st.divider()

# ---------- Format for display ----------

display = df.drop(columns=["_edge"]).copy()

def _fmt_price(x):
    if x is None or (x != x):
        return "—"
    return f"+{int(x)}" if x > 0 else str(int(x))

def _fmt_pct(x, sign=False):
    if x is None or (x != x):
        return "—"
    if sign and x > 0:
        return f"+{x:.1f}%"
    return f"{x:.1f}%"

display["Best Price"] = display["Best Price"].apply(_fmt_price)
display["Book %"]     = display["Book %"].apply(lambda x: _fmt_pct(x))
display["Edge %"]     = display["Edge %"].apply(lambda x: _fmt_pct(x, sign=True))
display["Model %"]    = display["Model %"].apply(lambda x: _fmt_pct(x))

# Reorder columns: most important first
display = display[["Player", "Team", "Pos", "Model %", "Book %", "Edge %", "Best Price", "Season TDs", "Games"]]

st.dataframe(display, use_container_width=True, hide_index=True)

st.divider()
st.caption(
    f"**Model**: Monte Carlo 10,000 sims · Poisson(team_total÷10) expected TDs · "
    f"Player share weighted by 2026 TD rate (Bayesian regression to position mean)  \n"
    f"**Edge** = Model % − Book implied % · Positive = model sees value · "
    f"Stats: nflverse 2026 · {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}"
)
