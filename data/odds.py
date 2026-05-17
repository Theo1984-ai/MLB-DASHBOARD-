"""
The Odds API client (https://the-odds-api.com).

Free tier: 500 requests/month. Each player-props pull is 1 request per event.
Returns DraftKings (and optionally other US books) HR prop odds in a
normalized format with quota tracking.

Sign up: https://the-odds-api.com/  →  email-only, get API key instantly.
"""

import urllib.request
import ssl
# System cert chain is broken on this Windows install — use unverified
# context for these public-data APIs. No auth/PII flows through these endpoints.
_UNVERIFIED_SSL = ssl._create_unverified_context()
import urllib.error
import urllib.parse
import json
from datetime import datetime, timezone

BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "baseball_mlb"
HR_MARKET = "batter_home_runs"  # standard market (BR/BO/ESPN BET)
HR_MARKET_ALT = "batter_home_runs_alternate"  # DK/FD/MGM/Caesars use this with point=0.5
# Sharp US books for HR props. BetOnline.ag intentionally excluded by default —
# their HR lines are routinely stale longshots (+18,000-+19,900) that don't
# match real market consensus.
DEFAULT_BOOKS = "draftkings,fanduel,betmgm,caesars"
SHARP_HR_BOOKS = "draftkings,fanduel,betmgm,caesars"  # alias for HR-specific use

# Alternate-line markets: higher-probability bets that price at negative odds
ALT_PROP_MARKETS = {
    "batter_hits":        "Hits",
    "batter_total_bases": "Total Bases",
}


def bdl_prop_rate_key(prop_type: str, line_value) -> str | None:
    """
    Maps a BallDontLie (prop_type, line_value) pair to the game-log rate field.
    line_value is the BDL string threshold, e.g. '0.5', '1.5'.

    hits / 0.5  → hits_1plus_rate
    hits / 1.5  → hits_2plus_rate
    total_bases / 0.5  → tb_1plus_rate
    total_bases / 1.5  → tb_2plus_rate
    total_bases / 2.5  → tb_3plus_rate
    """
    try:
        pt = float(line_value) if line_value is not None else None
    except (TypeError, ValueError):
        pt = None

    if prop_type == "hits":
        if pt is None or pt <= 0.5:
            return "hits_1plus_rate"
        if pt <= 1.5:
            return "hits_2plus_rate"
    elif prop_type == "total_bases":
        if pt is None or pt <= 0.5:
            return "tb_1plus_rate"
        if pt <= 1.5:
            return "tb_2plus_rate"
        if pt <= 2.5:
            return "tb_3plus_rate"
    elif prop_type == "hits_runs_rbis":
        if pt is None or pt <= 0.5:
            return "hrr_1plus_rate"
        if pt <= 1.5:
            return "hrr_2plus_rate"
        if pt <= 2.5:
            return "hrr_3plus_rate"
    return None


def alt_odds_rate_key(market_key: str, point) -> str | None:
    """
    Maps a (market_key, point) pair to the game-log rate field returned by
    mlb_api.get_batter_game_log_rates().  Returns None if unmapped.

    "Over 0.5 hits"       → hits_1plus_rate
    "Over 1.5 hits"       → hits_2plus_rate
    "Over 0.5 tot bases"  → tb_1plus_rate
    "Over 1.5 tot bases"  → tb_2plus_rate
    "Over 2.5 tot bases"  → tb_3plus_rate
    """
    try:
        pt = float(point) if point is not None else None
    except (TypeError, ValueError):
        pt = None

    if market_key == "batter_hits":
        if pt is None or pt <= 0.5:
            return "hits_1plus_rate"
        if pt <= 1.5:
            return "hits_2plus_rate"

    elif market_key == "batter_total_bases":
        if pt is None or pt <= 0.5:
            return "tb_1plus_rate"
        if pt <= 1.5:
            return "tb_2plus_rate"
        if pt <= 2.5:
            return "tb_3plus_rate"

    return None


class OddsAPIError(Exception):
    pass


_last_quota = {"used": None, "remaining": None, "fetched_at": None}


def get_quota() -> dict:
    """Return the most recently observed quota usage from API responses."""
    return dict(_last_quota)


def _fetch(url: str) -> tuple:
    """Returns (parsed_json, headers_dict). Raises OddsAPIError on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mlb-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as resp:
            body = resp.read().decode()
            headers = dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()[:300]
        except Exception:
            err_body = ""
        if e.code == 401:
            raise OddsAPIError("Invalid API key (401). Check that THE_ODDS_API_KEY is set correctly.")
        if e.code == 422:
            raise OddsAPIError(f"Bad request (422): {err_body}")
        if e.code == 429:
            raise OddsAPIError("Quota exceeded (429). Wait until next month or upgrade plan.")
        raise OddsAPIError(f"HTTP {e.code}: {err_body}")
    except urllib.error.URLError as e:
        raise OddsAPIError(f"Network error: {e.reason}")

    # Track quota from response headers
    used = headers.get("x-requests-used")
    rem  = headers.get("x-requests-remaining")
    if used is not None:
        _last_quota["used"] = int(used) if used.isdigit() else used
    if rem is not None:
        _last_quota["remaining"] = int(rem) if rem.isdigit() else rem
    _last_quota["fetched_at"] = datetime.now(tz=timezone.utc).isoformat()

    try:
        return json.loads(body), headers
    except json.JSONDecodeError:
        raise OddsAPIError(f"Bad JSON response: {body[:200]}")


def get_events(api_key: str) -> list:
    """
    List upcoming MLB events with their Odds-API-internal event IDs.
    Each event has: id, commence_time (ISO UTC), home_team, away_team.
    Costs: 1 request.
    """
    if not api_key:
        raise OddsAPIError("Missing API key")
    url = f"{BASE}/sports/{SPORT_KEY}/events?apiKey={urllib.parse.quote(api_key)}"
    data, _ = _fetch(url)
    return data or []


def get_game_lines(api_key: str, event_id: str,
                   bookmakers: str = "draftkings,fanduel,betmgm,betrivers,caesars") -> list:
    """
    Pull moneyline (h2h), spread (run-line), and total (over/under) odds
    for a single MLB event. Game-line markets are available on the free
    tier — no plan upgrade needed.

    Returns one row per (bookmaker, market, outcome). Each row has:
      bookmaker, bookmaker_name, market, name, point (None for h2h),
      american_odds, decimal_odds, implied_prob.

    Costs: 1 request per call (regardless of book count).
    """
    if not api_key or not event_id:
        return []
    params = urllib.parse.urlencode({
        "apiKey":     api_key,
        "regions":    "us",
        "markets":    "h2h,spreads,totals",
        "bookmakers": bookmakers,
        "oddsFormat": "american",
    })
    url = f"{BASE}/sports/{SPORT_KEY}/events/{event_id}/odds?{params}"
    try:
        data, _ = _fetch(url)
    except OddsAPIError:
        return []

    rows = []
    for bm in (data or {}).get("bookmakers", []):
        bm_key = bm.get("key")
        bm_title = bm.get("title")
        for market in bm.get("markets", []):
            mk = market.get("key")
            if mk not in ("h2h", "spreads", "totals"):
                continue
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                if price is None:
                    continue
                rows.append({
                    "bookmaker":      bm_key,
                    "bookmaker_name": bm_title,
                    "market":         mk,
                    "name":           outcome.get("name"),
                    "point":          outcome.get("point"),
                    "american_odds":  int(price),
                    "decimal_odds":   american_to_decimal(int(price)),
                    "implied_prob":   american_to_implied(int(price)),
                })
    return rows


def get_hr_odds(api_key: str, event_id: str,
                bookmakers: str = DEFAULT_BOOKS) -> list:
    """
    Player HR prop odds for a single event. Returns one row per
    (bookmaker, player) with the standard "Yes hit ≥1 HR" line.

    DraftKings, FanDuel, BetMGM, and Caesars expose HR props under the
    `batter_home_runs_alternate` market (filter to point=0.5 for the
    standard Yes line).  BetRivers / BetOnline / ESPN BET use the
    standard `batter_home_runs` market with name=Yes/No.

    We pull BOTH markets and combine — gives full coverage across all
    books on the Starter tier ($30/mo) and above.

    Costs: 2 requests per call (one per market).
    """
    if not api_key or not event_id:
        return []

    rows = []
    seen_keys = set()  # dedupe (bookmaker, player) — prefer first (alternate has DK/FD)

    # 1. Alternate market — DK / FD / MGM / Caesars use this with point=0.5
    params_alt = urllib.parse.urlencode({
        "apiKey":     api_key,
        "regions":    "us",
        "markets":    "batter_home_runs_alternate",
        "bookmakers": bookmakers,
        "oddsFormat": "american",
    })
    try:
        data_alt, _ = _fetch(f"{BASE}/sports/{SPORT_KEY}/events/{event_id}/odds?{params_alt}")
    except OddsAPIError:
        data_alt = {}

    for bm in (data_alt or {}).get("bookmakers", []):
        bm_key = bm.get("key"); bm_title = bm.get("title")
        for market in bm.get("markets", []):
            if market.get("key") != "batter_home_runs_alternate":
                continue
            for outcome in market.get("outcomes", []):
                # Alternate market uses name=Over/Under, description=player, point=line
                name = outcome.get("name", "").strip().lower()
                if name != "over":
                    continue   # skip "Under" (won't homer side)
                point = outcome.get("point")
                if point is None or float(point) != 0.5:
                    continue   # only standard "Yes 1+ HR" line
                player = outcome.get("description") or outcome.get("name")
                price = outcome.get("price")
                if price is None or not player:
                    continue
                k = (bm_key, player)
                if k in seen_keys: continue
                seen_keys.add(k)
                rows.append({
                    "bookmaker":      bm_key,
                    "bookmaker_name": bm_title,
                    "player":         player,
                    "american_odds":  int(price),
                    "decimal_odds":   american_to_decimal(int(price)),
                    "implied_prob":   american_to_implied(int(price)),
                })

    # 2. Standard market — BR / BO / ESPN BET use this
    params_std = urllib.parse.urlencode({
        "apiKey":     api_key,
        "regions":    "us",
        "markets":    HR_MARKET,
        "bookmakers": bookmakers,
        "oddsFormat": "american",
    })
    try:
        data_std, _ = _fetch(f"{BASE}/sports/{SPORT_KEY}/events/{event_id}/odds?{params_std}")
    except OddsAPIError:
        data_std = {}

    for bm in (data_std or {}).get("bookmakers", []):
        bm_key = bm.get("key"); bm_title = bm.get("title")
        for market in bm.get("markets", []):
            if market.get("key") != HR_MARKET:
                continue
            for outcome in market.get("outcomes", []):
                player = outcome.get("description") or outcome.get("name")
                yes_no = outcome.get("name", "").strip().lower()
                if yes_no in ("no", "under"):
                    continue
                price = outcome.get("price")
                if price is None or not player:
                    continue
                k = (bm_key, player)
                if k in seen_keys: continue   # already got from alternate market
                seen_keys.add(k)
                rows.append({
                    "bookmaker":      bm_key,
                    "bookmaker_name": bm_title,
                    "player":         player,
                    "american_odds":  int(price),
                    "decimal_odds":   american_to_decimal(int(price)),
                    "implied_prob":   american_to_implied(int(price)),
                })
    return rows


def get_hrr_odds(api_key: str, event_id: str,
                 point: float = 1.5,
                 bookmakers: str = DEFAULT_BOOKS) -> list:
    """
    Player Hits + Runs + RBIs (H+R+R) prop odds for a single event.

    Most sharp books post H+R+R only under the `batter_hits_runs_rbis_alternate`
    market, which exposes multiple thresholds (0.5, 1.5, 2.5, 3.5, 4.5) per
    player. We filter to a single `point` (default 1.5 = the most common line —
    needs any 2 of hits/runs/RBIs to cash). The market is typically Over-only.

    Currently DraftKings is the most consistent poster; FanDuel/BetMGM/Caesars
    often add coverage 4-8h before first pitch.

    Returns one row per (bookmaker, player):
      {bookmaker, bookmaker_name, player, point,
       american_odds, decimal_odds, implied_prob}

    Costs: 1 request per call.
    """
    if not api_key or not event_id:
        return []

    rows = []
    seen_keys = set()   # dedupe (bookmaker, player)

    params = urllib.parse.urlencode({
        "apiKey":     api_key,
        "regions":    "us",
        "markets":    "batter_hits_runs_rbis_alternate",
        "bookmakers": bookmakers,
        "oddsFormat": "american",
    })
    try:
        data, _ = _fetch(f"{BASE}/sports/{SPORT_KEY}/events/{event_id}/odds?{params}")
    except OddsAPIError:
        data = {}

    for bm in (data or {}).get("bookmakers", []):
        bm_key = bm.get("key"); bm_title = bm.get("title")
        for market in bm.get("markets", []):
            if market.get("key") != "batter_hits_runs_rbis_alternate":
                continue
            for outcome in market.get("outcomes", []):
                name = (outcome.get("name") or "").strip().lower()
                if name != "over":
                    continue   # Over-only market
                pt = outcome.get("point")
                if pt is None or float(pt) != float(point):
                    continue   # only the requested threshold
                player = outcome.get("description") or outcome.get("name")
                price = outcome.get("price")
                if price is None or not player:
                    continue
                k = (bm_key, player)
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                rows.append({
                    "bookmaker":      bm_key,
                    "bookmaker_name": bm_title,
                    "player":         player,
                    "point":          float(pt),
                    "american_odds":  int(price),
                    "decimal_odds":   american_to_decimal(int(price)),
                    "implied_prob":   american_to_implied(int(price)),
                })
    return rows


def get_alternate_prop_odds(
    api_key: str,
    event_id: str,
    bookmakers: str = DEFAULT_BOOKS,
    min_american: int = -1000,
    max_american: int = -200,
) -> list:
    """
    Fetch player props for hits and total-bases markets, filtered to a
    negative-odds band (default -1000 to -200, implied ~50 %–91 %).
    Returns Over/Yes outcomes only. Each row includes the `point` threshold.

    Costs: 1 request per call.
    """
    if not api_key or not event_id:
        return []
    markets = ",".join(ALT_PROP_MARKETS.keys())
    params = urllib.parse.urlencode({
        "apiKey":     api_key,
        "regions":    "us",
        "markets":    markets,
        "bookmakers": bookmakers,
        "oddsFormat": "american",
    })
    url = f"{BASE}/sports/{SPORT_KEY}/events/{event_id}/odds?{params}"
    try:
        data, _ = _fetch(url)
    except OddsAPIError:
        return []

    rows = []
    for bm in (data or {}).get("bookmakers", []):
        bm_key   = bm.get("key")
        bm_title = bm.get("title")
        for market in bm.get("markets", []):
            mk = market.get("key")
            if mk not in ALT_PROP_MARKETS:
                continue
            for outcome in market.get("outcomes", []):
                name_lower = (outcome.get("name") or "").strip().lower()
                if name_lower in ("under", "no"):
                    continue
                # Player name is in description when name == "Over"
                player = outcome.get("description") or outcome.get("name")
                if not player or player.strip().lower() in ("over", "under"):
                    continue
                point = outcome.get("point")
                price = outcome.get("price")
                if price is None:
                    continue
                price_int = int(price)
                # Keep only the target odds band (negative / heavy-favourite range)
                if price_int < min_american or price_int > max_american:
                    continue
                rows.append({
                    "bookmaker":      bm_key,
                    "bookmaker_name": bm_title,
                    "player":         player,
                    "market_key":     mk,
                    "market_label":   ALT_PROP_MARKETS[mk],
                    "point":          point,
                    "american_odds":  price_int,
                    "decimal_odds":   american_to_decimal(price_int),
                    "implied_prob":   american_to_implied(price_int),
                })
    return rows


# ---------- Odds helpers ----------

def american_to_decimal(american: int) -> float:
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def american_to_implied(american: int) -> float:
    """Implied probability with vig included (not de-vigged)."""
    if american > 0:
        return 100.0 / (american + 100.0)
    return abs(american) / (abs(american) + 100.0)


def decimal_to_implied(decimal: float) -> float:
    return 1.0 / decimal if decimal > 0 else 0.0


# ---------- Stale / suspect line filter ----------

# Lines below this implied probability AND offered by a single book are
# almost always either stale opening lines or limit-only display prices
# (BetRivers / BetOnline frequently leave longshot HR props at +18,000
# to +19,900 even when the true market is +400 to +800).
DEFAULT_MIN_IMPLIED        = 0.03   # 3% — anything below = +3266 American
DEFAULT_MAX_OUTLIER_RATIO  = 2.0    # if one book's implied < median/2, drop it
HIGH_EDGE_VERIFY_THRESHOLD = 0.20   # 20pp — flag pick for manual verification


def filter_stale_lines(rows: list,
                       min_implied: float = DEFAULT_MIN_IMPLIED,
                       require_multi_book: bool = True,
                       outlier_ratio: float = DEFAULT_MAX_OUTLIER_RATIO) -> tuple:
    """
    Drop suspect odds rows that are likely stale or limit-only.

    Filters applied (in order):
      1. SINGLE-BOOK LONGSHOT — if a player has only ONE bookmaker offer
         AND that offer's implied probability is below `min_implied`,
         drop that row. Real markets always have multiple books for any
         legit longshot.
      2. OUTLIER PRICE — if a player has multiple offers and one offer's
         implied probability is less than (median implied / outlier_ratio),
         drop that one offer (the others stay). Catches stale prices on
         a single book when others have updated.

    Rows are grouped by player name (case-insensitive). All grouping uses
    `outcome.player` for HR props or `outcome.name` for game-line outcomes.

    Returns:
        (filtered_rows, drop_log) where drop_log is a dict with keys:
            - 'single_book_longshot': count
            - 'outlier_price':        count
            - 'kept':                 count
    """
    if not rows:
        return [], {"single_book_longshot": 0, "outlier_price": 0, "kept": 0}

    # Group by player (HR props) or name (game lines)
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        key = (r.get("player") or r.get("name") or "").lower().strip()
        if not key:
            continue
        groups[key].append(r)

    kept = []
    dropped_single = 0
    dropped_outlier = 0

    for key, offers in groups.items():
        if len(offers) == 1:
            o = offers[0]
            ip = o.get("implied_prob", 1.0)
            if require_multi_book and ip < min_implied:
                dropped_single += 1
                continue
            kept.append(o)
            continue

        # Multiple offers — check for outliers
        implieds = sorted(o.get("implied_prob", 0) for o in offers)
        # median
        n = len(implieds)
        median = implieds[n // 2] if n % 2 else (implieds[n // 2 - 1] + implieds[n // 2]) / 2

        for o in offers:
            ip = o.get("implied_prob", 0)
            # Outlier = implied is more than `outlier_ratio` times BELOW the median
            if median > 0 and ip * outlier_ratio < median and ip < min_implied:
                dropped_outlier += 1
                continue
            kept.append(o)

    return kept, {
        "single_book_longshot": dropped_single,
        "outlier_price":        dropped_outlier,
        "kept":                 len(kept),
        "total_in":             len(rows),
    }


def is_suspect_edge(edge_pp: float, threshold: float = HIGH_EDGE_VERIFY_THRESHOLD * 100) -> bool:
    """
    Returns True when an edge is suspiciously large and the underlying line
    should be manually verified before placing a bet. Real markets rarely
    leave +20pp of edge sitting around — so when our model claims that
    much, the line is more likely stale/limit-only than truly mispriced.
    """
    return edge_pp is not None and edge_pp >= threshold


# ---------- Name normalization for joining model ↔ odds ----------

import re
_punct = re.compile(r"[.\-']")
_suffixes = re.compile(r"\s+(jr|sr|ii|iii|iv)\.?$", re.IGNORECASE)


def normalize_name(name: str) -> str:
    """
    Normalize "Acuña Jr., Ronald" / "Ronald Acuña Jr." → "ronald acuna"
    Lowercases, strips punctuation/suffixes/accents, removes commas.
    """
    if not name:
        return ""
    n = name.strip()
    # If "Last, First" — flip
    if "," in n:
        last, _, first = n.partition(",")
        n = f"{first.strip()} {last.strip()}"
    # Strip accents
    import unicodedata
    n = "".join(c for c in unicodedata.normalize("NFKD", n) if not unicodedata.combining(c))
    n = _punct.sub("", n)
    n = _suffixes.sub("", n)
    n = re.sub(r"\s+", " ", n).strip().lower()
    return n


if __name__ == "__main__":
    import os
    key = os.environ.get("THE_ODDS_API_KEY")
    if not key:
        print("Set THE_ODDS_API_KEY env var to test live API.")
        # offline tests
        print(f"american_to_implied(+250) = {american_to_implied(250):.4f}")
        print(f"american_to_implied(-150) = {american_to_implied(-150):.4f}")
        print(f"normalize 'Acuña Jr., Ronald' = '{normalize_name('Acuña Jr., Ronald')}'")
        print(f"normalize 'Ronald Acuña Jr.' = '{normalize_name('Ronald Acuña Jr.')}'")
    else:
        events = get_events(key)
        print(f"events: {len(events)}")
        if events:
            e = events[0]
            print(f"first: {e['away_team']} @ {e['home_team']} ({e['commence_time']})")
            odds = get_hr_odds(key, e["id"])
            print(f"HR prop offers: {len(odds)}")
            for r in odds[:3]:
                print(f"  {r['bookmaker']:10} {r['player']:25} {r['american_odds']:+5d}  ({r['implied_prob']*100:.1f}%)")
        print(f"quota: {get_quota()}")