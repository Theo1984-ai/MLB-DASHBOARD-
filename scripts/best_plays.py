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
import statistics
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
    tot = ctx.get("total") or {}
    if tot.get("line") is not None:
        out["total"] = tot["line"]
        if tot.get("over") is not None and tot.get("under") is not None:
            out["total_over"], out["total_under"] = tot["over"], tot["under"]
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


def build_game_rows(sport, tt_snap, first_tt, game_snap, props_snap, plays, now_et):
    """
    One row per open game: Pinnacle's game lines, each team's expected score and how it moved since
    the day's first snapshot, the annotated plays in that game, and a verdict on whether the props'
    lean agrees with the game market. `plays` should be the annotated, uncapped, open plays.
    """
    now_m = team_means(sport, tt_snap)
    first_m = team_means(sport, first_tt) if first_tt else {}
    games = {}
    for snap in (tt_snap, props_snap, game_snap):
        for g in (snap or {}).get("games", []):
            games.setdefault(g["game"], g)
    thr = SHIFT_MIN.get(sport, 0.5)
    rows = []
    for name, g in games.items():
        try:
            start = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        if start <= now_et:
            continue
        away, home = g["away"], g["home"]
        sides = next((x["sides"] for x in (tt_snap or {}).get("games", []) if x["game"] == name), {})
        teams = []
        for t in (away, home):
            bd = sides.get(t, {})
            pin = bd.get("pinnacle") or {}
            soft = [v["line"] for b, v in bd.items() if b != "pinnacle" and v.get("line") is not None]
            mean, first = now_m.get((name, t)), first_m.get((name, t))
            teams.append({
                "team": t, "line": pin.get("line"), "over": pin.get("over"), "under": pin.get("under"),
                "mean": None if mean is None else round(mean, 2),
                "shift": None if mean is None or first is None else round(mean - first, 2),
                "soft": sorted(set(soft)), "n_soft": len(soft),
                "soft_median": statistics.median(soft) if soft else None,
                "soft_max": max(soft) if soft else None, "soft_min": min(soft) if soft else None,
                "n_higher": sum(1 for l in soft if pin.get("line") is not None and l > pin["line"]),
                "n_lower": sum(1 for l in soft if pin.get("line") is not None and l < pin["line"]),
                "off": sum(1 for l in soft if pin.get("line") is not None and l != pin["line"]),
            })
        gplays = sorted((p for p in plays if p["game"] == name),
                        key=lambda p: -p.get("adj", p["edge"]))
        # Props lean: scoring-stat plays only, +1 per Over, -1 per Under, by team
        lean, lean_w = {}, {}
        for p in gplays:
            ctx = p.get("ctx") or {}
            if ctx.get("tag", "n/a") == "n/a":
                continue
            key = ctx.get("team") or "game"
            d = lean.setdefault(key, {"over": 0, "under": 0})
            d["over" if p["bet"] == "OVER" else "under"] += 1
            w = p["stale"] / p["n_books"] if p.get("n_books") else 1.0
            lean_w[key] = lean_w.get(key, 0.0) + (w if p["bet"] == "OVER" else -w)
        agree, conflict = [], []
        for t in teams:
            d = lean.get(t["team"])
            net = (d["over"] - d["under"]) if d else 0
            if net and t["shift"] is not None and abs(t["shift"]) >= thr:
                side = "Over" if net > 0 else "Under"
                msg = (f"props lean {side} on {t['team']} ({d['over']}O/{d['under']}U) and its expected "
                       f"score moved {t['shift']:+.1f}")
                (agree if net * t["shift"] > 0 else conflict).append(msg)
        if "game" in lean:
            d = lean["game"]
            net = d["over"] - d["under"]
            shifts = [t["shift"] for t in teams if t["shift"] is not None]
            avg = sum(shifts) / len(shifts) if shifts else None
            if net and avg is not None and abs(avg) >= thr:
                msg = (f"props lean {'Over' if net > 0 else 'Under'} ({d['over']}O/{d['under']}U, team unknown) "
                       f"and the game's expected score moved {avg:+.1f} per team")
                (agree if net * avg > 0 else conflict).append(msg)
        verdict_tag = "conflict" if conflict else "agree" if agree else "none"
        rows.append({
            "sport": sport, "game": name, "away": away, "home": home, "commence_time": g["commence_time"],
            "lines": game_line_info(sport, game_snap, name, None), "teams": teams,
            "plays": gplays, "lean": lean, "lean_w": lean_w, "verdict_tag": verdict_tag,
            "verdict": "; ".join(conflict or agree) or "no clear lean against the game market",
        })
    rows.sort(key=lambda r: (r["commence_time"], r["game"]))
    return rows


# ── Game predictions: winner, score and Over/Under from the plays ───────────────────────────

PRED_SIGMA = {"NBA": 12.0, "NFL": 13.0, "CFB": 14.0, "NHL": 2.5}   # std dev of the final margin
PROP_UNIT = {"NBA": 1.0, "NFL": 1.0, "CFB": 1.0, "NHL": 0.1}       # score nudge per prop play, x0.25
GAME_TOTAL_SIGMA = {"NBA": 18.0, "NFL": 13.5, "CFB": 15.0}         # std dev of a game's combined score
OU_LEAN_MEDIUM, OU_LEAN_HIGH = 0.03, 0.06                          # chance above 50% for Medium / High


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _total_spec(sport):
    """Distribution of a game's combined score: Poisson for hockey, normal otherwise."""
    return ("pois", None) if sport == "NHL" else ("norm", GAME_TOTAL_SIGMA.get(sport, 15.0))


def _fit_total_mean(spec, line, p_over):
    """Mean of the combined score that makes P(total > line) equal p_over."""
    kind, sigma = spec
    p = min(max(p_over, 0.02), 0.98)
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


def _total_prob_over(spec, line, mean):
    kind, sigma = spec
    if kind == "norm":
        return 1 - _norm_cdf((line - mean) / sigma)
    return _pois_sf(int(math.floor(line)) + 1, mean)


def predict_row(sport, row):
    """
    Predict one game from a `build_game_rows` row.
      1. Start from Pinnacle's expected score for each team (its team total, line and price).
      2. Nudge each team's score by its prop plays (0.25 x one unit per net Over; Unders subtract; a
         play counts by the share of books that are off Pinnacle), capped at 2 units, and split any
         plays with no known team across both teams.
      3. Winner: the higher predicted score. Win probability is the normal margin model, averaged
         with Pinnacle's moneyline when we have it.
      4. Over/Under: start from Pinnacle's game total and its price (the sharp market's own
         expectation), add the props nudge, and read off the chance of Over vs Under at Pinnacle's
         line. Confidence comes from how far that chance is from 50%. Soft-book lines and price
         outliers do not move the call; price-aware team-total value is shown as a note.
    Returns None when either team has no Pinnacle team total.
    """
    a, h = row["teams"][0], row["teams"][1]
    if a["mean"] is None or h["mean"] is None:
        return None
    unit = PROP_UNIT.get(sport, 1.0)
    lean = row["lean"]
    lean_w = row.get("lean_w", {})

    def net(key):                       # net Overs minus Unders, each weighted by books off Pinnacle
        return lean_w.get(key, 0.0)

    g = net("game")
    adj_a = _clamp(0.25 * net(a["team"]) + 0.125 * g, -2.0, 2.0) * unit
    adj_h = _clamp(0.25 * net(h["team"]) + 0.125 * g, -2.0, 2.0) * unit
    sa, sh = a["mean"] + adj_a, h["mean"] + adj_h
    margin = sh - sa
    p_model = _norm_cdf(margin / PRED_SIGMA.get(sport, 12.0))
    ln = row["lines"]
    market = None
    if ln.get("fav") and ln.get("fav_win_prob"):
        market = ln["fav_win_prob"] if ln["fav"] == row["home"] else 1 - ln["fav_win_prob"]
    p_home = p_model if market is None else (p_model + market) / 2
    winner = row["home"] if p_home >= 0.5 else row["away"]
    win_prob = max(p_home, 1 - p_home)
    pin_fav = row["home"] if h["mean"] >= a["mean"] else row["away"]
    pin_p_home = _norm_cdf((h["mean"] - a["mean"]) / PRED_SIGMA.get(sport, 12.0))

    # Over/Under: the sharp market's own expectation plus the props nudge.
    spec = _total_spec(sport)
    mkt_mean_tt = a["mean"] + h["mean"]                      # what the two team totals say
    nudge = adj_a + adj_h
    pin_total = ln.get("total")
    if pin_total is not None:
        ref, ref_kind = pin_total, "Pinnacle game total"
        if ln.get("total_over") is not None and ln.get("total_under") is not None:
            p_mkt_over = devig(ln["total_over"], ln["total_under"])[0]
        else:
            p_mkt_over = 0.5
        base_mean = _fit_total_mean(spec, ref, p_mkt_over)
    else:
        ref = (a["line"] + h["line"]) if a["line"] is not None and h["line"] is not None \
            else round(mkt_mean_tt * 2) / 2
        ref_kind = "sum of Pinnacle team totals"
        p_mkt_over = None
        base_mean = mkt_mean_tt
    pred_total = base_mean + nudge
    p_over = _total_prob_over(spec, ref, pred_total)
    ou_pick = "Over" if p_over >= 0.5 else "Under"
    ou_prob = max(p_over, 1 - p_over)
    ou_conf = ("High" if ou_prob - 0.5 >= OU_LEAN_HIGH else
               "Medium" if ou_prob - 0.5 >= OU_LEAN_MEDIUM else "Low")
    ou_gap = abs(pred_total - ref)
    # keep the two predicted scores adding up to the predicted total (the margin is unchanged)
    delta = (pred_total - (sa + sh)) / 2
    sa, sh = sa + delta, sh + delta
    soft_total = None

    reasons = [f"Pinnacle team totals imply {a['team']} {a['mean']:.1f}, {h['team']} {h['mean']:.1f}."]
    for t, ad in ((a, adj_a), (h, adj_h)):
        d = lean.get(t["team"])
        if d:
            reasons.append(f"{t['team']}: {d['over']} Over / {d['under']} Under prop plays ({ad:+.2f} to its score).")
    if "game" in lean:
        d = lean["game"]
        reasons.append(f"{d['over']} Over / {d['under']} Under prop plays with the team unknown, split across both teams.")
    for t in (a, h):
        if t["shift"] is not None and abs(t["shift"]) >= SHIFT_MIN.get(sport, 0.5):
            reasons.append(f"{t['team']}'s expected score moved {t['shift']:+.1f} since the day's first snapshot.")
    if market is not None:
        reasons.append(f"Pinnacle moneyline gives {row['home']} {market * 100:.0f}% to win.")
    if ln.get("fav") and ln.get("fav_spread") is not None:
        reasons.append(f"Spread: {ln['fav']} {ln['fav_spread']:+.1f}.")
    if ln.get("blowout"):
        reasons.append("Blowout risk: starters may sit, which hurts the Over.")
    if p_mkt_over is not None:
        reasons.append(f"Pinnacle's game total is {ref:.1f} ({p_mkt_over * 100:.0f}% Over after removing the vig).")
    else:
        reasons.append(f"No Pinnacle game total saved, so the line is the sum of the team totals ({ref:.1f}).")
    reasons.append(f"Props lean adds {nudge:+.2f} to the total, so we expect {pred_total:.1f}.")
    reasons.append(f"Over/Under: {ou_pick} {ref:.1f} ({ou_prob * 100:.0f}% chance, {ou_conf} confidence)."
                   + (" Too close to the market line to call with confidence." if ou_conf == "Low" else ""))
    value = sorted((p for p in row.get("plays", []) if p.get("kind") == "team total"
                    and p["edge"] >= 0.01 and p["price"] >= -140), key=lambda p: -p["edge"])[:2]
    for p in value:
        reasons.append(f"Team-total value: {p['who']} {p['bet']} {p['line']} @ "
                       f"{BOOK_DISPLAY.get(p['book'], p['book'])} {p['price']:+d} (edge {p['edge'] * 100:+.1f}%).")
    return {
        "winner": winner, "win_prob": round(win_prob, 3),
        "win_conf": "High" if win_prob >= 0.70 else "Medium" if win_prob >= 0.60 else "Low",
        "pred_away": round(sa, 1), "pred_home": round(sh, 1),
        "pred_margin": round(abs(margin), 1), "pred_total": round(pred_total, 1),
        "pin_total": None if pin_total is None else round(pin_total, 1),
        "soft_total": None if soft_total is None else round(soft_total, 1),
        "ou_ref": round(ref, 1), "ou_ref_kind": ref_kind,
        "ou_pick": ou_pick, "ou_gap": round(ou_gap, 1), "ou_conf": ou_conf, "ou_prob": round(ou_prob, 3),
        "pin_favorite": pin_fav, "pin_win_prob": round(max(pin_p_home, 1 - pin_p_home), 3),
        "market_win_prob_home": None if market is None else round(market, 3),
        "adj_away": round(adj_a, 2), "adj_home": round(adj_h, 2), "reasons": reasons,
    }


def predict_rows(sport, rows):
    for r in rows:
        r["pred"] = predict_row(sport, r)
    return rows


def pred_key(date, sport, game):
    return "|".join([date, sport, game])


HIST_KEYS = ("t", "winner", "win_prob", "pred_away", "pred_home", "pred_total", "ou_pick", "ou_ref", "ou_conf")
HIST_MAX = 30


def update_predictions(log, rows, snap_time, date):
    """
    Store each game's first prediction, its latest one (the latest is what gets graded) and a short
    history of every snapshot's prediction, so the page can show what changed.
    """
    n = 0
    for r in rows:
        pr = r.get("pred")
        if not pr:
            continue
        key = pred_key(date, r["sport"], r["game"])
        stamp = {"t": snap_time, **pr}
        entry = {k: stamp.get(k) for k in HIST_KEYS}
        if key not in log:
            first = {k: v for k, v in stamp.items() if k != "reasons"}
            log[key] = {"sport": r["sport"], "game": r["game"], "away": r["away"], "home": r["home"],
                        "commence_time": r["commence_time"], "first": first, "last": stamp,
                        "n_updates": 1, "history": [entry], "result": None}
            n += 1
        elif not log[key].get("result"):
            rec = log[key]
            rec["last"] = stamp
            rec["n_updates"] = rec.get("n_updates", 1) + 1
            hist = rec.setdefault("history", [])
            if not hist or hist[-1].get("t") != snap_time:
                hist.append(entry)
            del hist[:-HIST_MAX]
    return n


def prediction_changes(prev, cur):
    """
    What moved between two predictions (dicts with the HIST_KEYS fields). Returns a list of short
    strings, [] when nothing changed. `cur` can be a full prediction.
    """
    if not prev or not cur:
        return []
    out = []
    if prev["winner"] != cur["winner"]:
        out.append(f"⚠️ winner flipped: was {prev['winner']} ({prev['win_prob'] * 100:.0f}%) → "
                   f"now {cur['winner']} ({cur['win_prob'] * 100:.0f}%)")
    elif abs(cur["win_prob"] - prev["win_prob"]) >= 0.005:
        arrow = "▲" if cur["win_prob"] > prev["win_prob"] else "▼"
        out.append(f"{cur['winner']} win chance {prev['win_prob'] * 100:.0f}% → {cur['win_prob'] * 100:.0f}% {arrow}")
    if abs(cur["pred_total"] - prev["pred_total"]) >= 0.05:
        out.append(f"predicted total {prev['pred_total']:.1f} → {cur['pred_total']:.1f}")
    if prev.get("ou_pick") and cur.get("ou_pick"):
        if prev["ou_pick"] != cur["ou_pick"]:
            out.append(f"⚠️ Over/Under flipped: was {prev['ou_pick']} {prev['ou_ref']} → "
                       f"now {cur['ou_pick']} {cur['ou_ref']}")
        elif prev.get("ou_ref") != cur.get("ou_ref"):
            out.append(f"{cur['ou_pick']} line {prev['ou_ref']} → {cur['ou_ref']}")
        if prev.get("ou_conf") != cur.get("ou_conf"):
            out.append(f"Over/Under confidence {prev['ou_conf']} → {cur['ou_conf']}")
    return out


def grade_predictions(log, now_et, fetch=_get_plain, finished_after_hours=4):
    """Grade stored predictions against ESPN final scores. Anything it can't find stays ungraded."""
    todo = defaultdict(list)
    for rec in log.values():
        if rec.get("result"):
            continue
        try:
            start = datetime.fromisoformat(rec["commence_time"].replace("Z", "+00:00")).astimezone(EASTERN)
        except Exception:  # noqa: BLE001
            continue
        if (now_et - start).total_seconds() < finished_after_hours * 3600:
            continue
        todo[(rec["sport"], start.strftime("%Y-%m-%d"))].append(rec)
    counts = {"graded": 0, "pending": 0, "errors": []}
    for (sport, date_str), recs in todo.items():
        try:
            events = espn_events(sport, date_str, fetch)
        except Exception as e:  # noqa: BLE001
            counts["errors"].append(f"{sport} {date_str} scoreboard: {e}")
            continue
        for rec in recs:
            ev = next((e for e in events if team_match(e["home"], rec["home"])
                       and team_match(e["away"], rec["away"])), None)
            if not ev or not ev["completed"] or ev.get("home_score") is None or ev.get("away_score") is None:
                counts["pending"] += 1
                continue
            ah, aa = ev["home_score"], ev["away_score"]
            actual_winner = rec["home"] if ah > aa else rec["away"] if aa > ah else None
            last = rec["last"]
            total = ah + aa
            ref = last.get("ou_ref")
            if ref is None or last.get("ou_pick") is None:
                ou = None
            elif total == ref:
                ou = "push"
            else:
                ou = "win" if (total > ref) == (last["ou_pick"] == "Over") else "loss"
            rec["result"] = {
                "actual_home": ah, "actual_away": aa, "actual_total": total, "actual_winner": actual_winner,
                "winner_correct": None if actual_winner is None else last["winner"] == actual_winner,
                "pin_fav_correct": None if actual_winner is None else last["pin_favorite"] == actual_winner,
                "ou_outcome": ou, "total_error": round(total - last["pred_total"], 1),
                "graded_at": now_et.isoformat(),
            }
            counts["graded"] += 1
    return counts


def summarize_predictions(logs):
    """Hit rates for winner calls, Pinnacle's favorite (the baseline) and Over/Under calls."""
    recs = [r for log in logs for r in log.values()]
    done = [r for r in recs if r.get("result")]
    rows = []

    def add(name, items, key):
        items = [r for r in items if key(r) is not None]
        hits = sum(1 for r in items if key(r) is True or key(r) == "win")
        decided = [r for r in items if key(r) != "push"]
        if items:
            rows.append({"group": name, "n": len(items), "hits": hits,
                         "pct": round(100 * hits / len(decided), 1) if decided else None})

    win = lambda r: r["result"]["winner_correct"]
    fav = lambda r: r["result"]["pin_fav_correct"]
    ou = lambda r: r["result"]["ou_outcome"]
    add("Winner (our call)", done, win)
    for c in ("High", "Medium", "Low"):
        add(f"Winner, {c} confidence", [r for r in done if r["last"]["win_conf"] == c], win)
    add("Winner (Pinnacle favorite, baseline)", done, fav)
    add("Over/Under (our call)", done, ou)
    for c in ("High", "Medium", "Low"):
        add(f"Over/Under, {c} confidence", [r for r in done if r["last"].get("ou_conf") == c], ou)
    errs = [abs(r["result"]["total_error"]) for r in done]
    return {"rows": rows, "n": len(recs), "graded": len(done),
            "mean_total_error": round(sum(errs) / len(errs), 1) if errs else None}


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


# ── Look-back table: every prediction next to what happened ──────────────────────────────────

def prediction_results_rows(logs_by_date):
    """
    Rows for the Predictions tab's look-back table. `logs_by_date` is {date: prediction log}.
    Newest day first, games in start order. Games that have not finished yet show as pending.
    """
    rows = []
    for date, log in logs_by_date.items():
        for rec in (log or {}).values():
            last = rec.get("last") or {}
            first = rec.get("first") or last
            res = rec.get("result")
            try:
                start = datetime.fromisoformat(rec["commence_time"].replace("Z", "+00:00")).astimezone(EASTERN)
            except Exception:  # noqa: BLE001
                continue
            mark = {"win": "✅", "loss": "❌", "push": "➖"}
            row = {
                "date": date, "start": start, "sport": rec["sport"], "game": rec["game"],
                "winner": last.get("winner"), "win_prob": last.get("win_prob"), "win_conf": last.get("win_conf"),
                "pred_score": f"{last.get('pred_away', 0):.0f}–{last.get('pred_home', 0):.0f}"
                              f" ({rec['away'].split()[-1]}–{rec['home'].split()[-1]})",
                "pred_total": last.get("pred_total"),
                "ou_pick": last.get("ou_pick"), "ou_ref": last.get("ou_ref"), "ou_conf": last.get("ou_conf"),
                "flipped": bool(first.get("winner") and last.get("winner") and first["winner"] != last["winner"]),
                "updates": rec.get("n_updates", 1),
                "done": bool(res),
            }
            if res:
                aa, ah = res.get("actual_away"), res.get("actual_home")
                row["actual_score"] = f"{aa:.0f}–{ah:.0f}" if aa is not None and ah is not None else "—"
                wc = res.get("winner_correct")
                row["winner_result"] = "✅" if wc is True else "❌" if wc is False else "—"
                fc = res.get("pin_fav_correct")
                row["fav_result"] = "✅" if fc is True else "❌" if fc is False else "—"
                row["ou_result"] = mark.get(res.get("ou_outcome"), "—")
                row["total_miss"] = res.get("total_error")
                row["actual_total"] = res.get("actual_total")
            else:
                row["actual_score"] = "⏳"
                row["winner_result"] = row["fav_result"] = row["ou_result"] = "⏳"
                row["total_miss"] = row["actual_total"] = None
            rows.append(row)
    rows.sort(key=lambda r: (r["date"], r["start"]))
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def results_day_summary(rows):
    """Winner / Over-Under record for a set of look-back rows."""
    done = [r for r in rows if r["done"]]
    wr = [r["winner_result"] for r in done]
    fr = [r["fav_result"] for r in done]
    orr = [r["ou_result"] for r in done]
    errs = [abs(r["total_miss"]) for r in done if r["total_miss"] is not None]
    return {"games": len(rows), "graded": len(done), "pending": len(rows) - len(done),
            "winner_hits": wr.count("✅"), "winner_n": wr.count("✅") + wr.count("❌"),
            "fav_hits": fr.count("✅"), "fav_n": fr.count("✅") + fr.count("❌"),
            "ou_w": orr.count("✅"), "ou_l": orr.count("❌"), "ou_p": orr.count("➖"),
            "mean_total_miss": round(sum(errs) / len(errs), 1) if errs else None}


# ── Day-by-day results ───────────────────────────────────────────────────────────────────────

def _pick_day(log):
    w = l = pu = 0
    profit = 0.0
    for r in (log or {}).values():
        res = r.get("result")
        if not res or res.get("outcome") not in ("win", "loss", "push"):
            continue
        profit += res.get("profit") or 0.0
        if res["outcome"] == "win":
            w += 1
        elif res["outcome"] == "loss":
            l += 1
        else:
            pu += 1
    n = w + l + pu
    return {"logged": len(log or {}), "graded": n, "w": w, "l": l, "p": pu,
            "win_pct": round(100 * w / (w + l), 1) if (w + l) else None,
            "roi_pct": round(100 * profit / n, 1) if n else None}


def _pred_day(log):
    done = [r for r in (log or {}).values() if r.get("result")]
    win = [r for r in done if r["result"].get("winner_correct") is not None]
    fav = [r for r in done if r["result"].get("pin_fav_correct") is not None]
    ou = [r["result"]["ou_outcome"] for r in done if r["result"].get("ou_outcome")]
    ow, ol, op = ou.count("win"), ou.count("loss"), ou.count("push")
    return {"games": len(log or {}), "graded": len(done),
            "winner_pct": round(100 * sum(1 for r in win if r["result"]["winner_correct"]) / len(win), 1) if win else None,
            "fav_pct": round(100 * sum(1 for r in fav if r["result"]["pin_fav_correct"]) / len(fav), 1) if fav else None,
            "ou_w": ow, "ou_l": ol, "ou_p": op,
            "ou_pct": round(100 * ow / (ow + ol), 1) if (ow + ol) else None}


def daily_summary(pick_logs, pred_logs, claude_logs=None):
    """
    One row per date, newest first, plus an 'All days' row at the end.
    Each argument is {date: log}: the model's picks, the game predictions and (optionally) Claude's
    own picks, which use the same record format as the model's.
    """
    claude_logs = claude_logs or {}
    dates = sorted(set(pick_logs) | set(pred_logs) | set(claude_logs), reverse=True)
    rows = []
    for d in dates:
        rows.append({"date": d, "model": _pick_day(pick_logs.get(d)), "claude": _pick_day(claude_logs.get(d)),
                     "pred": _pred_day(pred_logs.get(d))})

    def merge(logs):
        out = {}
        for d, lg in logs.items():
            for k, v in (lg or {}).items():
                out[f"{d}|{k}"] = v
        return out
    rows.append({"date": "All days", "model": _pick_day(merge(pick_logs)), "claude": _pick_day(merge(claude_logs)),
                 "pred": _pred_day(merge(pred_logs))})
    return rows


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
