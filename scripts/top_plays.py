"""
Rules for the Top Plays page: the First Seen plays whose record has been best so far.

  RULE_STALE   any play with 3+ soft books stale (you choose the sports, markets and side on the page;
               the default is NFL receiving yards Over: 71-51, 58%, +8.5% ROI so far)
  RULE_NHL     NHL shots on goal Over with 2+ books stale (55-22, 71%, +13% ROI so far)
  RULE_NBA     NBA threes Over (107-45, 70%, +8% ROI so far, heavy juice)

A play's books stale = the most soft books that were off Pinnacle at its first sighting or its latest check.
Records are First Seen log records, so the same functions work on graded history and on today's open plays.
"""
from collections import defaultdict

RULE_STALE = "3+ books stale"
RULE_NHL = "NHL shots Over, 2+ stale"
RULE_NBA = "NBA threes Over"
RULES = (RULE_STALE, RULE_NHL, RULE_NBA)

STALE_MIN = 3
NHL_STALE_MIN = 2
DEFAULT_SPORTS = ("NFL",)
DEFAULT_MARKETS = ("reception_yds",)
DEFAULT_SIDE = "Over"


def peak(rec):
    """Most soft books stale on this play, at the first sighting or the latest check."""
    return max(rec["first_seen"]["stale"], (rec.get("last_seen") or rec["first_seen"])["stale"])


def matches(rec, sports=DEFAULT_SPORTS, markets=DEFAULT_MARKETS, side=DEFAULT_SIDE):
    """The rules this record passes (a list of rule names, possibly empty)."""
    out = []
    if (peak(rec) >= STALE_MIN and rec["sport"] in sports and rec["market"] in markets
            and (side == "Both" or rec["bet"] == side.upper())):
        out.append(RULE_STALE)
    if rec["sport"] == "NHL" and rec["market"] == "shots_on_goal" and rec["bet"] == "OVER" and peak(rec) >= NHL_STALE_MIN:
        out.append(RULE_NHL)
    if rec["sport"] == "NBA" and rec["market"] == "threes" and rec["bet"] == "OVER":
        out.append(RULE_NBA)
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
