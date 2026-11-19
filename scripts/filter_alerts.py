"""
Telegram alerts for the plays the First Seen record shows have done best:

  NHL shots on goal, Over, with 2+ soft books stale   55-22 (71%, +13% ROI) so far
  NBA threes (player threes made), Over               CORE = line 1.5 with 1-2 books stale: 77% vs 65% break-even, +18%
                                                      every other threes Over is still sent, in its own section
                                                      (all threes Overs: 69%, +6%; the non-core ones are 63%, -3%)

Each half hour, after the First Seen log is updated, every matching play whose game has not started is sent once. Core plays
(and NHL shots) come first under a star heading, then the other NBA threes Overs under their own heading. NBA threes also
show the player's last 5 games (threes made and attempted, from the saved box scores). It uses the same secrets
(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) and the same Automation-page switch as the Best Plays alerts, and skips players listed
Out or Doubtful. Players with an Over split on points, assists AND rebounds in the same game get a separate "Triple Over
watch" message, once a day each (a watch list, not a proven edge). There is no limit on how many plays are sent; a long list goes out as several messages. Sent plays are
remembered in best_plays_log/filter_alerts/<date>.json so nothing is sent twice.
"""
import os
from datetime import datetime, timedelta

from scripts import best_plays as bp
from scripts import first_seen as fs
from scripts import injuries as injm
from scripts import top_plays as tp
from scripts.best_plays_alerts import APP_URL, EASTERN, send_telegram

SENT_SUB = "filter_alerts"
# (sport, market, side, minimum books stale at the first sighting or the latest check)
RULES = (
    ("NHL", "shots_on_goal", "OVER", 2),
    ("NBA", "threes", "OVER", 1),
)
CFG = {
    "min_minutes_to_start": 15,   # not a game that is about to start
    "max_age_hours": 12,          # first seen within this long (no old backlog when switched on)
    "plays_per_message": 15,      # no limit on plays: a longer list is sent as several messages (Telegram cuts off text over 4096 characters)
    "quiet_start": 23,            # no alerts from 11 PM ...
    "quiet_end": 8,               # ... until 8 AM ET
}
LABEL = {"shots_on_goal": "shots on goal", "threes": "threes made"}
ICON = {"NHL": "🏒", "NBA": "🏀"}


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def peak(rec):
    """Most soft books stale on this play, at the first sighting or the latest check."""
    return max(rec["first_seen"]["stale"], rec["last_seen"]["stale"])


def matches(rec, rules=RULES):
    return any(rec["sport"] == s and rec["market"] == m and rec["bet"] == b and peak(rec) >= n
               for s, m, b, n in rules)


def is_core(rec):
    """NHL shots Overs (2+ stale) and NBA threes Overs on a 1.5 line with 1-2 books stale are the core plays; every other
    NBA threes Over is still sent, in its own section."""
    return rec["sport"] == "NHL" or tp.nba_core(rec)


def pick_alerts(log, sent, now_et, inj=None, cfg=CFG, rules=RULES, box=None):
    """
    New matching plays worth a message: [(pick id, play)], soonest game first. `log` is one day's First Seen log.
    Each play gets "core" (see is_core) and, for NBA threes with box-score history in `box`, a "form" note (last 5 games).
    """
    if now_et.hour >= cfg["quiet_start"] or now_et.hour < cfg["quiet_end"]:
        return []
    out = []
    for pid, rec in sorted(log.items(), key=lambda kv: kv[1]["commence_time"]):
        if pid in sent or not matches(rec, rules):
            continue
        tip = _parse(rec["commence_time"])
        if tip - now_et < timedelta(minutes=cfg["min_minutes_to_start"]):
            continue
        if now_et - _parse(rec["first_seen"]["t"]) > timedelta(hours=cfg["max_age_hours"]):
            continue
        l = rec["last_seen"]
        play = {**rec, "line": l["line"], "price": l["price"], "book": l["book"], "stale": peak(rec),
                "n_books": l["n_books"], "core": is_core(rec)}
        if box and rec["market"] == "threes":
            play["form"] = tp.form_note(box, rec, tip.astimezone(EASTERN).strftime("%Y-%m-%d"))
        e = injm.play_injury(inj, play)
        if e and injm.severity(e["status"]) >= injm.STAYS_OFF_ALERTS:     # Out / Doubtful: skip
            continue
        out.append((pid, play))
    return out


def pick_triples(log, sent, now_et, date, inj=None, cfg=CFG):
    """
    New triple Overs (an NBA player with an Over play on points, assists AND rebounds in the same game) whose game has not
    started: [(key, {who, game, commence_time, legs})], soonest first. The key is "triple|<date>|<game>|<who>", so each player
    is sent once a day. Same quiet hours and age limit as the other alerts; players listed Out or Doubtful are skipped.
    """
    if now_et.hour >= cfg["quiet_start"] or now_et.hour < cfg["quiet_end"]:
        return []
    out = []
    for (game, who), legs in sorted(tp.triples(log).items(), key=lambda kv: kv[1]["points"]["commence_time"]):
        key = f"triple|{date}|{game}|{who}"
        rec = legs["points"]
        if key in sent:
            continue
        tip = _parse(rec["commence_time"])
        if tip - now_et < timedelta(minutes=cfg["min_minutes_to_start"]):
            continue
        if now_et - min(_parse(legs[m]["first_seen"]["t"]) for m in tp.TRIPLE_MARKETS) > timedelta(hours=cfg["max_age_hours"]):
            continue
        e = injm.play_injury(inj, {**rec, "line": rec["last_seen"]["line"]})
        if e and injm.severity(e["status"]) >= injm.STAYS_OFF_ALERTS:
            continue
        out.append((key, {"who": who, "game": game, "commence_time": rec["commence_time"], "legs": legs,
                          "status": (e or {}).get("status")}))
    return out


def format_triples(triples):
    """One message for the triple Overs: the three lines per player, labelled as a watch list with no proven edge."""
    lines = ["👀 Triple Over watch: Over splits on points, assists AND rebounds (watch list, no proven edge: the three legs have "
             "hit about 53% so far)"]
    for _, t in triples:
        start = _parse(t["commence_time"]).astimezone(EASTERN).strftime("%I:%M %p").lstrip("0")
        who = t["who"] + (f" ⚠️ {t['status']}" if t.get("status") else "")
        lines.append(f"\n🏀 {who} · {t['game']} · {start} ET")
        for m, label in (("points", "PTS"), ("assists", "AST"), ("rebounds", "REB")):
            l = t["legs"][m].get("last_seen") or t["legs"][m]["first_seen"]
            lines.append(f"  {label} Over {l['line']} @ {bp.BOOK_DISPLAY.get(l['book'], l['book'])} {l['price']:+d} "
                         f"({peak(t['legs'][m])} of {l['n_books']} books stale)")
    lines.append(f"\nCheck the prices and lineups first. {APP_URL}")
    return "\n".join(lines)


def _line(p, inj):
    start = _parse(p["commence_time"]).astimezone(EASTERN).strftime("%I:%M %p").lstrip("0")
    who = p["who"]
    e = injm.play_injury(inj, p)
    if e:
        who += f" ⚠️ {e['status']}"
    text = (f"\n{ICON.get(p['sport'], '')} {who}: {LABEL.get(p['market'], p['market'])} {p['bet']} {p['line']} @ "
            f"{bp.BOOK_DISPLAY.get(p['book'], p['book'])} {p['price']:+d}\n"
            f"{p['stale']} of {p['n_books']} books stale · {p['game']} · {start} ET")
    if p.get("form"):
        text += f"\n{p['form']}"
    return text


def format_alert(picks, inj=None):
    """Core plays first under a star heading, then the other NBA threes Overs under their own heading."""
    core = [p for _, p in picks if p.get("core", True)]
    other = [p for _, p in picks if not p.get("core", True)]
    lines = []
    if core:
        lines.append("⭐ Top play" + ("s" if len(core) > 1 else "") + " (NHL shots Over, NBA threes line 1.5 with 1-2 books stale)")
        lines += [_line(p, inj) for p in core]
    if other:
        lines.append(("\n" if core else "") + "🏀 Other NBA threes Overs (not in the core filter)")
        lines += [_line(p, inj) for p in other]
    lines.append(f"\nCheck the price and lineups first. {APP_URL}")
    return "\n".join(lines)


def run(date, now_et, iso, load, save, inj=None, log_dir="best_plays_log", urlopen=None):
    """
    Send the new matching plays of today's and tomorrow's First Seen logs. Returns the number sent.
    Needs the two Telegram secrets (without them nothing happens) and respects the Automation-page switch.
    """
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return 0
    if load("automation_config.json").get("jobs", {}).get("telegram_alerts") is False:
        print("Telegram alerts are switched off on the Automation page.")
        return 0
    path = f"{log_dir}/{SENT_SUB}/{date}.json"
    today_sent = load(path) or {}
    yesterday = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
    sent = set(today_sent) | set(load(f"{log_dir}/{SENT_SUB}/{yesterday}.json") or {})
    logs = {d: load(f"{log_dir}/{fs.LOG_SUB}/{d}.json") or {}
            for d in (date, (now_et + timedelta(days=1)).strftime("%Y-%m-%d"))}
    box = None
    try:                                            # last-5 form for the threes plays (skipped if the box scores can't be read)
        if any(r["market"] == "threes" and r["bet"] == "OVER" and r["sport"] == "NBA" for lg in logs.values() for r in lg.values()):
            box = tp.load_recent_box(load, date, log_dir=log_dir)
    except Exception:  # noqa: BLE001
        box = None
    picks = []
    for d, log in logs.items():
        picks += pick_alerts(log, sent | {pid for pid, _ in picks}, now_et, inj, box=box)
    picks.sort(key=lambda x: (not x[1]["core"], x[1]["commence_time"]))   # core plays first, so the first message has them
    trips = []
    for d, log in logs.items():
        trips += pick_triples(log, sent | {k for k, _ in trips}, now_et, d, inj)
    if not picks and not trips:
        return 0
    kw = {"urlopen": urlopen} if urlopen else {}
    n, step = 0, CFG["plays_per_message"]
    for i in range(0, len(picks), step):
        chunk = picks[i:i + step]
        if not send_telegram(token, chat, format_alert(chunk, inj), **kw):
            break                                   # the rest is tried again on the next run
        for pid, _ in chunk:
            today_sent[pid] = iso
        n += len(chunk)
    for i in range(0, len(trips), 8):                 # triple Over watch list: its own message(s), 8 players each
        chunk = trips[i:i + 8]
        if not send_telegram(token, chat, format_triples(chunk), **kw):
            break
        for key, _ in chunk:
            today_sent[key] = iso
        n += len(chunk)
    if n:
        save(path, today_sent, f"Telegram filter alerts {date}: {n} sent")
    return n
