"""
Best Plays — one snapshot across every sport with a game today.

For each sport with an open game today it pulls player props and team totals,
ranks the line splits against Pinnacle by an estimated edge, and logs every
pick so we can later check (a) whether the soft books caught up and (b) how the
picks actually graded.

No Streamlit imports here, so everything can be tested on its own.

How the edge is estimated
  1. Remove the vig from Pinnacle's over/under price -> Pinnacle's probability
     of the over at its own line.
  2. Fit a simple distribution (normal or Poisson) that reproduces that
     probability at Pinnacle's line.
  3. Read off the probability at the soft book's line.
  4. edge = fair probability - the book's break-even probability.
This is an estimate. It does not see injuries, lineups or minutes.
"""
from __future__ import annotations

import json
import math
import re
import ssl
import time
import unicodedata
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
_CTX = ssl.create_default_context()
ODDS_API = "https://api.the-odds-api.com/v4"
ESPN_API = "https://site.api.espn.com/apis/site/v2/sports"

PROP_FOLLOWERS = [
    "draftkings", "fanduel", "williamhill_us", "fanatics",
    "betrivers", "betonlineag", "espnbet",
]
_TT_BOOKS_BIG = [
    "draftkings", "fanduel", "williamhill_us", "fanatics", "bovada",
    "betrivers", "betonlineag", "espnbet", "mybookieag", "pointsbetus",
]

# Only markets with a real line are ranked (Yes/No markets like anytime TD have none).
SPORTS = {
    "NHL": {
        "key": "icehockey_nhl", "label": "NHL 🏒", "espn": "hockey/nhl",
        "prop_markets": ["player_points", "player_goals", "player_assists",
                         "player_shots_on_goal", "player_blocked_shots"],
        "tt_books": ["williamhill_us", "draftkings", "fanduel", "bovada", "betrivers", "fanatics"],
    },
    "NBA": {
        "key": "basketball_nba", "label": "NBA 🏀", "espn": "basketball/nba",
        "prop_markets": ["player_points", "player_rebounds", "player_assists", "player_threes",
                         "player_steals", "player_blocks", "player_turnovers"],
        "tt_books": _TT_BOOKS_BIG,
    },
    "NFL": {
        "key": "americanfootball_nfl", "label": "NFL 🏈", "espn": "football/nfl",
        "prop_markets": ["player_pass_tds", "player_pass_yds", "player_rush_yds",
                         "player_reception_yds", "player_receptions"],
        "tt_books": _TT_BOOKS_BIG,
    },
    "CFB": {
        "key": "americanfootball_ncaaf", "label": "CFB 🎓", "espn": "football/college-football",
        "prop_markets": ["player_pass_tds", "player_pass_yds", "player_rush_yds",
                         "player_reception_yds", "player_receptions"],
        "tt_books": _TT_BOOKS_BIG,
    },
}

BOOK_DISPLAY = {
    "pinnacle": "Pinnacle", "williamhill_us": "Caesars", "fanatics": "Fanatics",
    "draftkings": "DraftKings", "fanduel": "FanDuel", "bovada": "Bovada",
    "betrivers": "BetRivers", "betonlineag": "BetOnline", "espnbet": "ESPN Bet",
    "mybookieag": "MyBookie", "pointsbetus": "PointsBet",
}

# min_edge is a probability difference (0.01 = 1%); max_juice 140 means "price no worse than -140".
DEFAULT_TH = {"min_edge": 0.01, "min_stale": 3, "min_gap": 1.0, "max_juice": 140}


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get(url, timeout=25, retries=1):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mlb-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return json.loads(r.read()), r.headers.get("x-requests-remaining", "?")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    raise last


def _get_plain(url, timeout=25):
    """Like _get but for APIs with no quota header (ESPN)."""
    return _get(url, timeout=timeout)[0]


# ── Snapshot ──────────────────────────────────────────────────────────────────

def todays_open_games(events, now_et):
    """Events whose start is later today (ET)."""
    out = []
    for ev in events:
        try:
            gt = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(EASTERN)
        except Exception:  # noqa: BLE001
            continue
        if gt.date() == now_et.date() and gt > now_et:
            out.append(ev)
    return out


def _fetch_event_props(key, sport_key, ev, markets, tracked):
    data, quota = _get(
        f"{ODDS_API}/sports/{sport_key}/events/{ev['id']}/odds"
        f"?apiKey={key}&regions=us,eu&markets={','.join(markets)}&oddsFormat=american")
    players = {}
    for bm in data.get("bookmakers", []):
        bk = bm["key"]
        if bk not in tracked:
            continue
        for mkt in bm.get("markets", []):
            mk = mkt.get("key")
            if mk not in markets:
                continue
            for out in mkt.get("outcomes", []):
                player = out.get("description", "").strip()
                name, price, point = out.get("name", ""), out.get("price"), out.get("point")
                if not player or point is None:
                    continue
                slot = players.setdefault(player, {}).setdefault(mk, {}).setdefault(bk, {})
                if name == "Over":
                    slot["line"], slot["over"] = point, price
                elif name == "Under":
                    slot["under"] = price
    return players, quota


def _canonical_team(desc, away, home):
    """Map a bookmaker's team label onto the event's away/home team name."""
    if not desc:
        return desc
    dl = desc.strip().lower()
    for canonical in (away, home):
        cl = canonical.lower()
        if dl == cl or dl in cl or cl in dl:
            return canonical
        words = cl.split()
        if dl == words[0][:len(dl)] and len(dl) >= 2:
            return canonical
        if dl == words[-1][:len(dl)] and len(dl) >= 3:
            return canonical
        common = set(dl.split()) & set(words)
        if common and len(max(common, key=len)) >= 3:
            return canonical
    return desc


def _fetch_event_totals(key, sport_key, ev, tracked):
    data, quota = _get(
        f"{ODDS_API}/sports/{sport_key}/events/{ev['id']}/odds"
        f"?apiKey={key}&regions=us,eu,uk&markets=team_totals&oddsFormat=american")
    away, home = ev["away_team"], ev["home_team"]
    sides = defaultdict(dict)
    for bm in data.get("bookmakers", []):
        bk = bm["key"]
        if bk not in tracked:
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "team_totals":
                continue
            for out in mkt.get("outcomes", []):
                team = _canonical_team(out.get("description", ""), away, home)
                if not team or out.get("point") is None:
                    continue
                slot = sides[team].setdefault(bk, {})
                if out.get("name") == "Over":
                    slot["line"], slot["over"] = out["point"], out.get("price")
                elif out.get("name") == "Under":
                    slot["under"] = out.get("price")
    return dict(sides), quota


def _fetch_event_game(key, sport_key, ev):
    """Pinnacle's moneyline, spread and game total (bookmakers=pinnacle costs 1 credit per market)."""
    data, quota = _get(
        f"{ODDS_API}/sports/{sport_key}/events/{ev['id']}/odds"
        f"?apiKey={key}&bookmakers=pinnacle&markets=h2h,spreads,totals&oddsFormat=american")
    ctx = {}
    for bm in data.get("bookmakers", []):
        if bm.get("key") != "pinnacle":
            continue
        for mkt in bm.get("markets", []):
            k, outs = mkt.get("key"), mkt.get("outcomes", [])
            if k == "h2h":
                ctx["h2h"] = {o["name"]: o["price"] for o in outs if o.get("price") is not None}
            elif k == "spreads":
                ctx["spread"] = {o["name"]: {"line": o.get("point"), "price": o.get("price")} for o in outs}
            elif k == "totals":
                tot = {}
                for o in outs:
                    if o.get("name") == "Over":
                        tot["line"], tot["over"] = o.get("point"), o.get("price")
                    elif o.get("name") == "Under":
                        tot["under"] = o.get("price")
                ctx["total"] = tot
    return ctx, quota


def snapshot_sport(key, sport, now_et, workers=6):
    """
    Pull props, team totals and Pinnacle's game lines (moneyline / spread / total) for
    today's open games in one sport.
    Returns (result, quota). result is None when the sport has no open game today.
    Snapshot shapes match props_history/ and line_alert_snapshots/.
    """
    cfg = SPORTS[sport]
    events, quota = _get(f"{ODDS_API}/sports/{cfg['key']}/events?apiKey={key}")
    games = todays_open_games(events, now_et)
    if not games:
        return None, quota

    prop_tracked = {"pinnacle"} | set(PROP_FOLLOWERS)
    tt_tracked = {"pinnacle"} | set(cfg["tt_books"])
    errors = []

    def one(ev):
        props = totals = game_ctx = None
        try:
            props, q1 = _fetch_event_props(key, cfg["key"], ev, cfg["prop_markets"], prop_tracked)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ev['away_team']} @ {ev['home_team']} props: {e}")
            q1 = None
        try:
            totals, q2 = _fetch_event_totals(key, cfg["key"], ev, tt_tracked)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ev['away_team']} @ {ev['home_team']} totals: {e}")
            q2 = None
        try:
            game_ctx, q3 = _fetch_event_game(key, cfg["key"], ev)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ev['away_team']} @ {ev['home_team']} spread/moneyline: {e}")
            q3 = None
        return ev, props, totals, game_ctx, q3 or q2 or q1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, games))

    captured = now_et.isoformat()
    props_games, tt_games, game_games = [], [], []
    for ev, props, totals, game_ctx, q in results:
        if q is not None:
            quota = q
        base = {"game": f"{ev['away_team']} @ {ev['home_team']}", "away": ev["away_team"],
                "home": ev["home_team"], "commence_time": ev["commence_time"], "started": False}
        if props:
            props_games.append({**base, "players": props})
        if totals:
            tt_games.append({**base, "sides": totals})
        if game_ctx:
            game_games.append({**base, "ctx": game_ctx})
    return {
        "sport": sport, "n_games": len(games), "errors": errors,
        "props_snap": {"captured_at": captured, "quota": quota, "games": props_games},
        "tt_snap": {"captured_at": captured, "quota": quota, "games": tt_games},
        "game_snap": {"captured_at": captured, "quota": quota, "games": game_games},
    }, quota


# ── Model ─────────────────────────────────────────────────────────────────────

def implied(price):
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def decimal_odds(price):
    return 1 + (price / 100 if price > 0 else 100 / -price)


def devig(over_price, under_price):
    po, pu = implied(over_price), implied(under_price)
    t = po + pu
    return po / t, pu / t


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p):
    lo, hi = -9.0, 9.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _pois_sf(k, lam):
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def dist_spec(sport, market, line):
    """('norm', sigma) or ('pois', None) for this stat at this line."""
    m = market.replace("player_", "")
    if m == "team total":
        if sport == "NHL":
            return ("pois", None)
        return ("norm", {"NBA": 11.5, "NFL": 7.5, "CFB": 10.5}.get(sport, 10.0))
    if sport == "NHL":
        return ("pois", None)
    if sport == "NBA":
        if m == "points":
            return ("norm", 2.2 + 0.27 * line)
        if m in ("rebounds", "assists"):
            return ("pois", None) if line <= 5.5 else (
                "norm", (1.3 + 0.28 * line) if m == "rebounds" else (1.0 + 0.30 * line))
        return ("pois", None)  # threes, steals, blocks, turnovers
    # football
    if m == "pass_tds":
        return ("pois", None)
    if m == "receptions":
        return ("pois", None) if line <= 5.5 else ("norm", 0.45 * line + 0.8)
    if m == "pass_yds":
        return ("norm", 0.22 * line + 8)
    if m == "rush_yds":
        return ("norm", 0.45 * line + 3)
    if m == "reception_yds":
        return ("norm", 0.50 * line + 5)
    return ("norm", max(1.0, 0.4 * line))


def _is_half(x):
    return abs(x * 2 - round(x * 2)) < 1e-9 and abs(x - round(x)) > 1e-9


def make_over_prob(spec, pin_line, p_over_pin):
    """
    Fit the distribution so P(over pin_line) == p_over_pin; return a function
    L -> P(over L). Returns None when it can't be fitted (e.g. whole-number lines).
    """
    kind, sigma = spec
    if not _is_half(pin_line):
        return None
    p = min(max(p_over_pin, 0.02), 0.98)
    if kind == "norm":
        mu = pin_line - sigma * _norm_ppf(1 - p)
        return lambda L: 1 - _norm_cdf((L - mu) / sigma)
    k_pin = int(math.floor(pin_line)) + 1
    lo, hi = 1e-6, 400.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if _pois_sf(k_pin, mid) < p:
            lo = mid
        else:
            hi = mid
    lam = (lo + hi) / 2
    return lambda L: _pois_sf(int(math.floor(L)) + 1, lam)


def evaluate(sport, kind, market, who, game, bd, followers, th):
    """
    Best qualifying play for one prop / team total, or None.
    bd = {book: {line, over, under}} with a 'pinnacle' entry.
    """
    pin = bd.get("pinnacle") or {}
    pl, po_price, pu_price = pin.get("line"), pin.get("over"), pin.get("under")
    if pl is None or po_price is None or pu_price is None:
        return None
    p_over_pin, _ = devig(po_price, pu_price)
    over_prob = make_over_prob(dist_spec(sport, market, pl), pl, p_over_pin)
    if over_prob is None:
        return None

    quotes = [(b, bd[b]["line"], bd[b].get("over"), bd[b].get("under"))
              for b in followers if b in bd and bd[b].get("line") is not None]
    n_books = len(quotes)
    best = None
    for side in ("OVER", "UNDER"):
        group = [q for q in quotes if (q[1] < pl if side == "OVER" else q[1] > pl)]
        if len(group) < th["min_stale"]:
            continue
        for book, ln, over, under in group:
            price = over if side == "OVER" else under
            if price is None or not _is_half(ln):
                continue
            if price < -th["max_juice"]:
                continue
            gap = abs(ln - pl)
            if gap < th["min_gap"]:
                continue
            fair_over = over_prob(ln)
            fair = fair_over if side == "OVER" else 1 - fair_over
            edge = fair - implied(price)
            if edge < th["min_edge"]:
                continue
            if best is None or edge > best["edge"]:
                best = {
                    "sport": sport, "kind": kind, "game": game, "who": who,
                    "market": market.replace("player_", ""), "bet": side, "line": ln,
                    "book": book, "price": price, "gap": round(gap, 1),
                    "stale": len(group), "n_books": n_books,
                    "pin_line": pl, "pin_over": po_price, "pin_under": pu_price,
                    "fair": round(fair, 4), "edge": round(edge, 4),
                    "ev": round(fair * decimal_odds(price) - 1, 4),
                }
    return best


def find_plays(sport, props_snap, tt_snap, th):
    """All qualifying plays for one sport, best edge first. Adds commence_time."""
    cfg = SPORTS[sport]
    plays = []
    for g in (props_snap or {}).get("games", []):
        for player, mks in g.get("players", {}).items():
            for market, bd in mks.items():
                p = evaluate(sport, "prop", market, player, g["game"], bd, PROP_FOLLOWERS, th)
                if p:
                    p["commence_time"] = g["commence_time"]
                    plays.append(p)
    for g in (tt_snap or {}).get("games", []):
        for team, bd in g.get("sides", {}).items():
            p = evaluate(sport, "team total", "team total", team, g["game"], bd, cfg["tt_books"], th)
            if p:
                p["commence_time"] = g["commence_time"]
                plays.append(p)
    plays.sort(key=lambda p: -p["edge"])
    return plays


# ── Game context: do the props agree with the game market? ──────────────────

# Stats that move with how many points the team scores. Rebounds, steals, blocks, turnovers and
# shots on goal don't, so they get no tag.
SCORING_MARKETS = {
    "NBA": {"points", "assists", "threes", "team total"},
    "NHL": {"points", "goals", "assists", "team total"},
    "NFL": {"pass_yds", "pass_tds", "rush_yds", "reception_yds", "receptions", "team total"},
    "CFB": {"pass_yds", "pass_tds", "rush_yds", "reception_yds", "receptions", "team total"},
}
SHIFT_MIN = {"NBA": 0.5, "NFL": 0.7, "CFB": 0.7, "NHL": 0.1}      # expected-score move that counts
BLOWOUT_SPREAD = {"NBA": 10.0, "NFL": 10.0, "CFB": 14.0, "NHL": 99.0}
TAG_ADJ = {"backed": 0.005, "neutral": 0.0, "conflicts": -0.01, "n/a": 0.0}


def implied_mean(sport, line, over_price, under_price):
    """Expected points/goals that a team total implies (vig removed, price included)."""
    p, _ = devig(over_price, under_price)
    p = min(max(p, 0.02), 0.98)
    kind, sigma = dist_spec(sport, "team total", line)
    if kind == "norm":
        return line - sigma * _norm_ppf(1 - p)
    k = int(math.floor(line)) + 1
    lo, hi = 1e-6, 400.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if _pois_sf(k, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def team_means(sport, tt_snap):
    """{(game, team): expected score} from Pinnacle's team totals."""
    out = {}
    for g in (tt_snap or {}).get("games", []):
        for team, bd in g.get("sides", {}).items():
            pin = bd.get("pinnacle") or {}
            if None in (pin.get("line"), pin.get("over"), pin.get("under")):
                continue
            out[(g["game"], team)] = implied_mean(sport, pin["line"], pin["over"], pin["under"])
    return out


def player_team(roster, away, home, who):
    nm = norm_name(who)
    for team in (away, home):
        if nm in set((roster or {}).get(team, ())):
            return team
    return None


def game_line_info(sport, game_snap, game, team):
    """Pinnacle's moneyline / spread / total for the game, from `team`'s side when known."""
    ctx = next((g.get("ctx") for g in (game_snap or {}).get("games", []) if g["game"] == game), None)
    if not ctx:
        return {}
    out = {}
    h2h = ctx.get("h2h") or {}
    names = list(h2h)
    if len(names) == 2:
        pa, pb = devig(h2h[names[0]], h2h[names[1]])
        win = {names[0]: pa, names[1]: pb}
        if team in win:
            out["win_prob"] = round(win[team], 3)
        fav = max(win, key=win.get)
        out["fav"], out["fav_win_prob"] = fav, round(win[fav], 3)
    spreads = {t: v.get("line") for t, v in (ctx.get("spread") or {}).items() if v.get("line") is not None}
    if spreads:
        fav_s = min(spreads, key=spreads.get)
        out["fav"] = out.get("fav", fav_s)
        out["fav_spread"] = spreads[fav_s]
        out["blowout"] = abs(spreads[fav_s]) >= BLOWOUT_SPREAD.get(sport, 99.0)
    total = (ctx.get("total") or {}).get("line")
    if total is not None:
        out["total"] = total
    return out


def annotate(plays, sport, tt_snap, first_tt, game_snap, roster):
    """
    Tag each play against the game market. For a scoring stat, `backed` means the team's
    expected score moved the way the bet needs since the day's first snapshot; `conflicts` means
    it moved the other way. Sets p["ctx"] and p["adj"] (edge nudged by the tag, used to rank).
    """
    now_m = team_means(sport, tt_snap)
    first_m = team_means(sport, first_tt) if first_tt else {}
    for p in plays:
        away, _, home = p["game"].partition(" @ ")
        team = p["who"] if p["kind"] == "team total" else player_team(roster, away, home, p["who"])

        def shift(t):
            a, b = now_m.get((p["game"], t)), first_m.get((p["game"], t))
            return None if a is None or b is None else a - b

        if team:
            sh, scope = shift(team), "team"
        else:
            vals = [v for v in (shift(away), shift(home)) if v is not None]
            sh, scope = (sum(vals) / len(vals) if vals else None), "game"
        tag = "n/a"
        if p["market"] in SCORING_MARKETS.get(sport, ()) and sh is not None:
            signed = sh if p["bet"] == "OVER" else -sh
            thr = SHIFT_MIN.get(sport, 0.5)
            tag = "backed" if signed >= thr else "conflicts" if signed <= -thr else "neutral"
        p["ctx"] = {"team": team, "opp": (home if team == away else away) if team else None,
                    "scope": scope, "shift": None if sh is None else round(sh, 2), "tag": tag,
                    **game_line_info(sport, game_snap, p["game"], team)}
        p["adj"] = round(p["edge"] + TAG_ADJ[tag], 4)
    return plays


def cap_per_game(plays, n):
    """Keep the best `n` plays (by adjusted edge) per game; 0 means no cap."""
    if not n:
        return sorted(plays, key=lambda p: -p.get("adj", p["edge"]))
    seen, out = defaultdict(int), []
    for p in sorted(plays, key=lambda p: -p.get("adj", p["edge"])):
        if seen[p["game"]] < n:
            out.append(p)
            seen[p["game"]] += 1
    return out


# ── Rosters (player -> team), cached so ESPN is only hit when a team is new or stale ──────────

def _roster_names(data):
    names = set()
    for a in data.get("athletes", []):
        for it in (a.get("items") if isinstance(a, dict) and "items" in a else [a]):
            nm = (it or {}).get("displayName") or (it or {}).get("fullName") or ""
            if nm:
                names.add(norm_name(nm))
    return names


def espn_rosters(sport, team_names, cache=None, fetch=_get_plain, max_age_days=5, today=None):
    """
    cache: {team: {"names": [...], "fetched": "YYYY-MM-DD"}}, keyed by the Odds API team name.
    Returns (cache, errors, changed). Teams already cached and fresh are not fetched again.
    """
    cache = dict(cache or {})
    today = today or datetime.now(EASTERN).strftime("%Y-%m-%d")
    errors = []

    def stale(t):
        try:
            age = (datetime.fromisoformat(today) - datetime.fromisoformat(cache[t]["fetched"])).days
        except Exception:  # noqa: BLE001
            return True
        return age >= max_age_days

    need = [t for t in team_names if t not in cache or stale(t)]
    if not need:
        return cache, errors, False
    try:
        teams = fetch(f"{ESPN_API}/{SPORTS[sport]['espn']}/teams?limit=500")["sports"][0]["leagues"][0]["teams"]
    except Exception as e:  # noqa: BLE001
        return cache, [f"{sport} team list: {e}"], False
    changed = False
    for name in need:
        hits = [t["team"] for t in teams if norm_name(t["team"].get("displayName", "")) == norm_name(name)] \
            or [t["team"] for t in teams if team_match(t["team"].get("displayName", ""), name)]
        if not hits:
            errors.append(f"{sport} roster: no ESPN team for {name}")
            continue
        try:
            data = fetch(f"{ESPN_API}/{SPORTS[sport]['espn']}/teams/{hits[0]['id']}/roster")
            cache[name] = {"names": sorted(_roster_names(data)), "fetched": today}
            changed = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sport} roster {name}: {e}")
    return cache, errors, changed


def roster_lookup(cache):
    return {t: set(v.get("names", ())) for t, v in (cache or {}).items()}


# ── Log: first-seen picks + later checks ─────────────────────────────────────

def pick_id(date, p):
    return "|".join([date, p["sport"], p["kind"], p["game"], p["who"], p["market"], p["bet"]])


def update_log(log, plays, snap_time, date):
    """Add new picks (keeps the first-seen numbers) and refresh last_seen for repeats."""
    new = 0
    for p in plays:
        pid = pick_id(date, p)
        stamp = {"t": snap_time, "line": p["line"], "price": p["price"], "book": p["book"],
                 "pin_line": p["pin_line"], "pin_over": p["pin_over"], "pin_under": p["pin_under"],
                 "edge": p["edge"], "stale": p["stale"], "n_books": p["n_books"]}
        if pid not in log:
            rec = {k: p[k] for k in ("sport", "kind", "game", "who", "market", "bet",
                                      "commence_time")}
            rec.update({"first_seen": stamp, "last_seen": stamp, "checks": [], "result": None,
                        "context": p.get("ctx")})
            log[pid] = rec
            new += 1
        else:
            log[pid]["last_seen"] = stamp
    return new


def _index_snapshot(props_snap, tt_snap):
    idx = {}
    for g in (props_snap or {}).get("games", []):
        for player, mks in g.get("players", {}).items():
            for market, bd in mks.items():
                idx[(g["game"], player, market.replace("player_", ""))] = bd
    for g in (tt_snap or {}).get("games", []):
        for team, bd in g.get("sides", {}).items():
            idx[(g["game"], team, "team total")] = bd
    return idx


def add_checks(log, props_snap, tt_snap, snap_time):
    """For every logged pick still open in this snapshot, record where the lines are now."""
    idx = _index_snapshot(props_snap, tt_snap)
    n = 0
    for rec in log.values():
        if rec.get("result"):
            continue
        bd = idx.get((rec["game"], rec["who"], rec["market"]))
        if not bd:
            continue
        pin, book = bd.get("pinnacle") or {}, bd.get(rec["first_seen"]["book"]) or {}
        if pin.get("line") is None or book.get("line") is None:
            continue
        rec["checks"].append({"t": snap_time, "pin_line": pin.get("line"),
                              "pin_over": pin.get("over"), "pin_under": pin.get("under"),
                              "book_line": book.get("line"), "book_over": book.get("over"),
                              "book_under": book.get("under")})
        n += 1
    return n


# ── Grading (ESPN box scores) ────────────────────────────────────────────────

_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z\s]", " ", s)
    return " ".join(t for t in s.split() if t not in _SUFFIX)


def team_match(a, b):
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na or na.split()[-1] == nb.split()[-1]


def _num(x):
    if x is None:
        return None
    s = str(x).strip()
    if s in ("", "--", "-"):
        return None
    if re.fullmatch(r"\d+-\d+", s):       # "3-7" -> made 3
        return float(s.split("-")[0])
    try:
        return float(s)
    except ValueError:
        return None


def espn_events(sport, date_str, fetch=_get_plain):
    """Final scores for one day: list of dicts(id, home, away, home_score, away_score, completed)."""
    data = fetch(f"{ESPN_API}/{SPORTS[sport]['espn']}/scoreboard?dates={date_str.replace('-', '')}&limit=300")
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        row = {"id": ev.get("id"), "completed": bool(((ev.get("status") or {}).get("type") or {}).get("completed"))}
        for c in comp.get("competitors", []):
            side = c.get("homeAway")
            row[side] = (c.get("team") or {}).get("displayName", "")
            row[f"{side}_score"] = _num(c.get("score"))
        if "home" in row and "away" in row:
            out.append(row)
    return out


def espn_boxscore(sport, event_id, fetch=_get_plain):
    """{normalized player name: {stat label: value}} (football labels are 'group:LABEL')."""
    data = fetch(f"{ESPN_API}/{SPORTS[sport]['espn']}/summary?event={event_id}")
    football = sport in ("NFL", "CFB")
    players = {}
    for team in (data.get("boxscore") or {}).get("players", []):
        for group in team.get("statistics", []):
            labels = group.get("labels") or []
            gname = (group.get("name") or group.get("type") or "").lower()
            for a in group.get("athletes", []):
                nm = norm_name((a.get("athlete") or {}).get("displayName", ""))
                stats = a.get("stats") or []
                if not nm or not stats:
                    continue
                rec = players.setdefault(nm, {})
                for lab, val in zip(labels, stats):
                    v = _num(val)
                    if v is not None:
                        rec[f"{gname}:{lab}" if football else lab] = v
    return players


def player_stat(sport, market, rec):
    g = rec.get
    if sport == "NBA":
        key = {"points": "PTS", "rebounds": "REB", "assists": "AST", "threes": "3PT",
               "steals": "STL", "blocks": "BLK", "turnovers": "TO"}.get(market)
        return g(key) if key else None
    if sport == "NHL":
        if market == "points":
            return g("G") + g("A") if g("G") is not None and g("A") is not None else None
        key = {"goals": "G", "assists": "A", "shots_on_goal": "S", "blocked_shots": "BS"}.get(market)
        return g(key) if key else None
    key = {"pass_yds": "passing:YDS", "pass_tds": "passing:TD", "rush_yds": "rushing:YDS",
           "reception_yds": "receiving:YDS", "receptions": "receiving:REC"}.get(market)
    return g(key) if key else None


def settle(bet, line, actual):
    if actual is None:
        return "no_data"
    if actual == line:
        return "push"
    return "win" if (actual > line) == (bet == "OVER") else "loss"


def grade_log(log, now_et, fetch=_get_plain, finished_after_hours=4):
    """
    Grade ungraded picks whose game has had time to finish. Never guesses:
    anything it can't find stays ungraded. Returns counts.
    """
    todo = defaultdict(list)
    for pid, rec in log.items():
        if rec.get("result"):
            continue
        try:
            start = datetime.fromisoformat(rec["commence_time"].replace("Z", "+00:00")).astimezone(EASTERN)
        except Exception:  # noqa: BLE001
            continue
        if (now_et - start).total_seconds() < finished_after_hours * 3600:
            continue
        todo[(rec["sport"], start.strftime("%Y-%m-%d"))].append(rec)

    counts = {"graded": 0, "pending": 0, "no_data": 0, "errors": []}
    for (sport, date_str), recs in todo.items():
        try:
            events = espn_events(sport, date_str, fetch)
        except Exception as e:  # noqa: BLE001
            counts["errors"].append(f"{sport} {date_str} scoreboard: {e}")
            continue
        boxes = {}
        for rec in recs:
            away, _, home = rec["game"].partition(" @ ")
            ev = next((e for e in events if team_match(e["home"], home) and team_match(e["away"], away)), None)
            if not ev or not ev["completed"]:
                counts["pending"] += 1
                continue
            line, bet = rec["first_seen"]["line"], rec["bet"]
            if rec["kind"] == "team total":
                if team_match(rec["who"], ev["home"]):
                    actual = ev.get("home_score")
                elif team_match(rec["who"], ev["away"]):
                    actual = ev.get("away_score")
                else:
                    actual = None
            else:
                try:
                    if ev["id"] not in boxes:
                        boxes[ev["id"]] = espn_boxscore(sport, ev["id"], fetch)
                except Exception as e:  # noqa: BLE001
                    counts["errors"].append(f"{sport} box {ev['id']}: {e}")
                    continue
                prec = boxes[ev["id"]].get(norm_name(rec["who"]))
                actual = player_stat(sport, rec["market"], prec) if prec else None
            outcome = settle(bet, line, actual)
            price = rec["first_seen"]["price"]
            profit = {"win": decimal_odds(price) - 1, "loss": -1.0, "push": 0.0}.get(outcome)
            rec["result"] = {"outcome": outcome, "actual": actual, "profit": profit,
                             "graded_at": now_et.isoformat()}
            counts["no_data" if outcome == "no_data" else "graded"] += 1
    return counts


# ── Track-record summary ─────────────────────────────────────────────────────

def _bucket(edge):
    return "3%+" if edge >= 0.03 else "2–3%" if edge >= 0.02 else "1–2%"


def summarize(logs):
    """
    logs: list of {pick_id: record}. Returns rows for the track-record tables:
      by_group: [{group, n, wins, losses, pushes, win_pct, roi_pct}]
      catch_up: {n, narrowed, avg_gap_first, avg_gap_last}
    """
    picks = [r for log in logs for r in log.values()]
    groups = defaultdict(lambda: {"w": 0, "l": 0, "p": 0, "profit": 0.0})

    def add(name, rec):
        g = groups[name]
        oc = rec["result"]["outcome"]
        if oc == "win":
            g["w"] += 1
        elif oc == "loss":
            g["l"] += 1
        elif oc == "push":
            g["p"] += 1
        else:
            return
        g["profit"] += rec["result"].get("profit") or 0.0

    for r in picks:
        if not r.get("result") or r["result"]["outcome"] == "no_data":
            continue
        add("All picks", r)
        add(f"Sport: {r['sport']}", r)
        add(f"Type: {r['kind']}", r)
        add(f"Edge: {_bucket(r['first_seen']['edge'])}", r)
        tag = (r.get("context") or {}).get("tag")
        if tag and tag != "n/a":
            add(f"Game market: {tag}", r)
    rows = []
    for name, g in sorted(groups.items()):
        n = g["w"] + g["l"] + g["p"]
        dec = g["w"] + g["l"]
        rows.append({"group": name, "n": n, "wins": g["w"], "losses": g["l"], "pushes": g["p"],
                     "win_pct": round(100 * g["w"] / dec, 1) if dec else None,
                     "roi_pct": round(100 * g["profit"] / n, 1) if n else None})
    firsts, lasts = [], []
    for r in picks:
        if r.get("checks"):
            f = r["first_seen"]
            firsts.append(abs(f["line"] - f["pin_line"]))
            last = r["checks"][-1]
            lasts.append(abs(last["book_line"] - last["pin_line"]))
    catch = {"n": len(firsts),
             "narrowed": sum(1 for a, b in zip(firsts, lasts) if b < a),
             "avg_gap_first": round(sum(firsts) / len(firsts), 2) if firsts else None,
             "avg_gap_last": round(sum(lasts) / len(lasts), 2) if lasts else None}
    return {"by_group": rows, "catch_up": catch,
            "n_picks": len(picks),
            "n_graded": sum(1 for r in picks if r.get("result") and r["result"]["outcome"] in ("win", "loss", "push"))}
