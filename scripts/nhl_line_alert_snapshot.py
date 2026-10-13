"""
NHL Line Alert snapshot — records actionable plays to nhl_line_alert_history/YYYY-MM-DD.json.

Run from GitHub Actions cron to capture LINE SPLIT plays throughout the day.
No Streamlit dependency — pure stdlib.

Usage:
    THE_ODDS_API_KEY=xxx python scripts/nhl_line_alert_snapshot.py
"""
import json
import os
import ssl
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASTERN = ZoneInfo("America/New_York")
TRACKED_BOOKS = {"pinnacle", "williamhill_us", "fanatics"}
HISTORY_DIR = os.path.join(ROOT, "nhl_line_alert_history")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _get(url, timeout=20):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    return json.loads(resp.read()), resp.headers.get("x-requests-remaining", "?")


def fetch_games(api_key):
    events, quota = _get(
        f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/events?apiKey={api_key}"
    )
    games = []
    for ev in events:
        eid  = ev["id"]
        home = ev["home_team"]
        away = ev["away_team"]
        ct   = ev["commence_time"]
        try:
            data, _ = _get(
                f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/events/{eid}/odds"
                f"?apiKey={api_key}&regions=us,eu,uk&markets=team_totals&oddsFormat=american"
            )
        except Exception as e:
            print(f"  Skipping {away} @ {home}: {e}")
            continue

        sides = defaultdict(dict)
        for bm in data.get("bookmakers", []):
            bk = bm["key"]
            if bk not in TRACKED_BOOKS:
                continue
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "team_totals":
                    continue
                for out in mkt.get("outcomes", []):
                    team  = out.get("description", "")
                    name  = out.get("name", "")
                    price = out.get("price")
                    point = out.get("point")
                    if name == "Over":
                        sides[team][bk] = {"line": point, "over": price}
                    elif name == "Under":
                        if bk not in sides[team]:
                            sides[team][bk] = {}
                        sides[team][bk]["under"] = price

        if sides:
            games.append({
                "game": f"{away} @ {home}",
                "away": away,
                "home": home,
                "commence_time": ct,
                "sides": dict(sides),
            })

    return games, quota


def detect_plays(games):
    plays = []
    for g in games:
        game_name = g["game"]
        ct = g["commence_time"]
        try:
            gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
            game_time = gt.strftime("%I:%M %p")
        except Exception:
            game_time = "?"

        for team in [g["away"], g["home"]]:
            cur = g["sides"].get(team, {})
            if not cur:
                continue

            pin = cur.get("pinnacle", {})
            wil = cur.get("williamhill_us", {})
            fan = cur.get("fanatics", {})

            pin_line = pin.get("line")
            wil_line = wil.get("line")
            fan_line = fan.get("line")

            if not pin_line:
                continue

            all_lines = [l for l in [pin_line, wil_line, fan_line] if l is not None]
            unique = set(l for l in all_lines if l != 0.5)
            if len(unique) <= 1:
                continue

            non_pin = [
                (bk, ln, cur.get(bk, {}).get("over"), cur.get(bk, {}).get("under"))
                for bk, ln in [("williamhill_us", wil_line), ("fanatics", fan_line)]
                if ln is not None and ln != 0.5
            ]
            if not non_pin:
                continue

            max_entry = max(non_pin, key=lambda x: x[1])
            min_entry = min(non_pin, key=lambda x: x[1])

            if pin_line < max_entry[1]:
                plays.append({
                    "game":     game_name,
                    "time":     game_time,
                    "team":     team,
                    "bet":      "UNDER",
                    "line":     max_entry[1],
                    "book":     max_entry[0],
                    "price":    max_entry[3],
                    "why":      f"LINE SPLIT — Pinnacle dropped to {pin_line}",
                    "priority": 2,
                })
            elif pin_line > min_entry[1]:
                plays.append({
                    "game":     game_name,
                    "time":     game_time,
                    "team":     team,
                    "bet":      "OVER",
                    "line":     min_entry[1],
                    "book":     min_entry[0],
                    "price":    min_entry[2],
                    "why":      f"LINE SPLIT — Pinnacle raised to {pin_line}",
                    "priority": 2,
                })

    return plays


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

    now_et = datetime.now(tz=EASTERN)
    print(f"NHL Line Alert snapshot — {now_et.strftime('%Y-%m-%d %I:%M %p ET')}")

    games, quota = fetch_games(api_key)
    print(f"  {len(games)} games fetched  |  quota remaining: {quota}")

    # Sort by start time (earliest first)
    games.sort(key=lambda g: g.get("commence_time", ""))
    plays = detect_plays(games)
    print(f"  {len(plays)} actionable plays detected")

    if not plays:
        print("  Nothing to record.")
        return

    os.makedirs(HISTORY_DIR, exist_ok=True)
    today = now_et.strftime("%Y-%m-%d")
    path  = os.path.join(HISTORY_DIR, f"{today}.json")

    existing = {}
    if os.path.exists(path):
        try:
            existing = json.load(open(path, encoding="utf-8"))
        except Exception:
            existing = {}

    snap_key = now_et.strftime("%H:%M")
    existing[snap_key] = {
        "captured_at": now_et.isoformat(),
        "plays":       plays,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    print(f"  Saved → {path}")


if __name__ == "__main__":
    main()
