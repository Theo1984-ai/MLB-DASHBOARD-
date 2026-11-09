"""
Anytime-TD tracker (NFL / CFB).

An anytime-TD prop has one line (0.5), so there is no line split to find. The only edge is a soft book paying
more than Pinnacle's fair price. For every player Pinnacle prices (both sides, so its margin can be removed),
the first sighting is logged with Pinnacle's fair chance, the best soft-book price at that moment and the
expected value of that price. The grader settles each record after the game (rushing, receiving and return TDs
count, passing TDs don't), so the Results table shows two things:
  1. does a Pinnacle-priced player score as often as Pinnacle says (calibration), and
  2. does betting the best soft-book price make money, in total and when the price beats Pinnacle's fair price.
Records use the same shape as the First Seen log, so the existing grader settles them.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from scripts import best_plays as bp
from scripts import first_seen as fs

LOG_SUB = "td_props"
DONE_FILE = "td_props_done.json"
MARKET = "player_anytime_td"
BOOKS = fs.FOLLOWERS
SPORTS = ("NFL", "CFB")
# The player's other props shown next to the TD price (key in the log -> market name in the snapshot).
EXTRA = {"rush_yds": "player_rush_yds", "rec_yds": "player_reception_yds", "recs": "player_receptions"}


def _imp(o):
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def fair_prob(over, under):
    """Pinnacle's chance that the player scores, with its margin removed."""
    a, b = _imp(over), _imp(under)
    return a / (a + b)


def other_props(mks):
    """Pinnacle line and prices for the player's rushing yards, receiving yards and receptions, plus the range of
    lines the soft books post: {"rush_yds": {"line", "over", "under", "lo", "hi"}, ...} (only those that exist)."""
    out = {}
    for key, market in EXTRA.items():
        bd = mks.get(market) or {}
        pin = bd.get("pinnacle") or {}
        if pin.get("line") is None:
            continue
        soft = [bd[b]["line"] for b in BOOKS if b in bd and bd[b].get("line") is not None]
        out[key] = {"line": pin["line"], "over": pin.get("over"), "under": pin.get("under"),
                    "lo": min(soft) if soft else None, "hi": max(soft) if soft else None,
                    # soft books whose line is below Pinnacle's (an Over line split) and how many soft books post it
                    "n": len(soft), "over_off": sum(1 for x in soft if x < pin["line"])}
    return out


SPLIT_KEYS = ("rec_yds", "rush_yds")      # the yards props whose Over splits are ranked on the TD page
SPLIT_MIN = 3                             # NFL receiving yards Overs with 3+ books off: 50-28 (64%, +15u) in week 1


def over_split(props):
    """(books off, key) for the player's strongest Over line split on receiving or rushing yards, or (0, None)."""
    best = (0, None)
    for k in SPLIT_KEYS:
        n = (props or {}).get(k, {}).get("over_off") or 0
        if n > best[0]:
            best = (n, k)
    return best


def detect_plays(sport, snap, snap_time):
    """One dict per player Pinnacle prices (both sides) in a props snapshot, for games that haven't started."""
    out = []
    for g in (snap or {}).get("games", []):
        try:
            if fs._parse(g["commence_time"]) <= snap_time:
                continue
        except Exception:  # noqa: BLE001
            continue
        for who, mks in g.get("players", {}).items():
            bd = mks.get(MARKET)
            if not bd:
                continue
            pin = bd.get("pinnacle") or {}
            if pin.get("over") is None or pin.get("under") is None:
                continue
            soft = [(b, bd[b]["over"]) for b in BOOKS if b in bd and bd[b].get("over") is not None]
            if not soft:
                continue
            book, price = max(soft, key=lambda x: x[1])
            p = fair_prob(pin["over"], pin["under"])
            dec = 1 + price / 100 if price > 0 else 1 + 100 / -price
            out.append({"sport": sport, "kind": "prop", "game": g["game"], "who": who, "market": "anytime_td",
                        "bet": "OVER", "commence_time": g["commence_time"], "line": 0.5, "price": price,
                        "book": book, "pin_over": pin["over"], "pin_under": pin["under"],
                        "fair": round(p, 4), "ev": round(p * dec - 1, 4), "n_books": len(soft),
                        "props": other_props(mks)})
    return out


def fetch_live(key, sport, now_et, days=7, workers=6):
    """
    Live anytime-TD prices (plus the rushing, receiving and receptions lines shown beside them) for games that
    haven't started, today and the next `days` days. Returns (snapshot, quota); the snapshot has the same shape
    as a saved props snapshot, so detect_plays reads it. Costs about 2 odds credits per market per game.
    """
    cfg = bp.SPORTS[sport]
    events, quota = bp._get(f"{bp.ODDS_API}/sports/{cfg['key']}/events?apiKey={key}")
    games = []
    for ev in events:
        try:
            start = fs._parse(ev["commence_time"]).astimezone(fs.EASTERN)
        except Exception:  # noqa: BLE001
            continue
        if start > now_et and (start.date() - now_et.date()).days <= days:
            games.append(ev)
    markets = [MARKET] + list(EXTRA.values())
    tracked = {"pinnacle"} | set(bp.PROP_FOLLOWERS)
    errors = []

    def one(ev):
        try:
            return ev, *bp._fetch_event_props(key, cfg["key"], ev, markets, tracked)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ev['away_team']} @ {ev['home_team']}: {e}")
            return ev, None, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, games))
    out = []
    for ev, props, q in results:
        if q is not None:
            quota = q
        if props:
            out.append({"game": f"{ev['away_team']} @ {ev['home_team']}", "away": ev["away_team"],
                        "home": ev["home_team"], "commence_time": ev["commence_time"],
                        "started": False, "players": props})
    return {"captured_at": now_et.isoformat(), "quota": quota, "games": out, "errors": errors}, quota


def update_log(log, plays, iso, date):
    """Log the first sighting of each player; later sightings only refresh last_seen. Returns the new count."""
    new = 0
    for p in plays:
        pid = bp.pick_id(date, p)
        stamp = {"t": iso, "line": 0.5, "price": p["price"], "book": p["book"], "pin_over": p["pin_over"],
                 "pin_under": p["pin_under"], "fair": p["fair"], "ev": p["ev"], "n_books": p["n_books"],
                 "props": p.get("props") or {}}
        if pid not in log:
            rec = {k: p[k] for k in ("sport", "kind", "game", "who", "market", "bet", "commence_time")}
            rec.update({"first_seen": stamp, "last_seen": stamp, "result": None})
            log[pid] = rec
            new += 1
        else:
            log[pid]["last_seen"] = stamp
    return new


def catch_up(date, list_names, load, save, log_dir="best_plays_log"):
    """
    Log the TD prices from every saved NFL/CFB props snapshot of `date` that hasn't been processed, stamped with
    the time the snapshot was taken. A progress file remembers which snapshots are done. Returns the new count.
    list_names(folder) -> [file names]; load(path) -> data or {}; save(path, data, commit_msg).
    """
    done_path = f"{log_dir}/{DONE_FILE}"
    done = load(done_path) or {}
    seen = set(done.get(date, []))
    todo = sorted((m.group(2), m.group(1), n) for n in list_names(f"{log_dir}/snapshots/{date}")
                  for m in [re.fullmatch(r"([A-Z]+)_(\d{4})_props\.json", n)]
                  if m and m.group(1) in SPORTS and n not in seen)
    if not todo:
        return 0
    path = f"{log_dir}/{LOG_SUB}/{date}.json"
    log = load(path) or {}
    new = 0
    for hhmm, sport, name in todo:
        snap = load(f"{log_dir}/snapshots/{date}/{name}") or {}
        try:
            t = fs._parse(snap["captured_at"])
        except Exception:  # noqa: BLE001
            t = datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=fs.EASTERN)
        new += update_log(log, detect_plays(sport, snap, t), t.isoformat(), date)
        seen.add(name)
    save(path, log, f"TD props {date}: {new} new")
    done[date] = sorted(seen)
    for old in sorted(done)[:-3]:
        del done[old]
    save(done_path, done, f"TD props progress {date}")
    return new
