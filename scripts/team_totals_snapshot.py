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


def _target_game_date(events, now_et):
    """Determine which ET date this snapshot should track.

    Priority (fixed 7/21 after user reported 8 PM refresh jumping to
    next day):
      1. If today (ET) still has ANY upcoming games → return today.
         Even at 8 PM ET, late West Coast games (Padres/Dodgers/etc.)
         are still upcoming. User pressing refresh wants today's current
         state, not tomorrow's opening lines.
      2. Otherwise (today's slate is fully done) → return the date with
         the most upcoming games (usually tomorrow).
    """
    from datetime import timezone as _tz
    now_utc = datetime.now(tz=_tz.utc)
    today_str = now_et.strftime("%Y-%m-%d")
    date_counts = {}
    for ev in events:
        try:
            ct = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ct <= now_utc:
            continue
        d = ct.astimezone(EASTERN).date().strftime("%Y-%m-%d")
        date_counts[d] = date_counts.get(d, 0) + 1
    # Prefer today if today has any upcoming games
    if date_counts.get(today_str, 0) > 0:
        return today_str
    if not date_counts:
        return today_str
    return max(date_counts.items(), key=lambda x: x[1])[0]


def main(force=False, min_gap_min=30):
    """Run the team-totals snapshot.

    force: bypass the freshness guard (used by manual refresh button).
    min_gap_min: skip if last snapshot for this date is < this many min ago.

    Returns: dict {status, path, snapshot_n, n_games} for programmatic callers.
    """
    api_key = _resolve_api_key()
    if not api_key:
        print("ERROR: THE_ODDS_API_KEY not set", file=sys.stderr)
        return {"status": "no_api_key"}

    now = datetime.now(tz=EASTERN)
    out_dir = os.path.join(ROOT, "team_totals_history")
    os.makedirs(out_dir, exist_ok=True)

    print(f"=== Team Totals snapshot ({BOOK}) — run at {now.strftime('%I:%M %p ET')} ===")
    race_safe_git_pull()

    events = _fetch_events(api_key)
    print(f"  {len(events)} MLB events found")

    # Smart date detection: files are keyed by the ET date of the GAMES
    # being tracked, not when the snapshot ran. So 11 PM Monday captures
    # Tuesday's opening lines and saves under 2026-07-21.json, and then
    # 12 PM Tuesday appends to the same file.
    target_date = _target_game_date(events, now)
    out_path = os.path.join(out_dir, f"{target_date}.json")
    print(f"  Target game date: {target_date}  →  {out_path}")

    from datetime import timezone as _tz
    now_utc = datetime.now(tz=_tz.utc)
    games = []
    for ev in events:
        try:
            ct = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ct <= now_utc:
            continue
        # Only include games for the target date (avoid mixing multi-day)
        if ct.astimezone(EASTERN).date().strftime("%Y-%m-%d") != target_date:
            continue
        data = _fetch_team_totals(api_key, ev["id"])
        parsed = _parse_game(data) if data else None
        if parsed:
            games.append(parsed)

    print(f"  {len(games)} games with DK team-total lines for {target_date}")

    # Load existing snapshots
    existing = {"snapshots": []}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    snapshots = existing.get("snapshots", []) or []

    # Freshness guard: if last snapshot < min_gap_min old and NOT forced,
    # skip so backup crons don't create duplicates.
    if not force and snapshots:
        try:
            last_ts = datetime.fromisoformat(snapshots[-1]["captured_at"])
            age_min = (now - last_ts).total_seconds() / 60
            if age_min < min_gap_min:
                print(f"  [SKIP] Last snapshot {age_min:.0f} min ago "
                      f"(< {min_gap_min} min gap). Use force=True to override.")
                return {
                    "status": "skipped_fresh",
                    "path": out_path,
                    "snapshot_n": len(snapshots),
                    "n_games": snapshots[-1].get("n_games", 0),
                    "age_min": age_min,
                }
        except Exception:
            pass

    snapshots.append({
        "captured_at": now.isoformat(),
        "n_games":     len(games),
        "games":       games,
    })

    payload = {
        "date":         target_date,
        "book":         BOOK,
        "market":       MARKET,
        "n_snapshots":  len(snapshots),
        "snapshots":    snapshots,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"  Saved snapshot #{len(snapshots)} -> {out_path}")
    return {
        "status": "ok",
        "path": out_path,
        "snapshot_n": len(snapshots),
        "n_games": len(games),
        "target_date": target_date,
    }


if __name__ == "__main__":
    import sys as _sys
    force = "--force" in _sys.argv
    main(force=force)
