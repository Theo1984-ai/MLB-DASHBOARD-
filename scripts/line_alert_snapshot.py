"""
Generic Line Alert snapshot script.
Records actionable LINE SPLIT plays to <sport>_line_alert_history/YYYY-MM-DD.json.

Usage:
    THE_ODDS_API_KEY=xxx python scripts/line_alert_snapshot.py <sport>

sport options: nfl, cfb, nba, mlb
"""
import json
import os
import ssl
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EASTERN = ZoneInfo("America/New_York")

SPORT_CONFIGS = {
    "nfl": {
        "api_key":     "americanfootball_nfl",
        "history_dir": os.path.join(ROOT, "nfl_line_alert_history"),
        "today_only":  False,
        "followers":   ["draftkings", "fanduel", "williamhill_us", "fanatics",
                        "bovada", "betrivers", "betonlineag", "espnbet",
                        "mybookieag", "pointsbetus"],
    },
    "cfb": {
        "api_key":     "americanfootball_ncaaf",
        "history_dir": os.path.join(ROOT, "cfb_line_alert_history"),
        "today_only":  False,
        "followers":   ["draftkings", "fanduel", "williamhill_us", "fanatics",
                        "bovada", "betrivers", "betonlineag", "espnbet",
                        "mybookieag", "pointsbetus"],
    },
    "nba": {
        "api_key":     "basketball_nba",
        "history_dir": os.path.join(ROOT, "nba_line_alert_history"),
        "today_only":  True,
        "followers":   ["draftkings", "fanduel", "williamhill_us", "fanatics",
                        "bovada", "betrivers", "betonlineag", "espnbet",
                        "mybookieag", "pointsbetus"],
    },
    "mlb": {
        "api_key":     "baseball_mlb",
        "history_dir": os.path.join(ROOT, "mlb_line_alert_history"),
        "today_only":  True,
        "followers":   ["draftkings", "fanduel", "williamhill_us", "fanatics",
                        "bovada", "betrivers", "betonlineag", "espnbet",
                        "mybookieag", "pointsbetus"],
    },
}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode    = ssl.CERT_NONE


def _get(url, timeout=20):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    return json.loads(resp.read()), resp.headers.get("x-requests-remaining", "?")


def fetch_games(api_key, sport_api_key, followers, today_only):
    tracked = {"pinnacle"} | set(followers)
    now_et  = datetime.now(tz=EASTERN)
    today   = now_et.date()

    events, quota = _get(
        f"https://api.the-odds-api.com/v4/sports/{sport_api_key}/events?apiKey={api_key}"
    )
    games = []
    for ev in events:
        eid  = ev["id"]
        home = ev["home_team"]
        away = ev["away_team"]
        ct   = ev["commence_time"]

        try:
            gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
            if today_only and gt.date() != today:
                continue
            if gt <= now_et:
                continue  # skip started games
        except Exception:
            continue

        try:
            data, _ = _get(
                f"https://api.the-odds-api.com/v4/sports/{sport_api_key}/events/{eid}/odds"
                f"?apiKey={api_key}&regions=us,eu,uk&markets=team_totals&oddsFormat=american"
            )
        except Exception as e:
            print(f"  Skipping {away} @ {home}: {e}")
            continue

        sides = defaultdict(dict)
        for bm in data.get("bookmakers", []):
            bk = bm["key"]
            if bk not in tracked:
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
                        sides[team].setdefault(bk, {})["under"] = price

        if sides:
            games.append({
                "game":           f"{away} @ {home}",
                "away":           away,
                "home":           home,
                "commence_time":  ct,
                "sides":          dict(sides),
            })

    return games, quota


def detect_plays(games, followers):
    plays = []
    for g in games:
        game_name = g["game"]
        ct        = g["commence_time"]
        try:
            gt        = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
            game_time = gt.strftime("%I:%M %p")
        except Exception:
            game_time = "?"

        for team in [g["away"], g["home"]]:
            cur = g["sides"].get(team, {})
            if not cur:
                continue

            pin      = cur.get("pinnacle", {})
            pin_line = pin.get("line")
            if not pin_line:
                continue

            non_pin = [
                (bk, cur[bk].get("line"), cur[bk].get("over"), cur[bk].get("under"))
                for bk in followers
                if bk in cur and cur[bk].get("line") is not None and cur[bk]["line"] != 0.5
            ]
            if not non_pin:
                continue

            all_lines = [pin_line] + [x[1] for x in non_pin]
            if len(set(l for l in all_lines if l != 0.5)) <= 1:
                continue

            max_entry = max(non_pin, key=lambda x: x[1])
            min_entry = min(non_pin, key=lambda x: x[1])

            if pin_line < max_entry[1] and max_entry[3] is not None:
                plays.append({
                    "game":           game_name,
                    "time":           game_time,
                    "commence_time":  ct,
                    "team":           team,
                    "bet":            "UNDER",
                    "line":           max_entry[1],
                    "book":           max_entry[0],
                    "price":          max_entry[3],
                    "why":            f"LINE SPLIT — Pinnacle dropped to {pin_line}",
                    "priority":       2,
                })
            elif pin_line > min_entry[1] and min_entry[2] is not None:
                plays.append({
                    "game":           game_name,
                    "time":           game_time,
                    "commence_time":  ct,
                    "team":           team,
                    "bet":            "OVER",
                    "line":           min_entry[1],
                    "book":           min_entry[0],
                    "price":          min_entry[2],
                    "why":            f"LINE SPLIT — Pinnacle raised to {pin_line}",
                    "priority":       2,
                })

    return plays


def main():
    if len(sys.argv) < 2:
        print("Usage: python line_alert_snapshot.py <sport>  (nfl | cfb | nba | mlb)", file=sys.stderr)
        sys.exit(1)

    sport = sys.argv[1].lower()
    if sport not in SPORT_CONFIGS:
        print(f"Unknown sport '{sport}'. Choose from: {list(SPORT_CONFIGS.keys())}", file=sys.stderr)
        sys.exit(1)

    cfg = SPORT_CONFIGS[sport]

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
    print(f"{sport.upper()} Line Alert snapshot — {now_et.strftime('%Y-%m-%d %I:%M %p ET')}")

    games, quota = fetch_games(api_key, cfg["api_key"], cfg["followers"], cfg["today_only"])
    print(f"  {len(games)} games fetched  |  quota remaining: {quota}")

    games.sort(key=lambda g: g.get("commence_time", ""))
    plays = detect_plays(games, cfg["followers"])
    print(f"  {len(plays)} actionable plays detected")

    if not plays:
        print("  Nothing to record.")
        return

    os.makedirs(cfg["history_dir"], exist_ok=True)
    today = now_et.strftime("%Y-%m-%d")
    path  = os.path.join(cfg["history_dir"], f"{today}.json")

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
