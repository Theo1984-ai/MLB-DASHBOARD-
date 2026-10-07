"""
NHL Team Totals daily snapshot — DraftKings + 4 other books.

Runs hourly from 11 AM through 11 PM ET during the NHL season.
Output: nhl_team_totals_history/YYYY-MM-DD.json

Structure mirrors team_totals_snapshot.py (MLB) exactly, with:
  - sport key: icehockey_nhl
  - history dir: nhl_team_totals_history/
"""
import json
import os
import ssl as _ssl
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_SSL = _ssl._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")
SPORT = "icehockey_nhl"
BOOKS = "fanatics,fanduel"
MARKET = "team_totals"
HISTORY_DIR = os.path.join(ROOT, "nhl_team_totals_history")


def _fetch_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/events?apiKey={api_key}"
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception as e:
        print(f"  ERROR fetching events: {e}")
        return []


def _fetch_team_totals(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/{SPORT}/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={MARKET}"
           f"&bookmakers={BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception:
        return {}


_SANE_PT_MIN = 1.5   # NHL team total floor
_SANE_PT_MAX = 6.0   # raised to handle dominant teams (CAR, EDM etc at 5.5)
_MAX_PRICE   = 220   # main-market odds rarely exceed ±220; alt lines are more extreme

def _best_outcome(candidates):
    """From a list of (point, price) tuples pick the one closest to even money
    within the sane point range. Returns None if nothing survives the filters."""
    valid = [(pt, pr) for pt, pr in candidates
             if pt is not None and pr is not None
             and _SANE_PT_MIN <= pt <= _SANE_PT_MAX
             and abs(pr) <= _MAX_PRICE]
    if not valid:
        return None
    return min(valid, key=lambda x: abs(x[1]))  # closest to 0 = most even-money


def _extract_book(bookmaker, away_team, home_team):
    away_overs = []; away_unders = []; home_overs = []; home_unders = []
    for m in bookmaker.get("markets", []):
        if m.get("key") != MARKET:
            continue
        for o in m.get("outcomes", []):
            side = (o.get("name") or "").lower()
            team = o.get("description")
            point = o.get("point")
            price = o.get("price")
            if team == away_team:
                if side == "over":    away_overs.append((point, price))
                elif side == "under": away_unders.append((point, price))
            elif team == home_team:
                if side == "over":    home_overs.append((point, price))
                elif side == "under": home_unders.append((point, price))

    away_over  = _best_outcome(away_overs)
    away_under = _best_outcome(away_unders)
    home_over  = _best_outcome(home_overs)
    home_under = _best_outcome(home_unders)

    # Require valid lines for BOTH teams — partial data (one team only) is unusable
    if not (away_over or away_under) or not (home_over or home_under):
        return None
    return {
        "away": {
            "line":        away_over[0] if away_over else (away_under[0] if away_under else None),
            "over_price":  away_over[1] if away_over else None,
            "under_price": away_under[1] if away_under else None,
        },
        "home": {
            "line":        home_over[0] if home_over else (home_under[0] if home_under else None),
            "over_price":  home_over[1] if home_over else None,
            "under_price": home_under[1] if home_under else None,
        },
    }


def _parse_game(event):
    away = event.get("away_team")
    home = event.get("home_team")
    if not away or not home:
        return None
    per_book = {}
    for bm in event.get("bookmakers", []):
        key = bm.get("key")
        if key not in ("fanatics", "fanduel"):
            continue
        parsed = _extract_book(bm, away, home)
        if parsed:
            per_book[key] = parsed
    if not per_book:
        return None
    # Fanatics has the most NHL team total coverage; FanDuel is sharper but misses games
    anchor_priority = ("fanatics", "fanduel")
    anchor_key = next((b for b in anchor_priority if b in per_book), next(iter(per_book)))
    anchor = per_book[anchor_key]
    return {
        "game":        f"{away} @ {home}",
        "away_team":   away,
        "home_team":   home,
        "first_pitch": event.get("commence_time"),
        "away":        anchor["away"],
        "home":        anchor["home"],
        "anchor_book": anchor_key,
        "books":       per_book,
        "n_books":     len(per_book),
    }


def _resolve_api_key():
    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
        try:
            import tomllib
            with open(os.path.join(ROOT, ".streamlit", "secrets.toml"), "rb") as f:
                api_key = tomllib.load(f).get("THE_ODDS_API_KEY")
        except Exception:
            pass
    return api_key


def main(force=False, min_gap_min=30):
    api_key = _resolve_api_key()
    if not api_key:
        return {"status": "no_api_key"}

    now = datetime.now(tz=EASTERN)
    os.makedirs(HISTORY_DIR, exist_ok=True)

    today = now.strftime("%Y-%m-%d")
    out_path = os.path.join(HISTORY_DIR, f"{today}.json")

    # Freshness guard — skip if snapshot taken recently
    existing = {"snapshots": []}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    snapshots = existing.get("snapshots", []) or []
    if not force and snapshots:
        try:
            last_ts = datetime.fromisoformat(snapshots[-1]["captured_at"])
            age_min = (now - last_ts).total_seconds() / 60
            if age_min < min_gap_min:
                return {"status": "fresh", "age_min": age_min}
        except Exception:
            pass

    from datetime import timezone
    now_utc = now.astimezone(timezone.utc)

    events = _fetch_events(api_key)
    games = []
    for ev in events:
        # Skip games that have already started — freeze pre-game lines in history
        commence = ev.get("commence_time", "")
        if commence:
            try:
                gt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                if gt <= now_utc:
                    continue
            except Exception:
                pass
        data = _fetch_team_totals(api_key, ev["id"])
        g = _parse_game({**ev, "bookmakers": data.get("bookmakers", [])})
        if g:
            games.append(g)

    snap = {
        "captured_at": now.isoformat(),
        "n_games":     len(games),
        "games":       games,
    }
    snapshots.append(snap)

    payload = {
        "date":        today,
        "sport":       SPORT,
        "book":        "fanduel",
        "market":      MARKET,
        "n_snapshots": len(snapshots),
        "snapshots":   snapshots,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"  NHL snapshot #{len(snapshots)}: {len(games)} games -> {out_path}")
    return {"status": "ok", "n_games": len(games), "snapshot_n": len(snapshots),
            "path": out_path, "target_date": today}


if __name__ == "__main__":
    result = main(force=True)
    print(result)
