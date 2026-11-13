"""
Telegram alerts for the two plays the First Seen record shows have done best:

  NHL shots on goal, Over, with 2+ soft books stale   46-17 (73%, +9.7u) so far
  NBA threes (player threes made), Over               97-39 (71%, +12.6u) so far; Unders lost (36-21, -2.0u) and
                                                      asking for more stale books did not help, so neither is required

Each hour, after the First Seen log is updated, every matching play whose game has not started is sent once.
It uses the same secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) and the same Automation-page switch as the
Best Plays alerts, and skips players listed Out or Doubtful. Sent plays are remembered in
best_plays_log/filter_alerts/<date>.json so nothing is sent twice.
"""
import os
from datetime import datetime, timedelta

from scripts import best_plays as bp
from scripts import first_seen as fs
from scripts import injuries as injm
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
    "daily_cap": 12,              # alerts per day
    "max_per_message": 6,
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


def pick_alerts(log, sent, now_et, inj=None, cfg=CFG, rules=RULES, sent_today=0):
    """New matching plays worth a message: [(pick id, play)], soonest game first. `log` is one day's First Seen log."""
    if now_et.hour >= cfg["quiet_start"] or now_et.hour < cfg["quiet_end"]:
        return []
    room = min(cfg["max_per_message"], cfg["daily_cap"] - sent_today)
    if room <= 0:
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
                "n_books": l["n_books"]}
        e = injm.play_injury(inj, play)
        if e and injm.severity(e["status"]) >= injm.STAYS_OFF_ALERTS:     # Out / Doubtful: skip
            continue
        out.append((pid, play))
        if len(out) >= room:
            break
    return out


def format_alert(picks, inj=None):
    lines = ["🎯 NHL shots / NBA threes play" + ("s" if len(picks) > 1 else "")]
    for _, p in picks:
        start = _parse(p["commence_time"]).astimezone(EASTERN).strftime("%I:%M %p").lstrip("0")
        who = p["who"]
        e = injm.play_injury(inj, p)
        if e:
            who += f" ⚠️ {e['status']}"
        lines.append(f"\n{ICON.get(p['sport'], '')} {who}: {LABEL.get(p['market'], p['market'])} {p['bet']} {p['line']} @ "
                     f"{bp.BOOK_DISPLAY.get(p['book'], p['book'])} {p['price']:+d}\n"
                     f"{p['stale']} of {p['n_books']} books stale · {p['game']} · {start} ET")
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
    picks = []
    for d in (date, (now_et + timedelta(days=1)).strftime("%Y-%m-%d")):
        log = load(f"{log_dir}/{fs.LOG_SUB}/{d}.json") or {}
        picks += pick_alerts(log, sent | {pid for pid, _ in picks}, now_et, inj,
                             sent_today=len(today_sent) + len(picks))
    picks = picks[:CFG["max_per_message"]]
    if not picks:
        return 0
    kw = {"urlopen": urlopen} if urlopen else {}
    if not send_telegram(token, chat, format_alert(picks, inj), **kw):
        return 0
    for pid, _ in picks:
        today_sent[pid] = iso
    save(path, today_sent, f"Telegram filter alerts {date}: {len(picks)} sent")
    return len(picks)
