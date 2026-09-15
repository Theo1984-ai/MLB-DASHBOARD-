"""
NFL Team Totals snapshot — on-demand, one file per week.

All games this NFL week (Thu Night, Sunday slate, Mon Night) go into
one file keyed by the Monday that starts the week:
  nfl_team_totals_history/week_YYYY-MM-DD.json

No cron — triggered manually from the page's Refresh button.
"""
import json
import os
import ssl as _ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_SSL = _ssl._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")
SPORT = "americanfootball_nfl"
BOOKS = "draftkings,fanduel,betmgm,bovada,williamhill_us"
MARKET = "team_totals"
HISTORY_DIR = os.path.join(ROOT, "nfl_team_totals_history")


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


def _extract_book(bookmaker, away_team, home_team):
    away_over = away_under = home_over = home_under = None
    for m in bookmaker.get("markets", []):
        if m.get("key") != MARKET:
            continue
        for o in m.get("outcomes", []):
            side = (o.get("name") or "").lower()
            team = o.get("description")
            point = o.get("point")
            price = o.get("price")
            if team == away_team:
                if side == "over":    away_over  = (point, price)
                elif side == "under": away_under = (point, price)
            elif team == home_team:
                if side == "over":    home_over  = (point, price)
                elif side == "under": home_under = (point, price)
    if not (away_over or away_under or home_over or home_under):
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
        if key not in ("draftkings", "fanduel", "betmgm", "bovada", "williamhill_us"):
            continue
        parsed = _extract_book(bm, away, home)
        if parsed:
            per_book[key] = parsed
    if not per_book:
        return None
    dk = per_book.get("draftkings") or next(iter(per_book.values()))
    return {
        "game":        f"{away} @ {home}",
        "away_team":   away,
        "home_team":   home,
        "first_pitch": event.get("commence_time"),
        "away":        dk["away"],
        "home":        dk["home"],
        "books":       per_book,
        "n_books":     len(per_book),
    }


def week_monday(dt_et):
    """Return Monday of the NFL week containing dt_et (Mon=0)."""
    days_since_mon = dt_et.weekday()
    return (dt_et - timedelta(days=days_since_mon)).replace(
        hour=0, minute=0, second=0, microsecond=0)


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

    now    = datetime.now(tz=EASTERN)
    now_utc = datetime.now(tz=timezone.utc)
    os.makedirs(HISTORY_DIR, exist_ok=True)

    # Week window: this Monday 00:00 ET → next Monday 00:00 ET
    mon = week_monday(now)
    week_start = mon
    week_end   = mon + timedelta(days=7)

    week_key  = mon.strftime("%Y-%m-%d")
    out_path  = os.path.join(HISTORY_DIR, f"week_{week_key}.json")

    # Freshness guard
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
                return {
                    "status":      "skipped_fresh",
                    "path":        out_path,
                    "snapshot_n":  len(snapshots),
                    "n_games":     snapshots[-1].get("n_games", 0),
                    "age_min":     age_min,
                    "week_key":    week_key,
                }
        except Exception:
            pass

    # Fetch ALL games in this week (Thu + Sun + Mon)
    events = _fetch_events(api_key)
    games = []
    for ev in events:
        try:
            ct = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        ct_et = ct.astimezone(EASTERN)
        # Include games within this week window (upcoming or very recently started)
        if not (week_start <= ct_et < week_end):
            continue
        data   = _fetch_team_totals(api_key, ev["id"])
        parsed = _parse_game(data) if data else None
        if parsed:
            games.append(parsed)

    # Sort by kickoff
    games.sort(key=lambda g: g.get("first_pitch") or "")

    snapshots.append({
        "captured_at": now.isoformat(),
        "n_games":     len(games),
        "games":       games,
    })

    payload = {
        "week":        week_key,
        "book":        "draftkings",
        "market":      MARKET,
        "sport":       SPORT,
        "n_snapshots": len(snapshots),
        "snapshots":   snapshots,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    return {
        "status":      "ok",
        "path":        out_path,
        "snapshot_n":  len(snapshots),
        "n_games":     len(games),
        "week_key":    week_key,
    }


if __name__ == "__main__":
    force = "--force" in sys.argv
    print(main(force=force))
