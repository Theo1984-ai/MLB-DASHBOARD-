"""
Rules for the Top Plays page: the First Seen plays whose record has been best so far.

  RULE_STALE   any play with 3+ soft books stale (you choose the sports, markets and side on the page;
               the default is NFL receiving yards Over: 71-51, 58%, +8.5% ROI so far)
  RULE_NHL     NHL shots on goal Over with 2+ books stale (55-22, 71%, +13% ROI so far)
  RULE_NBA     NBA threes Over, the CORE filter: line 1.5 with 1 to 2 books stale (78 plays, 77% vs 65% break-even,
               +18% ROI so far; everything else in NBA threes Overs is 63%, -3%)
  RULE_NBA_ALL every NBA threes Over (181 plays, 69%, +6% ROI), still shown, with the core ones starred
  RULE_TRIPLE  WATCH LIST, not an edge: an NBA player with an Over line split on points, assists AND rebounds in the same game
               (46 graded player-days: the three Overs hit 53% of the time, -10% ROI, all three the same night 13%)
  RULE_NCAAB   NCAAB team total line alert with a 3 or 4 point gap to Pinnacle, at -160 to -199, on BetRivers
               (about 67-73% vs 65% break-even, +3% to +9% ROI so far; thin, and heavy juice eats most of it)

A play's books stale = the most soft books that were off Pinnacle at its first sighting or its latest check.
Records are First Seen log records, so the same functions work on graded history and on today's open plays.
"""
import re
from collections import defaultdict

RULE_STALE = "3+ books stale"
RULE_NHL = "NHL shots Over, 2+ stale"
RULE_NHL_POINTS = "NHL points Over, 1+ stale"   # NOT PROVEN: Over 0.5 lost about -11% over 19 graded plays (63% vs 70% break-even)
RULE_NBA = "NBA threes Over, line 1.5 + 1-2 stale"
RULE_NBA_ALL = "NBA threes Over (all)"
RULE_NCAAB = "NCAAB team total, gap 3-4"
RULE_TRIPLE = "NBA triple Over (points + assists + rebounds)"
RULES = (RULE_STALE, RULE_NHL, RULE_NHL_POINTS, RULE_NBA, RULE_NBA_ALL, RULE_NCAAB)
TRIPLE_MARKETS = ("points", "assists", "rebounds")

NBA_CORE_LINE = 1.5
NBA_CORE_MAX_STALE = 2

NCAAB_GAP = (3, 5)                 # gap to Pinnacle of 3 or 4 points (5+ had only 8 plays, and lost)
NCAAB_PRICE = (-199, -160)         # -160 to -199; -200 or worse was 65% vs 68% break-even
NCAAB_BOOKS = ("betrivers",)       # 295 of the 298 gap 3+ plays were BetRivers

STALE_MIN = 3
NHL_STALE_MIN = 2
DEFAULT_SPORTS = ("NFL",)
DEFAULT_MARKETS = ("reception_yds",)
DEFAULT_SIDE = "Over"


def peak(rec):
    """Most soft books stale on this play, at the first sighting or the latest check (0 for a line alert record)."""
    return max(rec["first_seen"].get("stale") or 0, (rec.get("last_seen") or rec["first_seen"]).get("stale") or 0)


def alert_to_rec(rec):
    """
    A line alert record (best_plays_log/line_alert_results/<SPORT>/<date>.json, a team total) in the First Seen shape the
    page's cards use, with the gap to Pinnacle read from its 'why' text ("... Pinnacle dropped to 70.5"). `_alert` marks it.
    The books-stale count is unknown for line alerts, so it is None.
    """
    f = rec["first_seen"]
    m = re.search(r"([\d.]+)\s*$", f.get("why") or "")
    pin = float(m.group(1)) if m else None
    gap = round(abs(pin - f["line"]), 1) if pin is not None else 0.0
    stamp = {"t": f["t"], "line": f["line"], "price": f["price"], "book": f["book"], "pin_line": pin, "gap": gap,
             "stale": None, "n_books": None}
    return {"sport": rec["sport"], "kind": rec.get("kind", "team total"), "game": rec["game"], "who": rec["who"],
            "market": "team total", "bet": rec["bet"], "commence_time": rec["commence_time"],
            "first_seen": stamp, "last_seen": stamp, "result": rec.get("result"), "_alert": True}


def alert_matches(rec):
    """Does a line alert record (alert_to_rec shape) pass the NCAAB team-total rule?"""
    f = rec["first_seen"]
    return (rec["sport"] == "NCAAB" and f["pin_line"] is not None and NCAAB_GAP[0] <= f["gap"] < NCAAB_GAP[1]
            and NCAAB_PRICE[0] <= f["price"] <= NCAAB_PRICE[1] and f["book"] in NCAAB_BOOKS)


def nba_core(rec):
    """The core NBA threes filter: an Over on a 1.5 line with 1 to 2 soft books stale."""
    return (rec["sport"] == "NBA" and rec["market"] == "threes" and rec["bet"] == "OVER"
            and rec["first_seen"]["line"] == NBA_CORE_LINE and peak(rec) <= NBA_CORE_MAX_STALE)


def matches(rec, sports=DEFAULT_SPORTS, markets=DEFAULT_MARKETS, side=DEFAULT_SIDE):
    """The rules this record passes (a list of rule names, possibly empty)."""
    if rec.get("_alert"):                       # a line alert record: only the NCAAB team-total rule applies
        return [RULE_NCAAB] if alert_matches(rec) else []
    out = []
    if (peak(rec) >= STALE_MIN and rec["sport"] in sports and rec["market"] in markets
            and (side == "Both" or rec["bet"] == side.upper())):
        out.append(RULE_STALE)
    if rec["sport"] == "NHL" and rec["market"] == "shots_on_goal" and rec["bet"] == "OVER" and peak(rec) >= NHL_STALE_MIN:
        out.append(RULE_NHL)
    if rec["sport"] == "NHL" and rec["market"] == "points" and rec["bet"] == "OVER" and peak(rec) >= 1:
        out.append(RULE_NHL_POINTS)
    if rec["sport"] == "NBA" and rec["market"] == "threes" and rec["bet"] == "OVER":
        if nba_core(rec):
            out.append(RULE_NBA)
        out.append(RULE_NBA_ALL)
    return out


def record(recs):
    """{w, l, units, n} over graded (win/loss) records."""
    w = l = 0
    units = 0.0
    for r in recs:
        res = r.get("result") or {}
        if res.get("outcome") == "win":
            w += 1
            units += res.get("profit") or 0.0
        elif res.get("outcome") == "loss":
            l += 1
            units -= 1
    return {"w": w, "l": l, "n": w + l, "units": units}


def by_rule(recs, **opts):
    """{rule name: [records that pass it]} (a record can pass several rules)."""
    out = defaultdict(list)
    for r in recs:
        for name in matches(r, **opts):
            out[name].append(r)
    return out


def stale_breakdown(recs, side="Over"):
    """{(sport, market): [records]} for graded plays with 3+ books stale on the chosen side: shows where the
    '3+ books stale' rule has and hasn't worked, to help choose its sports and markets."""
    out = defaultdict(list)
    for r in recs:
        if peak(r) >= STALE_MIN and (side == "Both" or r["bet"] == side.upper()):
            out[(r["sport"], r["market"])].append(r)
    return out


# ── Which team a player is on (from the saved rosters, best_plays_log/rosters/<SPORT>.json) ─────────────────────

def player_team(rosters, game, who):
    """
    The team of player `who` in `game` ("Away @ Home"), or "" when it can't be told. `rosters` = {team name: {"names": [normalized
    names], ...}} for the sport. Only the game's two teams are checked, and the answer must be unambiguous (on exactly one of them).
    """
    from scripts import best_plays as bp
    teams = [t.strip() for t in (game or "").split("@")]
    nm = bp.norm_name(who)
    if len(teams) != 2 or not nm:
        return ""
    on = [t for t in teams if nm in {bp.norm_name(n) for n in ((rosters or {}).get(t) or {}).get("names", [])}]
    return on[0] if len(on) == 1 else ""


# ── Last-5-games form for NBA threes (from the saved NBA box scores, best_plays_log/nba_box/<date>.json) ──────────

def load_recent_box(load, date, days=14, log_dir="best_plays_log"):
    """
    {normalized name: [(day, threes made, threes attempted), ...] oldest first} from the box scores of the `days` days up to
    and including `date`. load(path) -> data or {}. Missing days are skipped.
    """
    from datetime import datetime, timedelta
    from scripts import best_plays as bp
    hist = defaultdict(list)
    end = datetime.strptime(date, "%Y-%m-%d")
    for i in range(days - 1, -1, -1):
        day = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            data = load(f"{log_dir}/nba_box/{day}.json") or {}
        except Exception:  # noqa: BLE001
            continue
        for rows in (data.get("games") or {}).values():
            for name, r in rows.items():
                hist[bp.norm_name(name)].append((day, r.get("fg3m", 0), r.get("fg3a", 0)))
    return hist


def form_last5(hist, who, before, n=5):
    """(avg threes made, avg attempted, games) over the player's last `n` games before the day `before`, or None (fewer than 3 games)."""
    from scripts import best_plays as bp
    g = [x for x in hist.get(bp.norm_name(who), []) if x[0] < before][-n:]
    if len(g) < 3:
        return None
    return sum(x[1] for x in g) / len(g), sum(x[2] for x in g) / len(g), len(g)


def form_note(hist, rec, before):
    """'Last 5: 2.4 threes made, 5.4 attempted per game' ('✓' when the makes are at least 0.5 above the line), or ''."""
    f = form_last5(hist, rec["who"], before)
    if not f:
        return ""
    ok = rec["bet"] == "OVER" and f[0] >= rec["first_seen"]["line"] + 0.5
    return f"Last {f[2]}: {f[0]:.1f} threes made, {f[1]:.1f} attempted per game" + (" ✓" if ok else "")


# ── Triple Over watch: one NBA player with an Over split on points, assists AND rebounds ─────────────────────────

def triples(log):
    """
    {(game, who): {"points": rec, "assists": rec, "rebounds": rec}} for the NBA players who have an Over play on all three
    markets in one day's First Seen log ({pick id: record}). Any number of books stale counts.
    """
    g = defaultdict(dict)
    for rec in log.values():
        if rec["sport"] == "NBA" and rec["bet"] == "OVER" and rec["market"] in TRIPLE_MARKETS:
            g[(rec["game"], rec["who"])][rec["market"]] = rec
    return {k: v for k, v in g.items() if all(m in v for m in TRIPLE_MARKETS)}


def triple_record(logs):
    """
    History of the triple Overs over every day's log ({day: {pick id: record}}): {"days": player-days with all three legs graded,
    "w", "l", "units": the three legs together, "all3": player-days where all three hit}.
    """
    out = {"days": 0, "w": 0, "l": 0, "units": 0.0, "all3": 0}
    for log in logs.values():
        for legs in triples(log).values():
            if not all((legs[m].get("result") or {}).get("outcome") in ("win", "loss") for m in TRIPLE_MARKETS):
                continue
            s = record(list(legs.values()))
            out["days"] += 1
            out["w"] += s["w"]
            out["l"] += s["l"]
            out["units"] += s["units"]
            out["all3"] += s["w"] == 3
    return out


# ── Triple Over watch from the LIVE Player Props snapshot (the newest NBA props snapshot) ──────────────────────

LIVE_MAX_AGE_MIN = 90            # a snapshot older than this is not used for the live watch (the First Seen log is used instead)


def play_to_rec(p, iso):
    """A play from first_seen.split_plays (a line split in one props snapshot) in the First Seen record shape, stamped `iso`."""
    stamp = {"t": iso, "line": p["line"], "price": p["price"], "book": p["book"], "pin_line": p["pin_line"], "gap": p["gap"],
             "stale": p["stale"], "n_books": p["n_books"]}
    return {"sport": p["sport"], "kind": p["kind"], "game": p["game"], "who": p["who"], "market": p["market"], "bet": p["bet"],
            "commence_time": p["commence_time"], "first_seen": stamp, "last_seen": stamp, "result": None}


def live_log(snap, snap_time):
    """{key: record} of every NBA line split in one props snapshot, for games that have not started (what Player Props shows)."""
    from scripts import first_seen as fs
    iso = snap_time.isoformat()
    return {str(i): play_to_rec(p, iso) for i, p in enumerate(fs.split_plays("NBA", snap, snap_time))}


def live_nba_log(load, list_names, date, now, log_dir="best_plays_log"):
    """
    live_log of the newest NBA props snapshot saved for `date` (best_plays_log/snapshots/<date>/NBA_<hhmm>_props.json), or None
    when there is none or it is older than LIVE_MAX_AGE_MIN minutes. load(path) -> data or {}; list_names(folder) -> [names].
    """
    import re
    from datetime import timedelta
    from scripts import first_seen as fs
    folder = f"{log_dir}/snapshots/{date}"
    names = sorted(n for n in list_names(folder) if re.fullmatch(r"NBA_\d{4}_props\.json", n))
    if not names:
        return None
    snap = load(f"{folder}/{names[-1]}") or {}
    try:
        t = fs._parse(snap["captured_at"])
    except Exception:  # noqa: BLE001
        return None
    if now - t > timedelta(minutes=LIVE_MAX_AGE_MIN):
        return None
    return live_log(snap, t)


# ── Forward scorecard: each frozen rule judged only on plays first seen from FORWARD_START on ─────────────────────

FORWARD_START = "2026-11-19"      # the day the rules were frozen; nothing earlier counts (earlier results were used to pick them)
FORWARD_RULES = (RULE_NHL, RULE_NBA, RULE_NCAAB)
FORWARD_MIN = 100                 # plays needed before a verdict is given

# Candidates, chosen after seeing results: tracked on the scorecard from their own start day (🧪), so the earlier results don't count.
# The two CFB ones are scorecard-only (candidate_matches, never on the cards); NHL points Over is also a card rule (matches).
RULE_CFB_PROP = "CFB props Under, 2 books stale"
RULE_CFB_ALERT = "CFB team total alert, -140 to -179"
CANDIDATE_RULES = (RULE_CFB_PROP, RULE_CFB_ALERT, RULE_NHL_POINTS)
CFB_ALERT_PRICE = (-179, -140)
FORWARD_STARTS = {RULE_CFB_PROP: "2026-11-21", RULE_CFB_ALERT: "2026-11-21",
                  RULE_NHL_POINTS: "2026-11-28"}      # every other rule starts at FORWARD_START


def forward_start(rule):
    return FORWARD_STARTS.get(rule, FORWARD_START)


def candidate_matches(rec):
    """The candidate rules (CANDIDATE_RULES) this record passes; works on First Seen records and alert_to_rec records."""
    out = []
    if rec.get("_alert"):
        if rec["sport"] == "CFB" and CFB_ALERT_PRICE[0] <= rec["first_seen"]["price"] <= CFB_ALERT_PRICE[1]:
            out.append(RULE_CFB_ALERT)
    elif rec["sport"] == "CFB" and rec["bet"] == "UNDER" and peak(rec) == 2:
        out.append(RULE_CFB_PROP)
    return out


def implied(price):
    """Break-even win probability of an American price."""
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def wilson(w, n, z=1.96):
    """95% confidence range (low, high) for a win rate of w out of n (Wilson score interval); (0, 1) when n is 0."""
    if n == 0:
        return 0.0, 1.0
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)


def forward_recs(recs, start=FORWARD_START):
    """Records first seen on or after `start` (an ET date, 'YYYY-MM-DD')."""
    return [r for r in recs if str(r["first_seen"]["t"])[:10] >= start]


def scorecard_row(recs, clv=None, minimum=FORWARD_MIN):
    """
    One rule's forward line from its records: n (graded), w, l, win rate and its 95% range, average break-even, units, ROI,
    open (not graded yet), CLV (clv = {pick id: {pin_move, gap_close}}, matched on r["_id"]: n_clv plays, the share that beat
    the close, the average gap_close) and a plain verdict: not enough plays / can't tell / ahead of / below break-even.
    """
    s = record(recs)
    graded = [r for r in recs if (r.get("result") or {}).get("outcome") in ("win", "loss")]
    n = s["n"]
    be = sum(implied(r["first_seen"]["price"]) for r in graded) / n if n else 0.0
    lo, hi = wilson(s["w"], n)
    if n < minimum:
        verdict = f"Not enough plays yet ({n} of {minimum})"
    elif lo > be:
        verdict = "Ahead of break-even (range is above it)"
    elif hi < be:
        verdict = "Below break-even (range is under it)"
    else:
        verdict = "Can't tell yet (range includes break-even)"
    c = [clv[r["_id"]] for r in recs if clv and r.get("_id") in clv and clv[r["_id"]].get("gap_close") is not None]
    return {"n": n, "w": s["w"], "l": s["l"], "units": s["units"], "roi": 100 * s["units"] / n if n else 0.0,
            "win": s["w"] / n if n else 0.0, "lo": lo, "hi": hi, "be": be, "verdict": verdict,
            "open": len(recs) - n, "n_clv": len(c),
            "beat_close": sum(1 for x in c if x["gap_close"] > 0) / len(c) if c else None,
            "avg_gap_close": sum(x["gap_close"] for x in c) / len(c) if c else None}
