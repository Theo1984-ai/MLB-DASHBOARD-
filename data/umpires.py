"""
MLB Umpire data integration.

Two sources:
  1. MLB Stats API  → today's home plate umpire per game (name + id)
  2. Covers.com     → career season O/U record, avg runs/game, strike%

Umpire impact on totals (from research):
  - Strike% > 66% (large zone) → pitchers dominate → ~0.4 fewer runs/game
  - Strike% < 62% (small zone) → batters walk more  → ~0.4 more runs/game
  - Under rate > 58% over career → meaningful pitcher-friendly lean
  - Under rate < 42% → meaningful batter-friendly lean
"""

import json
import re
import time
import urllib.request
import ssl as _ssl_compat
_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
import urllib.error
from typing import Optional

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    import pandas as pd
    import io as _io
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

LEAGUE_RUNS_PER_GAME = 9.1   # 2026 approximate
NEUTRAL_UNDER_RATE   = 0.50
NEUTRAL_STRIKE_PCT   = 64.5  # league average


# ── MLB Stats API ─────────────────────────────────────────────────────────────

def get_today_umpires(date_str: str) -> dict[int, dict]:
    """
    Return {game_pk: {name, id}} for today's home plate umpires.
    Uses MLB Stats API schedule with officials hydration.
    """
    url = (
        f"https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1&date={date_str}&hydrate=officials"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mlb-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
            data = json.loads(r.read())
    except Exception:
        return {}

    result: dict[int, dict] = {}
    for day in data.get("dates", []):
        for g in day.get("games", []):
            gid = g.get("gamePk")
            officials = g.get("officials", [])
            hp = next(
                (o for o in officials if o.get("officialType", "") == "Home Plate"),
                None,
            )
            if hp:
                official = hp.get("official", {})
                result[gid] = {
                    "name": official.get("fullName", "Unknown"),
                    "id":   official.get("id"),
                }
    return result


# ── Covers.com scraping ───────────────────────────────────────────────────────

def _covers_ump_stats(covers_id: int, year: int = 2026) -> dict:
    """
    Scrape career stats for one umpire from Covers.com.
    Returns dict with: under_rate, avg_runs, strike_pct, games, ou_record
    """
    if not _HAS_REQUESTS or not _HAS_PANDAS:
        return {}

    url = f"https://www.covers.com/sport/baseball/mlb/umpires/{year}/{covers_id}"
    try:
        r = _requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return {}
    except Exception:
        return {}

    try:
        tables = pd.read_html(_io.StringIO(r.text))
    except Exception:
        return {}

    result = {}

    # Table 1 — Game Stats (Average runs, Strike%)
    for t in tables:
        cols = list(t.columns)
        if len(cols) >= 2 and "Game Stats" in str(cols[0]):
            stats_dict = dict(zip(t.iloc[:, 0], t.iloc[:, 1]))
            avg_str    = str(stats_dict.get("Average", "")).replace(",", "")
            strike_str = str(stats_dict.get("Strike%", "")).replace("%", "").strip()
            try:
                result["avg_runs"]    = float(avg_str)
            except ValueError:
                result["avg_runs"]    = LEAGUE_RUNS_PER_GAME
            try:
                result["strike_pct"]  = float(strike_str)
            except ValueError:
                result["strike_pct"]  = NEUTRAL_STRIKE_PCT
            break

    # Table 3 — Vs Total O/U (Overall over-under)
    for t in tables:
        cols = list(t.columns)
        if "Vs Total" in str(cols[0]) and "O/U" in str(cols[1]):
            overall_row = t[t.iloc[:, 0].astype(str).str.lower() == "overall"]
            if not overall_row.empty:
                ou_str = str(overall_row.iloc[0, 1])   # e.g. "4 - 4"
                m = re.match(r"(\d+)\s*-\s*(\d+)", ou_str)
                if m:
                    over_n  = int(m.group(1))
                    under_n = int(m.group(2))
                    total   = over_n + under_n
                    result["over_n"]    = over_n
                    result["under_n"]   = under_n
                    result["games"]     = total
                    result["ou_record"] = f"{over_n}O-{under_n}U"
                    result["under_rate"] = under_n / total if total else NEUTRAL_UNDER_RATE
            break

    return result


def _find_covers_id(name: str, year: int = 2026) -> Optional[int]:
    """
    Scrape the Covers umpire list page to find the ID for a given umpire name.
    Name can be 'Hunter Wendelstedt' or 'Wendelstedt, Hunter'.
    """
    if not _HAS_REQUESTS:
        return None

    url = f"https://www.covers.com/sport/baseball/mlb/umpires"
    try:
        r = _requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 Chrome/120.0"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
    except Exception:
        return None

    # Normalize name for matching
    parts = name.strip().split()
    last  = parts[-1].lower() if parts else ""

    # Find all umpire links: /umpires/{year}/{id}
    pattern = rf'href="/sport/baseball/mlb/umpires/{year}/(\d+)"[^>]*>([^<]+)</a>'
    for m in re.finditer(pattern, r.text):
        ump_id   = int(m.group(1))
        ump_name = m.group(2).strip()
        # Match on last name
        if last and last in ump_name.lower():
            return ump_id
    return None


# Cache to avoid re-scraping same umpire multiple times per session
_covers_cache: dict[str, dict] = {}


def get_umpire_stats(name: str, year: int = 2026) -> dict:
    """
    High-level function: given an umpire's full name, return their betting stats.

    Returns dict with keys:
      name, under_rate, avg_runs, strike_pct, games, ou_record,
      impact  ('under' | 'over' | 'neutral')
      run_adj (multiplier to apply to expected total, e.g. 0.97 or 1.03)
    """
    cache_key = f"{name}_{year}"
    if cache_key in _covers_cache:
        return _covers_cache[cache_key]

    result = {
        "name":        name,
        "under_rate":  NEUTRAL_UNDER_RATE,
        "avg_runs":    LEAGUE_RUNS_PER_GAME,
        "strike_pct":  NEUTRAL_STRIKE_PCT,
        "games":       0,
        "ou_record":   "N/A",
        "impact":      "neutral",
        "run_adj":     1.00,
    }

    covers_id = _find_covers_id(name, year)
    if covers_id:
        time.sleep(0.2)   # be polite
        stats = _covers_ump_stats(covers_id, year)
        if stats:
            result.update(stats)

    # Derive impact and run adjustment
    under_rate  = result["under_rate"]
    strike_pct  = result["strike_pct"]
    avg_runs    = result["avg_runs"]
    games       = result["games"]

    if games >= 10:   # need a meaningful sample
        # Combine strike% and under rate signals
        tight_zone  = strike_pct > 65.5
        loose_zone  = strike_pct < 62.5
        under_lean  = under_rate > 0.56
        over_lean   = under_rate < 0.44

        if (tight_zone or under_lean) and avg_runs < LEAGUE_RUNS_PER_GAME:
            result["impact"]  = "under"
            result["run_adj"] = 0.97
        elif (loose_zone or over_lean) and avg_runs > LEAGUE_RUNS_PER_GAME:
            result["impact"]  = "over"
            result["run_adj"] = 1.03
        else:
            result["impact"]  = "neutral"
            result["run_adj"] = 1.00
    else:
        # Insufficient sample — use avg_runs vs league as weak signal
        if avg_runs < LEAGUE_RUNS_PER_GAME - 0.5:
            result["impact"]  = "under"
            result["run_adj"] = 0.98
        elif avg_runs > LEAGUE_RUNS_PER_GAME + 0.5:
            result["impact"]  = "over"
            result["run_adj"] = 1.02

    _covers_cache[cache_key] = result
    return result


if __name__ == "__main__":
    from datetime import date
    today = date.today().isoformat()
    print(f"Today's umpires ({today}):")
    umps = get_today_umpires(today)
    for gid, info in list(umps.items())[:5]:
        print(f"  GamePk {gid}: {info['name']}")

    # Test a known umpire
    print("\nLooking up Hunter Wendelstedt stats...")
    stats = get_umpire_stats("Hunter Wendelstedt")
    for k, v in stats.items():
        print(f"  {k}: {v}")
