"""
Team Totals hourly snapshot for DraftKings.

Runs hourly from 11 AM through 10 PM ET. Each run appends a new snapshot
to today's file so users can track line movement across the day.

Output: team_totals_history/YYYY-MM-DD.json
Structure:
  {
    "date": "2026-07-14",
    "book": "draftkings",
    "market": "team_totals",
    "n_snapshots": N,
    "snapshots": [
      {
        "captured_at": ISO timestamp,
        "n_games": int,
        "games": [
          {
            "game": "Yankees @ Orioles",
            "away_team": ...,
            "home_team": ...,
            "first_pitch": ISO,
            "away": {"line": 4.5, "over_price": -110, "under_price": -110},
            "home": {"line": 4.5, "over_price": -105, "under_price": -115}
          },
          ...
        ]
      },
      ...
    ]
  }
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
BOOK = "draftkings"
MARKET = "team_totals"


def _fetch_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={api_key}"
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception as e:
        print(f"  ERROR fetching events: {e}")
        return []


def _fetch_team_totals(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={MARKET}"
           f"&bookmakers={BOOK}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception:
        return {}


def _parse_game(event):
    """Extract team-totals for the DK book. Returns dict or None if no data."""
    away = event.get("away_team")
    home = event.get("home_team")
    if not away or not home:
        return None
    dk = None
    for bm in event.get("bookmakers", []):
        if bm.get("key") == BOOK:
            dk = bm
            break
    if not dk:
        return None
    # team_totals market outcomes: name="Over"/"Under", description=team name
    # e.g. {name: "Over", description: "Cleveland Guardians", point: 3.5, price: -110}
    away_over = away_under = home_over = home_under = None
    for m in dk.get("markets", []):
        if m.get("key") != MARKET:
            continue
        for o in m.get("outcomes", []):
            side = (o.get("name") or "").lower()   # "over" / "under"
            team = o.get("description")            # team name
            point = o.get("point")
            price = o.get("price")
            if team == away:
                if side == "over":  away_over = (point, price)
                elif side == "under": away_under = (point, price)
            elif team == home:
                if side == "over":  home_over = (point, price)
                elif side == "under": home_under = (point, price)
    # Skip games where DK hasn't posted lines yet
    if not (away_over or away_under or home_over or home_under):
        return None
    return {
        "game":        f"{away} @ {home}",
        "away_team":   away,
        "home_team":   home,
        "first_pitch": event.get("commence_time"),
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


def race_safe_git_pull():
    """When running in GitHub Actions, pull remote first to avoid concurrent-runner
    write conflicts. Silent no-op locally."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        import subprocess
        subprocess.run(
            ["git", "pull", "--rebase", "-X", "theirs", "origin", "main"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
    except Exception:
        pass


def main():
    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
        try:
            import tomllib
            with open(os.path.join(ROOT, ".streamlit", "secrets.toml"), "rb") as f:
                api_key = tomllib.load(f).get("THE_ODDS_API_KEY")
        except Exception:
            pass
    if not api_key:
        print("ERROR: THE_ODDS_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(tz=EASTERN)
    today_et = now.strftime("%Y-%m-%d")
    out_dir = os.path.join(ROOT, "team_totals_history")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{today_et}.json")

    print(f"=== Team Totals snapshot ({BOOK}) — {today_et} ===")
    race_safe_git_pull()

    events = _fetch_events(api_key)
    print(f"  {len(events)} MLB events found")

    games = []
    from datetime import timezone as _tz
    now_utc = datetime.now(tz=_tz.utc)
    for ev in events:
        # Skip games already started. Include all upcoming games in the
        # next 24h — usually all today's evening games (if snapshot runs
        # early) and tomorrow's day games (if snapshot runs late tonight).
        try:
            ct = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ct <= now_utc:
            continue   # game already started
        data = _fetch_team_totals(api_key, ev["id"])
        parsed = _parse_game(data) if data else None
        if parsed:
            games.append(parsed)

    print(f"  {len(games)} games with DK team-total lines")

    # Load existing snapshots for today (append pattern)
    existing = {"snapshots": []}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    snapshots = existing.get("snapshots", []) or []

    snapshots.append({
        "captured_at": now.isoformat(),
        "n_games":     len(games),
        "games":       games,
    })

    payload = {
        "date":         today_et,
        "book":         BOOK,
        "market":       MARKET,
        "n_snapshots":  len(snapshots),
        "snapshots":    snapshots,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"  Saved snapshot #{len(snapshots)} -> {out_path}")


if __name__ == "__main__":
    main()
