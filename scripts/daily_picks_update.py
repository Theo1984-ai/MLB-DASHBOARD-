"""
Daily picks auto-updater. Runs in GitHub Actions on a cron schedule.

Scans today's MLB + NBA slates for Tier A picks (cross-book disagreement
methodology), and writes tonight_picks/latest.json which the Streamlit
Tonight's Picks page reads from the GitHub repo.

Environment variables required:
  THE_ODDS_API_KEY  — sportsbook odds API key (set as GitHub Secret)
"""
import os
import sys
import json
import ssl
import urllib.request
import urllib.error
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Add parent dir to path so we can import project modules
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ctx = ssl._create_unverified_context()

KEY = os.environ.get("THE_ODDS_API_KEY")
if not KEY:
    print("ERROR: THE_ODDS_API_KEY env var not set")
    sys.exit(1)

SHARP = "draftkings,fanduel,betmgm,williamhill_us,bovada"
EASTERN = ZoneInfo("America/New_York")

# Today's date in ET (auto-handles late-night runs)
today_dt = datetime.now(tz=EASTERN)
TODAY = today_dt.strftime("%Y-%m-%d")
print(f"=== Daily picks update for {TODAY} ===")


def amer_to_imp(am):
    return 100 / (am + 100) if am > 0 else abs(am) / (abs(am) + 100)


def ev(am, p):
    if am > 0:
        return p * am - (1 - p) * 100
    return p * 100 - (1 - p) * abs(am)


def fetch_odds(eid, sport, markets):
    url = (f"https://api.the-odds-api.com/v4/sports/{sport}/events/{eid}/odds"
           f"?apiKey={KEY}&regions=us&markets={markets}"
           f"&bookmakers={SHARP}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=ctx).read())
    except (urllib.error.URLError, urllib.error.HTTPError):
        return {}


def fetch_events(sport):
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/events?apiKey={KEY}"
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=ctx).read())
    except Exception:
        return []


# ===== MLB SCAN =====
print("\nScanning MLB...")
from data import mlb_api

mlb_games = mlb_api.get_schedule(TODAY)
mlb_events = fetch_events("baseball_mlb")
mlb_map = {(e["away_team"] + "|" + e["home_team"]).lower(): e["id"] for e in mlb_events}

MKT_LABEL = {
    "batter_home_runs": "HR", "batter_home_runs_alternate": "HR",
    "batter_hits": "Hits", "batter_hits_alternate": "Hits",
    "batter_total_bases": "TB", "batter_total_bases_alternate": "TB",
    "batter_hits_runs_rbis_alternate": "H+R+R",
}

PROP_MARKETS_MLB = (
    "batter_home_runs,batter_home_runs_alternate,"
    "batter_hits,batter_hits_alternate,"
    "batter_total_bases,batter_total_bases_alternate,"
    "batter_hits_runs_rbis_alternate"
)

mlb_picks = []
for g in mlb_games:
    aw = g["teams"]["away"]["team"]["name"]
    hm = g["teams"]["home"]["team"]["name"]
    eid = mlb_map.get((aw + "|" + hm).lower())
    if not eid:
        continue
    game_short = f'{aw.split()[-1]} @ {hm.split()[-1]}'

    pp = fetch_odds(eid, "baseball_mlb", PROP_MARKETS_MLB)
    offers = defaultdict(list)
    for b in pp.get("bookmakers", []):
        for m in b.get("markets", []):
            for o in m.get("outcomes", []):
                name = (o.get("name") or "").strip()
                player = o.get("description") or name
                if player in ("Over", "Under"):
                    continue
                side = name if name in ("Over", "Under") else "Yes"
                pt = o.get("point")
                price = o.get("price")
                if price is None:
                    continue
                offers[(m["key"], player, side, pt)].append((b["key"], int(price)))

    for (mkt, player, side, pt), books in offers.items():
        if len(books) < 3:
            continue
        best_book, best_price = max(books, key=lambda x: x[1])
        if best_price < -300 or best_price > 700:
            continue
        imp_list = [amer_to_imp(p) for _, p in books]
        consensus = sum(imp_list) / len(imp_list)
        edge_pp = (max(imp_list) - amer_to_imp(best_price)) * 100
        EV = ev(best_price, consensus)
        if EV < 5:
            continue
        mlb_picks.append({
            "sport": "MLB", "game": game_short,
            "market": MKT_LABEL.get(mkt, mkt),
            "player": player, "side": side, "line": pt,
            "price": best_price, "book": best_book,
            "fair_pct": round(consensus * 100, 1),
            "edge_pp": round(edge_pp, 1),
            "ev": round(EV, 2),
            "n_books": len(books),
        })

print(f"  MLB Tier A picks: {len(mlb_picks)}")

# ===== NBA SCAN =====
print("\nScanning NBA...")
NBA_MKT = {
    "player_points": "PTS", "player_points_alternate": "PTS",
    "player_rebounds": "REB", "player_rebounds_alternate": "REB",
    "player_assists": "AST", "player_assists_alternate": "AST",
    "player_threes": "3PM", "player_threes_alternate": "3PM",
    "player_points_rebounds_assists": "PRA",
    "player_points_rebounds_assists_alternate": "PRA",
}
PROP_MARKETS_NBA = (
    "player_points,player_points_alternate,"
    "player_rebounds,player_rebounds_alternate,"
    "player_assists,player_assists_alternate,"
    "player_threes,player_threes_alternate,"
    "player_points_rebounds_assists,player_points_rebounds_assists_alternate"
)

nba_events = fetch_events("basketball_nba")
nba_picks = []
nba_game_label = None
for e in nba_events:
    ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
    dh = (ct - datetime.now(timezone.utc)).total_seconds() / 3600
    if not (0 <= dh < 30):  # next 30 hours
        continue
    eid = e["id"]
    game_short = f'{e["away_team"].split()[-1]} @ {e["home_team"].split()[-1]}'
    if nba_game_label is None:
        nba_game_label = f'{e["away_team"]} @ {e["home_team"]} ({ct.astimezone(EASTERN).strftime("%I:%M %p ET")})'

    pp = fetch_odds(eid, "basketball_nba", PROP_MARKETS_NBA)
    offers = defaultdict(list)
    for b in pp.get("bookmakers", []):
        for m in b.get("markets", []):
            for o in m.get("outcomes", []):
                name = (o.get("name") or "").strip()
                player = o.get("description") or name
                if player in ("Over", "Under"):
                    continue
                side = name if name in ("Over", "Under") else "Yes"
                pt = o.get("point")
                price = o.get("price")
                if price is None:
                    continue
                offers[(m["key"], player, side, pt)].append((b["key"], int(price)))

    for (mkt, player, side, pt), books in offers.items():
        if len(books) < 3:
            continue
        best_book, best_price = max(books, key=lambda x: x[1])
        if best_price < -300 or best_price > 700:
            continue
        imp_list = [amer_to_imp(p) for _, p in books]
        consensus = sum(imp_list) / len(imp_list)
        edge_pp = (max(imp_list) - amer_to_imp(best_price)) * 100
        EV = ev(best_price, consensus)
        if EV < 5:
            continue
        nba_picks.append({
            "sport": "NBA", "game": game_short,
            "market": NBA_MKT.get(mkt, mkt),
            "player": player, "side": side, "line": pt,
            "price": best_price, "book": best_book,
            "fair_pct": round(consensus * 100, 1),
            "edge_pp": round(edge_pp, 1),
            "ev": round(EV, 2),
            "n_books": len(books),
        })
    break  # only scan the first upcoming NBA game

print(f"  NBA Tier A picks: {len(nba_picks)}")

# ===== BUILD JSON =====
all_picks = mlb_picks + nba_picks
all_picks.sort(key=lambda x: -x["ev"])
top_picks = all_picks[:15]

# Concentration plays
player_counts = Counter((p["sport"], p["player"], p["game"]) for p in all_picks)
concentration_plays = []
for (sport, player, game), count in player_counts.most_common():
    if count < 5:
        break
    plays = [p for p in all_picks if p["player"] == player and p["game"] == game]
    plays.sort(key=lambda x: -x["ev"])
    books = Counter(p["book"] for p in plays)
    most_common_book, n_in_book = books.most_common(1)[0]
    book_label = f"{most_common_book}"
    if n_in_book < count:
        book_label += f" ({n_in_book}/{count} picks)"
    else:
        book_label += f" (all {count} picks)"
    concentration_plays.append({
        "player": player, "game": game, "book": book_label,
        "n_picks": count,
        "picks": [{
            "market": p["market"], "side": p["side"], "line": p["line"],
            "price": p["price"], "book": p["book"], "ev": p["ev"],
        } for p in plays[:6]]
    })

# Safe locks (negative odds, high fair %)
safe_locks = [
    p for p in all_picks
    if p["price"] < 0 and p["fair_pct"] >= 65 and p["ev"] >= 5
][:6]
safe_lock_dicts = [{
    "player": s["player"], "market": s["market"], "side": s["side"],
    "line": s["line"], "price": s["price"], "book": s["book"],
    "fair_pct": s["fair_pct"], "ev": s["ev"]
} for s in safe_locks]

# Build bet card from top picks
bet_card = {}
max_conviction = top_picks[:3]
if max_conviction:
    bet_card["💎 Max Conviction (2u each)"] = [{
        "stake": "2u",
        "description": f"{p['player']} {p['market']} {p['side']} {p['line']}",
        "price": p["price"], "book": p["book"],
    } for p in max_conviction]

tier_two = top_picks[3:8]
if tier_two:
    bet_card["🎲 Tier 2 (1u each)"] = [{
        "stake": "1u",
        "description": f"{p['player']} {p['market']} {p['side']} {p['line']}",
        "price": p["price"], "book": p["book"],
    } for p in tier_two]

if safe_lock_dicts:
    bet_card["🛡️ Safe Locks (1u each)"] = [{
        "stake": "1u",
        "description": f"{s['player']} {s['market']} {s['side']} {s['line']}",
        "price": s["price"], "book": s["book"],
    } for s in safe_lock_dicts[:3]]

games_with_props = len({p["game"] for p in mlb_picks})
intro = (f"{len(mlb_picks)} MLB picks across {games_with_props}/{len(mlb_games)} games + "
         f"{len(nba_picks)} NBA picks. Auto-scanned by GitHub Actions cron. "
         f"Cross-book disagreement methodology — only picks with 3+ books pricing and "
         f"$5+ EV are surfaced.")

if games_with_props < len(mlb_games) / 2:
    intro += " ⚠️ Many MLB games haven't posted props yet — re-check after 3 PM ET when more lines come up."

# Strategy notes
strategy_notes = [
    f"🤖 Auto-generated by GitHub Actions cron at {today_dt.strftime('%I:%M %p ET')}",
    "💎 Singles only — no parlays. Yesterday's parlays went 0/5.",
    f"🎯 {sum(1 for p in all_picks if p['ev'] >= 15)} picks have +$15 EV per $100",
    "📱 Bookmark this page on your phone for daily access",
]
if top_picks:
    p = top_picks[0]
    strategy_notes.append(
        f"🌟 Pick of the night: {p['player']} {p['market']} O{p['line']} "
        f"@ {p['price']:+d} ({p['book']}) = ${p['ev']:+.2f} EV"
    )

# Top picks formatted
top_picks_display = [{
    "player": p["player"], "game": p["game"],
    "market": p["market"], "side": p["side"],
    "line": p["line"], "price": p["price"],
    "book": p["book"], "ev": p["ev"], "fair_pct": p["fair_pct"]
} for p in top_picks[:10]]

# Avoid list
avoid = [
    "⛔ Multi-leg parlays — singles only",
    "⛔ Single-book longshots at +1500+ (stale pricing trap)",
    "⛔ Stacking 4+ same-game props (correlation traps)",
]
if games_with_props < len(mlb_games) / 2:
    avoid.append(f"⏰ {len(mlb_games) - games_with_props} MLB games have no Tier A picks yet — re-scan after 3 PM ET")

# NBA picks (top 10)
nba_picks_display = [{
    "player": p["player"], "market": p["market"], "side": p["side"],
    "line": p["line"], "price": p["price"], "book": p["book"], "ev": p["ev"]
} for p in nba_picks[:10]]

payload = {
    "date": TODAY,
    "generated_at": today_dt.strftime("%Y-%m-%d %I:%M %p %Z"),
    "analyst": "Auto-scanner (GitHub Actions cron)",
    "intro": intro,
    "concentration_plays": concentration_plays[:5],
    "top_picks": top_picks_display,
    "bet_card": bet_card,
    "safe_locks": safe_lock_dicts,
    "nba_game": nba_game_label or "No NBA game on the slate",
    "nba_picks": nba_picks_display,
    "avoid": avoid,
    "strategy_notes": strategy_notes,
}

# Write JSON
out_path = os.path.join(ROOT, "tonight_picks", "latest.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

print(f"\n✅ Wrote {len(top_picks)} top picks + {len(concentration_plays)} concentration plays")
print(f"   → {out_path}")
print(f"   Total Tier A picks: {len(all_picks)} ({len(mlb_picks)} MLB + {len(nba_picks)} NBA)")
if top_picks:
    p = top_picks[0]
    print(f"   Pick of the night: {p['player']} {p['market']} {p['side']} {p['line']} "
          f"@ {p['price']:+d} ({p['book']}) — EV ${p['ev']:+.2f}")
