"""
Picks updater — importable scan + JSON-builder.
Used by both:
  - scripts/daily_picks_update.py (GitHub Actions cron)
  - pages/5_Tonight_Picks.py (manual "Update" button)

TIER A FILTER — TIGHTENED 2026-05-24 after 8-day analysis showed
loose filters (3+ books, $5+ EV) were underperforming. New filters
require 5+ book consensus and $10+ EV, dramatically reducing volume
but improving conviction per pick.
"""
import os
import sys
import json
import ssl
import urllib.request
import urllib.error
from collections import defaultdict, Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ctx = ssl._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")
SHARP = "draftkings,fanduel,betmgm,williamhill_us,bovada"

# ============================================================================
# TIER A FILTER PARAMETERS (tightened 2026-05-24)
# ============================================================================
# Increased from 3 → 5 minimum books. With 5 sharp books in our pool, this
# requires near-total consensus on the prop. Eliminates 2-book disagreements
# that turned out to be noise rather than signal.
MIN_BOOKS = 5

# Increased from $5 → $10 minimum EV per $100. Doubles the conviction bar.
MIN_EV = 10.0

# Price band — unchanged. Avoids extreme juice on both ends.
MIN_PRICE = -300
MAX_PRICE = 700


def amer_to_imp(am):
    return 100 / (am + 100) if am > 0 else abs(am) / (abs(am) + 100)


def ev(am, p):
    if am > 0:
        return p * am - (1 - p) * 100
    return p * 100 - (1 - p) * abs(am)


def _fetch_odds(api_key, eid, sport, markets):
    url = (f"https://api.the-odds-api.com/v4/sports/{sport}/events/{eid}/odds"
           f"?apiKey={api_key}&regions=us&markets={markets}"
           f"&bookmakers={SHARP}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=ctx).read())
    except Exception:
        return {}


def _fetch_events(api_key, sport):
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/events?apiKey={api_key}"
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=ctx).read())
    except Exception:
        return []


MKT_LABEL_MLB = {
    "batter_home_runs": "HR", "batter_home_runs_alternate": "HR",
    "batter_hits": "Hits", "batter_hits_alternate": "Hits",
    "batter_total_bases": "TB", "batter_total_bases_alternate": "TB",
    "batter_hits_runs_rbis_alternate": "H+R+R",
}
MKT_LABEL_NBA = {
    "player_points": "PTS", "player_points_alternate": "PTS",
    "player_rebounds": "REB", "player_rebounds_alternate": "REB",
    "player_assists": "AST", "player_assists_alternate": "AST",
    "player_threes": "3PM", "player_threes_alternate": "3PM",
    "player_points_rebounds_assists": "PRA",
    "player_points_rebounds_assists_alternate": "PRA",
}
PROP_MARKETS_MLB = (
    "batter_home_runs,batter_home_runs_alternate,"
    "batter_hits,batter_hits_alternate,"
    "batter_total_bases,batter_total_bases_alternate,"
    "batter_hits_runs_rbis_alternate"
)
PROP_MARKETS_NBA = (
    "player_points,player_points_alternate,"
    "player_rebounds,player_rebounds_alternate,"
    "player_assists,player_assists_alternate,"
    "player_threes,player_threes_alternate,"
    "player_points_rebounds_assists,player_points_rebounds_assists_alternate"
)


def _scan_event(api_key, eid, sport, markets, mkt_label_map):
    """Scan one event and return Tier A picks list."""
    pp = _fetch_odds(api_key, eid, sport, markets)
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

    picks = []
    for (mkt, player, side, pt), books in offers.items():
        # TIGHTENED FILTER (5/24): require near-total consensus
        if len(books) < MIN_BOOKS:
            continue
        best_book, best_price = max(books, key=lambda x: x[1])
        if best_price < MIN_PRICE or best_price > MAX_PRICE:
            continue
        imp_list = [amer_to_imp(p) for _, p in books]
        consensus = sum(imp_list) / len(imp_list)
        edge_pp = (max(imp_list) - amer_to_imp(best_price)) * 100
        EV = ev(best_price, consensus)
        if EV < MIN_EV:
            continue
        picks.append({
            "market": mkt_label_map.get(mkt, mkt),
            "player": player, "side": side, "line": pt,
            "price": best_price, "book": best_book,
            "fair_pct": round(consensus * 100, 1),
            "edge_pp": round(edge_pp, 1),
            "ev": round(EV, 2),
            "n_books": len(books),
        })
    return picks


def run_scan(api_key):
    """
    Run the full slate scan. Returns a dict ready to be serialized to JSON
    and dropped into tonight_picks/latest.json.
    """
    today_dt = datetime.now(tz=EASTERN)
    today_str = today_dt.strftime("%Y-%m-%d")

    # MLB
    from data import mlb_api
    mlb_games = mlb_api.get_schedule(today_str)
    mlb_events = _fetch_events(api_key, "baseball_mlb")
    mlb_map = {(e["away_team"] + "|" + e["home_team"]).lower(): e["id"] for e in mlb_events}

    mlb_picks = []
    for g in mlb_games:
        aw = g["teams"]["away"]["team"]["name"]
        hm = g["teams"]["home"]["team"]["name"]
        eid = mlb_map.get((aw + "|" + hm).lower())
        if not eid:
            continue
        game_short = f'{aw.split()[-1]} @ {hm.split()[-1]}'
        for p in _scan_event(api_key, eid, "baseball_mlb", PROP_MARKETS_MLB, MKT_LABEL_MLB):
            p["sport"] = "MLB"
            p["game"] = game_short
            mlb_picks.append(p)

    # NBA — only first upcoming game in next 30h
    nba_events = _fetch_events(api_key, "basketball_nba")
    nba_picks = []
    nba_game_label = None
    for e in nba_events:
        ct = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        dh = (ct - datetime.now(timezone.utc)).total_seconds() / 3600
        if not (0 <= dh < 30):
            continue
        nba_game_label = (f'{e["away_team"]} @ {e["home_team"]} '
                          f'({ct.astimezone(EASTERN).strftime("%I:%M %p ET")})')
        game_short = f'{e["away_team"].split()[-1]} @ {e["home_team"].split()[-1]}'
        for p in _scan_event(api_key, e["id"], "basketball_nba", PROP_MARKETS_NBA, MKT_LABEL_NBA):
            p["sport"] = "NBA"
            p["game"] = game_short
            nba_picks.append(p)
        break

    # Combine + rank
    all_picks = mlb_picks + nba_picks
    all_picks.sort(key=lambda x: -x["ev"])
    top_picks = all_picks[:15]

    # Concentration plays — lowered threshold from 5 to 3 since the new strict
    # filter (5+ books, $10+ EV) naturally produces fewer picks per player.
    player_counts = Counter((p["sport"], p["player"], p["game"]) for p in all_picks)
    concentration_plays = []
    for (sport, player, game), count in player_counts.most_common():
        if count < 3:
            break
        plays = sorted(
            [p for p in all_picks if p["player"] == player and p["game"] == game],
            key=lambda x: -x["ev"],
        )
        books = Counter(p["book"] for p in plays)
        most_common_book, n_in_book = books.most_common(1)[0]
        book_label = f"{most_common_book}"
        book_label += f" (all {count} picks)" if n_in_book == count else f" ({n_in_book}/{count} picks)"
        concentration_plays.append({
            "player": player, "game": game, "book": book_label,
            "n_picks": count,
            "picks": [{
                "market": p["market"], "side": p["side"], "line": p["line"],
                "price": p["price"], "book": p["book"], "ev": p["ev"],
            } for p in plays[:6]]
        })

    # Safe locks
    safe_locks = sorted(
        [p for p in all_picks if p["price"] < 0 and p["fair_pct"] >= 65 and p["ev"] >= 5],
        key=lambda x: -x["ev"],
    )[:6]
    safe_lock_dicts = [{
        "player": s["player"], "market": s["market"], "side": s["side"],
        "line": s["line"], "price": s["price"], "book": s["book"],
        "fair_pct": s["fair_pct"], "ev": s["ev"]
    } for s in safe_locks]

    # Bet card
    bet_card = {}
    if top_picks[:3]:
        bet_card["💎 Max Conviction (2u each)"] = [{
            "stake": "2u",
            "description": f"{p['player']} {p['market']} {p['side']} {p['line']}",
            "price": p["price"], "book": p["book"],
        } for p in top_picks[:3]]
    if top_picks[3:8]:
        bet_card["🎲 Tier 2 (1u each)"] = [{
            "stake": "1u",
            "description": f"{p['player']} {p['market']} {p['side']} {p['line']}",
            "price": p["price"], "book": p["book"],
        } for p in top_picks[:8][3:]]
    if safe_lock_dicts:
        bet_card["🛡️ Safe Locks (1u each)"] = [{
            "stake": "1u",
            "description": f"{s['player']} {s['market']} {s['side']} {s['line']}",
            "price": s["price"], "book": s["book"],
        } for s in safe_lock_dicts[:3]]

    games_with_props = len({p["game"] for p in mlb_picks})
    intro = (f"🔒 STRICT FILTER: {len(mlb_picks)} MLB + {len(nba_picks)} NBA Tier A picks "
             f"(5+ books, $10+ EV). Tightened 5/24 after 8-day analysis showed loose "
             f"filters (3+ books, $5+ EV) underperformed. Fewer picks per night, but "
             f"each one has near-total book consensus.")
    if mlb_games and games_with_props < len(mlb_games) / 2:
        intro += " ⚠️ Many MLB games haven't posted props yet — re-scan later."

    strategy_notes = [
        f"🤖 Scanned at {today_dt.strftime('%I:%M %p %Z')} with TIGHTENED filter (5+ books, $10+ EV)",
        "💎 Singles only — no parlays",
        f"🎯 {sum(1 for p in all_picks if p['ev'] >= 15)} picks have $15+ EV",
        "📊 8-day data: BetMGM picks profitable, FanDuel picks lost money. Watch book.",
        "📱 Bookmark this page for daily access",
    ]
    if top_picks:
        p = top_picks[0]
        strategy_notes.append(
            f"🌟 Pick of the night: {p['player']} {p['market']} "
            f"{p['side']} {p['line']} @ {p['price']:+d} ({p['book']}) = "
            f"${p['ev']:+.2f} EV"
        )

    avoid = [
        "⛔ Multi-leg parlays — singles only",
        "⛔ Single-book longshots at +1500+ (stale pricing trap)",
        "⛔ Stacking 4+ same-game props (correlation traps)",
        "⚠️ FanDuel HR picks underperformed in 8-day sample — sanity-check before betting",
    ]
    if mlb_games and games_with_props < len(mlb_games) / 2:
        avoid.append(f"⏰ {len(mlb_games) - games_with_props} MLB games still lack props — re-scan later")
    if len(all_picks) < 5:
        avoid.append("ℹ️ Strict filter produced few picks tonight — this is normal. Quality > quantity.")

    return {
        "date": today_str,
        "generated_at": today_dt.strftime("%Y-%m-%d %I:%M %p %Z"),
        "analyst": "Auto-scanner",
        "intro": intro,
        "concentration_plays": concentration_plays[:5],
        "top_picks": [{
            "player": p["player"], "game": p["game"],
            "market": p["market"], "side": p["side"],
            "line": p["line"], "price": p["price"],
            "book": p["book"], "ev": p["ev"], "fair_pct": p["fair_pct"]
        } for p in top_picks[:10]],
        "bet_card": bet_card,
        "safe_locks": safe_lock_dicts,
        "nba_game": nba_game_label or "No NBA game on the slate",
        "nba_picks": [{
            "player": p["player"], "market": p["market"], "side": p["side"],
            "line": p["line"], "price": p["price"], "book": p["book"], "ev": p["ev"]
        } for p in nba_picks[:10]],
        "avoid": avoid,
        "strategy_notes": strategy_notes,
    }
