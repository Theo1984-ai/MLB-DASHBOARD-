"""
HR Line Movement snapshot for DraftKings.

Same schedule as Team Totals: 11 PM ET (opener for next day's games)
and 12 PM ET (pre-first-pitch check). Manual refresh via page button.

Pulls DraftKings batter_home_runs prop for every game, saves each
player's Yes-to-HR price as a snapshot. Comparison of open vs current
across snapshots reveals line movement — where sharps have moved DK.

Output: hr_line_movement/YYYY-MM-DD.json
Structure:
  {
    "date": "2026-07-21",
    "book": "draftkings",
    "market": "batter_home_runs",
    "n_snapshots": N,
    "snapshots": [
      {
        "captured_at": ISO,
        "n_players": int,
        "players": [
          {
            "player": "Aaron Judge",
            "game": "Yankees @ Orioles",
            "away_team": "New York Yankees",
            "home_team": "Baltimore Orioles",
            "first_pitch": ISO,
            "price": +320,
            "line": 0.5
          }, ...
        ]
      }, ...
    ]
  }
"""
import json
import os
import ssl as _ssl
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_SSL = _ssl._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")
BOOK = "draftkings"
# DK uses `batter_home_runs_alternate` (not `batter_home_runs`). The alt
# market includes multiple lines (0.5, 1.5, 2.5) per player. We filter
# to point=0.5 which is the standard "≥1 HR in the game" prop.
MARKET = "batter_home_runs_alternate"
STANDARD_LINE = 0.5


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


def _fetch_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={api_key}"
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception as e:
        print(f"  ERROR fetching events: {e}")
        return []


def _fetch_hr_props(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us&markets={MARKET}"
           f"&bookmakers={BOOK}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())
    except Exception:
        return {}


def _parse_event(event):
    """Extract per-player HR props. Returns list of player dicts."""
    away = event.get("away_team")
    home = event.get("home_team")
    if not away or not home:
        return []
    dk = None
    for bm in event.get("bookmakers", []):
        if bm.get("key") == BOOK:
            dk = bm
            break
    if not dk:
        return []
    players = []
    for m in dk.get("markets", []):
        if m.get("key") != MARKET:
            continue
        # DK batter_home_runs_alternate outcomes:
        #   {name: "Over", description: "Byron Buxton", point: 0.5, price: 272}
        # Filter to the standard 0.5 line (≥1 HR). Higher lines (1.5, 2.5)
        # are multi-HR longshots and not the "will he hit a HR" prop.
        for o in m.get("outcomes", []):
            name = (o.get("name") or "").lower()
            desc = o.get("description") or ""
            price = o.get("price")
            point = o.get("point")
            # Filter: only Over side at the standard 0.5 line
            if name != "over":
                continue
            if point is None or abs(point - STANDARD_LINE) > 0.001:
                continue
            if price is None or not desc:
                continue
            players.append({
                "player":      desc,
                "game":        f"{away} @ {home}",
                "away_team":   away,
                "home_team":   home,
                "first_pitch": event.get("commence_time"),
                "price":       int(price),
                "line":        point,
            })
    return players


def race_safe_git_pull():
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


def _target_game_date(events, now_et):
    now_utc = datetime.now(tz=timezone.utc)
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
    if not date_counts:
        return now_et.strftime("%Y-%m-%d")
    return max(date_counts.items(), key=lambda x: x[1])[0]


def main(force=False, min_gap_min=30):
    """Run the HR line-movement snapshot.

    force: bypass freshness guard (used by manual refresh button).
    min_gap_min: skip if last snapshot for target date is < this many min ago.

    Returns: dict with status, path, snapshot_n, n_players, target_date.
    """
    api_key = _resolve_api_key()
    if not api_key:
        print("ERROR: THE_ODDS_API_KEY not set", file=sys.stderr)
        return {"status": "no_api_key"}

    now = datetime.now(tz=EASTERN)
    out_dir = os.path.join(ROOT, "hr_line_movement")
    os.makedirs(out_dir, exist_ok=True)

    print(f"=== HR Line Movement snapshot ({BOOK}) — run at "
          f"{now.strftime('%I:%M %p ET')} ===")
    race_safe_git_pull()

    events = _fetch_events(api_key)
    print(f"  {len(events)} MLB events found")

    target_date = _target_game_date(events, now)
    out_path = os.path.join(out_dir, f"{target_date}.json")
    print(f"  Target game date: {target_date}  →  {out_path}")

    now_utc = datetime.now(tz=timezone.utc)
    all_players = []
    for ev in events:
        try:
            ct = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ct <= now_utc:
            continue
        if ct.astimezone(EASTERN).date().strftime("%Y-%m-%d") != target_date:
            continue
        data = _fetch_hr_props(api_key, ev["id"])
        if data:
            all_players.extend(_parse_event(data))

    # Deduplicate by (player, game) — keep the tighter (shorter) price
    seen = {}
    for p in all_players:
        k = (p["player"], p["game"])
        if k not in seen or (p["price"] is not None and
                              seen[k]["price"] is not None and
                              p["price"] < seen[k]["price"]):
            seen[k] = p
    players = list(seen.values())
    print(f"  {len(players)} HR props from DK for {target_date}")

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
                print(f"  [SKIP] Last snapshot {age_min:.0f} min ago "
                      f"(< {min_gap_min} min gap).")
                return {
                    "status": "skipped_fresh",
                    "path": out_path,
                    "snapshot_n": len(snapshots),
                    "n_players": snapshots[-1].get("n_players", 0),
                    "age_min": age_min,
                }
        except Exception:
            pass

    snapshots.append({
        "captured_at": now.isoformat(),
        "n_players":   len(players),
        "players":     players,
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
        "n_players": len(players),
        "target_date": target_date,
    }


if __name__ == "__main__":
    import sys as _sys
    force = "--force" in _sys.argv
    main(force=force)
