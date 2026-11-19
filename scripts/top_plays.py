"""
Rules for the Top Plays page: the First Seen plays whose record has been best so far.

  RULE_STALE   any play with 3+ soft books stale (you choose the sports, markets and side on the page;
               the default is NFL receiving yards Over: 71-51, 58%, +8.5% ROI so far)
  RULE_NHL     NHL shots on goal Over with 2+ books stale (55-22, 71%, +13% ROI so far)
  RULE_NBA     NBA threes Over, the CORE filter: line 1.5 with 1 to 2 books stale (78 plays, 77% vs 65% break-even,
               +18% ROI so far; everything else in NBA threes Overs is 63%, -3%)
  RULE_NBA_ALL every NBA threes Over (181 plays, 69%, +6% ROI), still shown, with the core ones starred
  RULE_NCAAB   NCAAB team total line alert with a 3 or 4 point gap to Pinnacle, at -160 to -199, on BetRivers
               (about 67-73% vs 65% break-even, +3% to +9% ROI so far; thin, and heavy juice eats most of it)

A play's books stale = the most soft books that were off Pinnacle at its first sighting or its latest check.
Records are First Seen log records, so the same functions work on graded history and on today's open plays.
"""
import re
from collections import defaultdict

RULE_STALE = "3+ books stale"
RULE_NHL = "NHL shots Over, 2+ stale"
RULE_NBA = "NBA threes Over, line 1.5 + 1-2 stale"
RULE_NBA_ALL = "NBA threes Over (all)"
RULE_NCAAB = "NCAAB team total, gap 3-4"
RULES = (RULE_STALE, RULE_NHL, RULE_NBA, RULE_NBA_ALL, RULE_NCAAB)

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
