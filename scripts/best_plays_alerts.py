"""
Telegram alerts for new Best Plays.

After each auto snapshot, `pick_alerts` chooses the few new plays that clear a stricter bar than the
page's list (so your phone only buzzes for plays worth acting on) and `send_telegram` sends them
once. Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; without them nothing is sent.
"""
import json
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scripts import best_plays as bp

EASTERN = ZoneInfo("America/New_York")
APP_URL = "https://theo1984-mlb.streamlit.app"
ALERT = {
    "min_edge": 0.02,            # estimated edge (2%) or better
    "max_juice": 130,            # nothing worse than -130
    "min_minutes_to_start": 15,  # not a game that is about to start
    "max_age_hours": 3,          # only plays first seen recently (no backlog when switched on)
    "daily_cap": 8,              # alerts per day
    "max_per_message": 3,
    "quiet_start": 23,           # no alerts from 11 PM ...
    "quiet_end": 8,              # ... until 8 AM ET
    "min_yard_line": 10,         # skip football yardage lines under 10 (tiny numbers give fake edges)
}


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def pick_alerts(plays, log, date, now_et, cfg=ALERT):
    """New plays worth a notification: [(pick id, play)], best first. `log` is the day's pick log."""
    if now_et.hour >= cfg["quiet_start"] or now_et.hour < cfg["quiet_end"]:
        return []
    sent_today = sum(1 for r in log.values() if r.get("alerted"))
    room = min(cfg["max_per_message"], cfg["daily_cap"] - sent_today)
    if room <= 0:
        return []
    out = []
    for p in sorted(plays, key=lambda p: -p["adj"]):
        pid = bp.pick_id(date, p)
        rec = log.get(pid)
        if not rec or rec.get("alerted"):
            continue
        if p["edge"] < cfg["min_edge"] or p["price"] < -cfg["max_juice"]:
            continue
        if _parse(p["commence_time"]) - now_et < timedelta(minutes=cfg["min_minutes_to_start"]):
            continue
        if now_et - _parse(rec["first_seen"]["t"]) > timedelta(hours=cfg["max_age_hours"]):
            continue
        if p["sport"] in ("NFL", "CFB") and p["market"].endswith("_yds") and p["line"] < cfg["min_yard_line"]:
            continue
        out.append((pid, p))
        if len(out) >= room:
            break
    return out


def format_alert(picks):
    lines = ["🎯 New play" + ("s" if len(picks) > 1 else "")]
    for _, p in picks:
        start = _parse(p["commence_time"]).astimezone(EASTERN).strftime("%I:%M %p").lstrip("0")
        lines.append(f"\n{p['who']} {p['market']} {p['bet']} {p['line']} @ "
                     f"{bp.BOOK_DISPLAY.get(p['book'], p['book'])} {p['price']:+d}\n"
                     f"Edge ~{p['edge'] * 100:.1f}% · {p['game']} · {start} ET")
    lines.append(f"\nCheck the price and injuries first. {APP_URL}")
    return "\n".join(lines)


def send_telegram(token, chat_id, text, urlopen=urllib.request.urlopen):
    """Send one message. Returns True on success; never raises or prints the token."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception as e:  # noqa: BLE001
        print(f"note: Telegram send failed ({type(e).__name__})")
        return False
