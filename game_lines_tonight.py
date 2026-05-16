"""Run the game-line (ML/RL/Totals) model on remaining games tonight."""
import urllib.request, json, ssl, tomllib
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from data import mlb_api, savant, weather as wx
from data.stadiums import get_stadium
from models import game_model, markets

ctx = ssl._create_unverified_context()
with open(".streamlit/secrets.toml", "rb") as f:
    cfg = tomllib.load(f)
key = cfg["THE_ODDS_API_KEY"]

season = 2026
today = datetime.now(tz=ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
now_utc = datetime.now(tz=timezone.utc)

print(f"Date: {today}  |  Now (ET): {datetime.now(tz=ZoneInfo('America/New_York')).strftime('%I:%M %p')}\n")

all_games = mlb_api.get_schedule(today)
upcoming = []
for g in all_games:
    fp_dt, _ = mlb_api.parse_first_pitch(g.get("gameDate", ""))
    if fp_dt and fp_dt.astimezone(timezone.utc) > now_utc:
        upcoming.append((g, fp_dt))
upcoming.sort(key=lambda x: x[1])
print(f"Upcoming games: {len(upcoming)}\n")

print("Loading model data...")
px_df = savant.pitcher_xstats(season)

# Match Odds API events
events = json.loads(urllib.request.urlopen(
    f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={key}",
    timeout=15, context=ctx).read())
event_map = {(e["away_team"] + "|" + e["home_team"]).lower(): e["id"] for e in events}

all_picks = []
for g, fp_dt in upcoming:
    away_name = g["teams"]["away"]["team"]["name"]
    home_name = g["teams"]["home"]["team"]["name"]
    home_id = g["teams"]["home"]["team"]["id"]
    away_id = g["teams"]["away"]["team"]["id"]
    stadium = get_stadium(home_id)
    fp_local = fp_dt.astimezone(ZoneInfo("America/New_York"))

    print(f"\n=== {away_name} @ {home_name} -- {fp_local.strftime('%I:%M %p ET')} @ {stadium['park']}")
    away_p = g["teams"]["away"].get("probablePitcher") or {}
    home_p = g["teams"]["home"].get("probablePitcher") or {}
    away_pid = away_p.get("id"); home_pid = home_p.get("id")
    print(f"  SP: {away_p.get('fullName','TBD')} vs {home_p.get('fullName','TBD')}")

    eid = event_map.get((away_name + "|" + home_name).lower())
    if not eid:
        print("  [No Odds API event]")
        continue

    # Game model — predict runs + win prob
    weather_g = wx.get_forecast(stadium["lat"], stadium["lon"], fp_dt) or {}
    home_stats = mlb_api.get_pitcher_season(home_pid, season) if home_pid else mlb_api._empty_pitcher()
    away_stats = mlb_api.get_pitcher_season(away_pid, season) if away_pid else mlb_api._empty_pitcher()
    home_h = mlb_api.get_team_season(home_id, season, "hitting")
    away_h = mlb_api.get_team_season(away_id, season, "hitting")
    home_r = mlb_api.get_team_recent(home_id, season, 15, "hitting")
    away_r = mlb_api.get_team_recent(away_id, season, 15, "hitting")
    home_p_team = mlb_api.get_team_season(home_id, season, "pitching")
    away_p_team = mlb_api.get_team_season(away_id, season, "pitching")
    home_bp = mlb_api.get_team_bullpen_stats(home_id, season)
    away_bp = mlb_api.get_team_bullpen_stats(away_id, season)

    home_p_savant = away_p_savant = None
    if home_pid and not px_df.empty:
        h = px_df[px_df["player_id"] == home_pid]
        if not h.empty: home_p_savant = h.iloc[0].to_dict()
    if away_pid and not px_df.empty:
        h = px_df[px_df["player_id"] == away_pid]
        if not h.empty: away_p_savant = h.iloc[0].to_dict()

    pred = game_model.predict_game(
        home_team_season=home_h, home_team_recent=home_r,
        away_team_season=away_h, away_team_recent=away_r,
        home_pitcher_stats=home_stats, home_pitcher_savant=home_p_savant,
        away_pitcher_stats=away_stats, away_pitcher_savant=away_p_savant,
        home_team_pitching_season=home_p_team, away_team_pitching_season=away_p_team,
        park_hr=stadium["hr_factor"], weather=weather_g,
        home_bullpen=home_bp, away_bullpen=away_bp,
    )
    print(f"  Model: {away_name} {pred['away_exp_runs']:.2f} -- {pred['home_exp_runs']:.2f} {home_name}  "
          f"total={pred['total_runs']:.2f}  p_home={pred['p_home_win']*100:.1f}%")

    # Fetch ML/RL/Totals
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{eid}/odds"
           f"?apiKey={key}&regions=us&markets=h2h,spreads,totals"
           f"&bookmakers=draftkings,fanduel,betmgm,betrivers&oddsFormat=american")
    try:
        d = json.loads(urllib.request.urlopen(url, timeout=15, context=ctx).read())
    except Exception:
        continue

    # Best lines per market+outcome (best = highest payout = lowest implied)
    best = {}
    for bm in d.get("bookmakers", []):
        for m in bm.get("markets", []):
            for o in m.get("outcomes", []):
                k = (m["key"], o.get("name"), o.get("point"))
                price = o.get("price")
                if price is None: continue
                if k not in best or price > best[k][0]:
                    best[k] = (price, bm["key"])

    # Compute edges per outcome — WITH BLEND LOGIC matching dashboard
    home_exp = pred["home_exp_runs"]; away_exp = pred["away_exp_runs"]
    p_home = pred["p_home_win"]; total_exp = pred["total_runs"]

    def _imp(price):
        return 100/(price+100) if price > 0 else abs(price)/(abs(price)+100)

    # Group totals by line so we can devig over/under pairs
    totals_by_line = {}
    for (mk, name, point), (price, book) in best.items():
        if mk == "totals" and point is not None:
            totals_by_line.setdefault(point, {})[name.lower()] = (price, book)

    for (mk, name, point), (price, book) in best.items():
        implied = _imp(price)
        dec = 1.0 + price/100.0 if price > 0 else 1.0 + 100.0/abs(price)

        if mk == "h2h":
            # Find opposing ML for devig
            opp_team = away_name if name == home_name else home_name
            opp_key = ("h2h", opp_team, None)
            if opp_key not in best: continue
            opp_imp = _imp(best[opp_key][0])
            mp_raw = p_home if name == home_name else 1.0 - p_home
            mp = markets.blended_moneyline(mp_raw, implied, opp_imp)  # alpha=0.5
            label = f"ML {name}"
        elif mk == "spreads":
            side = "home" if name == home_name else "away"
            mp = markets.prob_team_covers_spread(home_exp, away_exp, side, float(point))
            # RL uses pure model — no blend
            label = f"RL {name} {point:+.1f}"
        elif mk == "totals":
            # Need over and under pair to devig
            pair = totals_by_line.get(point, {})
            if "over" not in pair or "under" not in pair: continue
            imp_over = _imp(pair["over"][0]); imp_under = _imp(pair["under"][0])
            mp_raw = (markets.prob_total_over(total_exp, float(point))
                      if name.lower() == "over"
                      else markets.prob_total_under(total_exp, float(point)))
            mp = markets.blended_total(mp_raw, imp_over, imp_under, name)  # alpha=0.3
            label = f"TOT {name} {point}"
        else:
            continue

        edge_pp = (mp - implied) * 100
        ev = (mp * (dec - 1) - (1 - mp)) * 100
        all_picks.append({
            "label": label, "market": mk, "matchup": f"{away_name} @ {home_name}",
            "game_id": eid, "model_p": mp*100, "implied": implied*100, "edge_pp": edge_pp,
            "american": price, "decimal": dec, "ev": ev, "book": book,
            "park": stadium["park"],
        })

# Rank by edge
all_picks.sort(key=lambda x: -x["edge_pp"])
print(f"\n{'='*80}")
print(f"=== Top 15 game-line edges across remaining games ===")
print(f"{'='*80}")
print(f"{'Pick':<32} {'Matchup':<32} {'Model':>6} {'Mkt':>6} {'Edge':>6} {'Odds':>6}")
for p in all_picks[:15]:
    print(f"{p['label']:<32} {p['matchup'][:30]:<32} {p['model_p']:>5.1f}% {p['implied']:>5.1f}% {p['edge_pp']:>+5.1f}  {p['american']:+5d} [{p['book']}]")

# Auto-pick 4 across different games, edge >= +3pp
print(f"\n=== Auto-pick 4 legs (edge >= +3pp, 1 per game) ===")
selected = []
used = set()
for p in all_picks:
    if len(selected) >= 4: break
    if p["edge_pp"] < 3.0: continue
    if p["game_id"] in used: continue
    selected.append(p)
    used.add(p["game_id"])
for i, p in enumerate(selected, 1):
    print(f"  {i}. {p['label']:<32} {p['matchup']:<32} edge {p['edge_pp']:+5.1f}pp  {p['american']:+5d} [{p['book']}]")

if not selected:
    print("  No picks pass +3pp edge filter.")
elif len(selected) < 4:
    print(f"  Only {len(selected)} pick(s) pass the +3pp filter -- below threshold for 4-leg round robin.")
