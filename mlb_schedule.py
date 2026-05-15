"""
Fetches tonight's MLB schedule from the MLB Stats API and writes a CSV
with home/away teams, first pitch time (Eastern), probable starters, and ERA.
"""

import csv
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo  # Python 3.9+
import urllib.request
import ssl as _ssl_compat
_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
import urllib.error
import json
import os

EASTERN = ZoneInfo("America/New_York")
MLB_BASE = "https://statsapi.mlb.com/api/v1"


def fetch_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=10, context=_UNVERIFIED_SSL) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        print(f"Network error fetching {url}: {e}", file=sys.stderr)
        sys.exit(1)


def get_pitcher_era(person_id: int, season: int) -> str:
    url = (
        f"{MLB_BASE}/people/{person_id}/stats"
        f"?stats=season&season={season}&group=pitching"
    )
    data = fetch_json(url)
    try:
        splits = data["stats"][0]["splits"]
        if splits:
            era = splits[0]["stat"].get("era", "N/A")
            return str(era)
    except (KeyError, IndexError):
        pass
    return "N/A"


def parse_first_pitch_eastern(game_date_str: str) -> str:
    """Convert ISO 8601 UTC string from the API to Eastern time."""
    try:
        # API returns e.g. "2026-04-30T23:10:00Z"
        dt_utc = datetime.fromisoformat(game_date_str.replace("Z", "+00:00"))
        dt_et = dt_utc.astimezone(EASTERN)
        return dt_et.strftime("%I:%M %p ET")
    except (ValueError, AttributeError):
        return game_date_str


def main():
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    season = datetime.now(tz=EASTERN).year

    print(f"Fetching MLB schedule for {today}...")

    schedule_url = (
        f"{MLB_BASE}/schedule"
        f"?sportId=1&date={today}"
        f"&hydrate=probablePitcher,linescore,team"
    )
    schedule = fetch_json(schedule_url)

    dates = schedule.get("dates", [])
    if not dates:
        print(f"No games found for {today}.")
        sys.exit(0)

    games = dates[0].get("games", [])
    print(f"Found {len(games)} game(s). Fetching pitcher stats...")

    rows = []
    for game in games:
        away_team = game["teams"]["away"]["team"]["name"]
        home_team = game["teams"]["home"]["team"]["name"]
        first_pitch = parse_first_pitch_eastern(game.get("gameDate", ""))
        game_status = game.get("status", {}).get("abstractGameState", "Preview")

        away_pitcher_name = "TBD"
        away_pitcher_era = "N/A"
        home_pitcher_name = "TBD"
        home_pitcher_era = "N/A"

        away_pitcher = game["teams"]["away"].get("probablePitcher")
        if away_pitcher:
            away_pitcher_name = away_pitcher.get("fullName", "TBD")
            away_pitcher_id = away_pitcher.get("id")
            if away_pitcher_id:
                away_pitcher_era = get_pitcher_era(away_pitcher_id, season)

        home_pitcher = game["teams"]["home"].get("probablePitcher")
        if home_pitcher:
            home_pitcher_name = home_pitcher.get("fullName", "TBD")
            home_pitcher_id = home_pitcher.get("id")
            if home_pitcher_id:
                home_pitcher_era = get_pitcher_era(home_pitcher_id, season)

        rows.append({
            "Away Team":           away_team,
            "Home Team":           home_team,
            "First Pitch (ET)":    first_pitch,
            "Game Status":         game_status,
            "Away Starter":        away_pitcher_name,
            "Away Starter ERA":    away_pitcher_era,
            "Home Starter":        home_pitcher_name,
            "Home Starter ERA":    home_pitcher_era,
        })

        print(
            f"  {away_team} @ {home_team}  {first_pitch}"
            f"  |  {away_pitcher_name} ({away_pitcher_era})"
            f" vs {home_pitcher_name} ({home_pitcher_era})"
        )

    output_dir = os.path.dirname(os.path.abspath(__file__))
    filename = os.path.join(output_dir, f"MLB_homerun_{today}.csv")
    fieldnames = [
        "Away Team", "Home Team", "First Pitch (ET)", "Game Status",
        "Away Starter", "Away Starter ERA",
        "Home Starter", "Home Starter ERA",
    ]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved {len(rows)} game(s) to: {filename}")


if __name__ == "__main__":
    main()
