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


def snapshot_sport(key, sport, now_et, workers=6):
    """
    Pull props + team totals for today's open games in one sport.
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
        props = totals = None
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
        return ev, props, totals, q2 or q1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, games))

    captured = now_et.isoformat()
    props_games, tt_games = [], []
    for ev, props, totals, q in results:
        if q is not None:
            quota = q
        base = {"game": f"{ev['away_team']} @ {ev['home_team']}", "away": ev["away_team"],
                "home": ev["home_team"], "commence_time": ev["commence_time"], "started": False}
        if props:
            props_games.append({**base, "players": props})
        if totals:
            tt_games.append({**base, "sides": totals})
    return {
        "sport": sport, "n_games": len(games), "errors": errors,
        "props_snap": {"captured_at": captured, "quota": quota, "games": props_games},
        "tt_snap": {"captured_at": captured, "quota": quota, "games": tt_games},
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
            rec.update({"first_seen": stamp, "last_seen": stamp, "checks": [], "result": None})
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
