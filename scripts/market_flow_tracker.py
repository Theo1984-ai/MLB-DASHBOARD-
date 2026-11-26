"""
Track record of the Market Flow "playable" sides.

Every playable side (scripts.market_flow.playable) is saved the first time a report shows it, with the odds, delta and money at
that moment, to best_plays_log/market_flow_plays/<date>.json (the date it was first seen, ET):

  {"<SPORT>|<game>|<kind>|<side>": {sport, game, kind, side, odds, delta, cash, orders, avg, top_wallet_pct, agree,
                                     first_seen, start, condition_id, slug, result}}

After the game it is graded from Polymarket's own resolution of that market (the winning outcome pays 1, the other 0), so the
result is win / loss / push with units at the saved odds (a win pays the odds, a loss is -1). The odds are Polymarket's price at
the time, a stand-in for what a sportsbook would have paid. A play can only be graded once its market has resolved.

    python -m scripts.market_flow_tracker     (record the new playable sides from the newest reports, then grade what is due)
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from scripts import market_flow as mf

ROOT = "best_plays_log"
PLAYS_SUB = "market_flow_plays"
REPORT_SUB = "market_flow"
GRADE_AFTER_H = 3          # grade a play only this many hours after its game starts (NBA/NHL games are shorter than football)
GIVE_UP_H = 72             # a play still unresolved this long after its game is marked "unresolved" and not retried


def play_key(sport, game, kind, side):
    return f"{sport}|{game}|{kind}|{side}"


def _aware(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def new_plays(report, existing):
    """
    The playable sides of one saved report ({captured_at, sport, games, meta}) whose game had not started when the report was
    taken and that are not in `existing` ({key: play}): {key: play}. A side already recorded without a condition id or slug
    (reports saved before those were kept) gets them filled in; the result is in `filled`.
    """
    out, filled = {}, {}
    cap = _aware(report.get("captured_at"))
    meta = report.get("meta") or {}
    for game, markets in (report.get("games") or {}).items():
        start = _aware((meta.get(game) or {}).get("start"))
        if start and cap and start <= cap:
            continue
        for p in mf.playable(markets):
            key = play_key(report["sport"], game, p["kind"], p["side"])
            m = next((x for x in markets if x["kind"] == p["kind"]), {})
            cid, slug = m.get("condition_id"), (meta.get(game) or {}).get("slug")
            if key in existing:
                old = existing[key]
                if (cid and not old.get("condition_id")) or (slug and not old.get("slug")):
                    filled[key] = {**old, "condition_id": old.get("condition_id") or cid, "slug": old.get("slug") or slug}
                continue
            out[key] = {"sport": report["sport"], "game": game, "kind": p["kind"], "side": p["side"], "odds": p["odds"],
                        "delta": p["delta"], "cash": p["cash"], "orders": p["orders"], "avg": p["avg"],
                        "top_wallet_pct": p["top_wallet_pct"], "agree": p["agree"], "first_seen": report.get("captured_at"),
                        "start": start.isoformat() if start else None, "condition_id": cid, "slug": slug, "result": None}
    return out, filled


def winner_of(market):
    """The winning outcome name of a resolved Gamma market, "push" for a 50/50 settlement, or None while it is unresolved."""
    outs = [str(o) for o in mf._json(market.get("outcomes"), [])]
    try:
        prices = [float(x) for x in mf._json(market.get("outcomePrices"), [])]
    except (TypeError, ValueError):
        return None
    if len(outs) != 2 or len(prices) != 2:
        return None
    if max(prices) >= 0.99:
        return outs[prices.index(max(prices))]
    if market.get("closed") and all(abs(p - 0.5) < 0.02 for p in prices):
        return "push"
    return None


def pay(odds):
    """Profit on a winning 1-unit stake at American odds."""
    return odds / 100 if odds > 0 else 100 / -odds


def grade_play(play, fetch_event):
    """
    {"outcome": win|loss|push, "profit": units, "graded_at": iso} from Polymarket's resolution of the play's market, or None when
    it can't be read yet. fetch_event(slug) -> the Gamma event dict (or None).
    """
    if not play.get("slug") or not play.get("condition_id"):
        return None
    ev = fetch_event(play["slug"])
    if not ev:
        return None
    m = next((x for x in ev.get("markets") or [] if x.get("conditionId") == play["condition_id"]), None)
    w = winner_of(m) if m else None
    if w is None:
        return None
    outcome = "push" if w == "push" else "win" if w == play["side"] else "loss"
    odds = play.get("odds")
    profit = 0.0 if outcome == "push" else (pay(odds) if odds else None) if outcome == "win" else -1.0
    return {"outcome": outcome, "profit": profit, "graded_at": datetime.now(timezone.utc).isoformat()}


def fetch_event(slug):
    """The Gamma event with this slug (open or closed), or None."""
    for q in (f"slug={slug}", f"slug={slug}&closed=true"):
        evs = mf._get(f"{mf.GAMMA}/events?{q}")
        if isinstance(evs, list) and evs:
            return evs[0]
    return None


def due(play, now):
    """Should this play be graded now? Its game must have started GRADE_AFTER_H hours ago (or, with no start time, it was first seen
    a day ago) and it must not already be settled or given up on."""
    r = play.get("result")
    if r and r.get("outcome") in ("win", "loss", "push", "unresolved"):
        return False
    start = _aware(play.get("start"))
    if start:
        return now >= start + timedelta(hours=GRADE_AFTER_H)
    fs = _aware(play.get("first_seen"))
    return bool(fs and now >= fs + timedelta(hours=24))


def grade_all(plays, now, fetch=fetch_event):
    """Grade every due play in {key: play} in place; returns how many were settled."""
    n = 0
    for p in plays.values():
        if not due(p, now):
            continue
        try:
            res = grade_play(p, fetch)
        except Exception as e:  # noqa: BLE001
            print(f"  could not grade {p['game']} / {p['kind']} / {p['side']}: {e}")
            continue
        if res:
            p["result"] = res
            n += 1
        else:
            start = _aware(p.get("start"))
            if start and now >= start + timedelta(hours=GIVE_UP_H):
                p["result"] = {"outcome": "unresolved", "profit": None, "graded_at": now.isoformat()}
    return n


def summarize(plays):
    """{"n", "w", "l", "push", "open", "units", "roi"} over {key: play} (units only from graded win/loss with known odds)."""
    w = l = push = open_ = 0
    units = 0.0
    for p in plays.values():
        r = p.get("result") or {}
        o = r.get("outcome")
        if o == "win":
            w += 1
            units += r.get("profit") or 0.0
        elif o == "loss":
            l += 1
            units -= 1
        elif o == "push":
            push += 1
        elif o != "unresolved":
            open_ += 1
    n = w + l
    return {"n": n, "w": w, "l": l, "push": push, "open": open_, "units": units, "roi": 100 * units / n if n else 0.0}


def load_day(day, root=ROOT):
    path = f"{root}/{PLAYS_SUB}/{day}.json"
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def newest_reports(days, root=ROOT):
    """The newest saved report of each sport over the given days (folder names): {sport: report}."""
    best = {}
    for day in days:
        for f in sorted(glob.glob(f"{root}/{REPORT_SUB}/{day}/*.json")):
            m = re.fullmatch(r"([A-Z0-9]+)_(\d{4})\.json", os.path.basename(f))
            if m:
                best[m.group(1)] = f           # later files (and later days) overwrite earlier ones
    out = {}
    for sport, f in best.items():
        with open(f) as fh:
            rep = json.load(fh)
        if rep.get("games"):
            out[sport] = rep
    return out


def main(root=ROOT, now=None, fetch=fetch_event):
    """Record new playable sides from the newest reports, fill in missing ids, grade what is due, save. Returns the counts."""
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(mf.EASTERN)
    today = et.strftime("%Y-%m-%d")
    days = [(et - timedelta(days=d)).strftime("%Y-%m-%d") for d in (3, 2, 1, 0)]
    stored = {d: load_day(d, root) for d in days}
    allp = {k: p for d in days for k, p in stored[d].items()}
    added = filled = 0
    for rep in newest_reports(days, root).values():
        new, fill = new_plays(rep, allp)
        for k, p in fill.items():
            for d in days:
                if k in stored[d]:
                    stored[d][k] = p
                    allp[k] = p
                    filled += 1
        for k, p in new.items():
            stored[today][k] = p
            allp[k] = p
            added += 1
    graded = 0
    for d in days:
        graded += grade_all(stored[d], now, fetch)
    for d in days:
        if stored[d]:
            os.makedirs(f"{root}/{PLAYS_SUB}", exist_ok=True)
            with open(f"{root}/{PLAYS_SUB}/{d}.json", "w") as f:
                json.dump(stored[d], f)
    print(f"playable sides: {added} new, {filled} ids filled in, {graded} graded; "
          + json.dumps(summarize(allp)))
    return {"added": added, "filled": filled, "graded": graded}


if __name__ == "__main__":
    main()
    sys.exit(0)
