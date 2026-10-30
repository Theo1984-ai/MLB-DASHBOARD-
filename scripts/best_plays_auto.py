"""
Headless version of the Best Plays 📸 button, for the scheduled GitHub Action.

Snapshots every sport with a game today, saves the snapshot files, logs the picks and game
predictions, and grades anything that finished. Same logic and same files as the page.
Needs THE_ODDS_API_KEY and GITHUB_TOKEN in the environment.
"""
import importlib.util
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from scripts import best_plays as bp  # noqa: E402
from scripts import best_plays_alerts as alerts  # noqa: E402

# Load github_storage straight from its file: importing the `data` package would pull in
# pandas and the rest of the app, which the Actions runner doesn't install.
_spec = importlib.util.spec_from_file_location("github_storage", os.path.join(ROOT, "data", "github_storage.py"))
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

EASTERN = ZoneInfo("America/New_York")
OWNER, REPO = "Theo1984-ai", "MLB-DASHBOARD-"
LOG_DIR = "best_plays_log"
LOOSE_TH = {"min_edge": -1.0, "min_stale": 1, "min_gap": 0.5, "max_juice": 1000}
MIN_GAP_MINUTES = 45   # the workflow wakes every 10 minutes; a run is skipped if one finished this recently
ODDS_KEY = os.environ.get("THE_ODDS_API_KEY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")


def load(path):
    return gh.load_json(TOKEN, OWNER, REPO, path) or {}


def first_totals(date, sport):
    """Earliest non-empty saved totals snapshot of the day for a sport."""
    folder = f"{LOG_DIR}/snapshots/{date}"
    names = sorted(i["name"] for i in gh.list_dir(TOKEN, OWNER, REPO, folder)
                   if re.fullmatch(rf"{sport}_\d{{4}}_totals\.json", i["name"]))
    for name in names:
        snap = gh.load_json(TOKEN, OWNER, REPO, f"{folder}/{name}")
        if (snap or {}).get("games"):
            return snap
    return None


def grade_recent(now_et, days=7):
    for sub, grader, label in (("picks", bp.grade_log, "picks"),
                               ("claude_picks", bp.grade_log, "Claude picks"),
                               ("predictions", bp.grade_predictions, "predictions")):
        have = {i["name"] for i in gh.list_dir(TOKEN, OWNER, REPO, f"{LOG_DIR}/{sub}")}
        for i in range(days):
            d = (now_et - timedelta(days=i)).strftime("%Y-%m-%d")
            if f"{d}.json" not in have:
                continue
            path = f"{LOG_DIR}/{sub}/{d}.json"
            log = load(path)
            c = grader(log, now_et)
            for e in c["errors"][:2]:
                print(f"grading {label} {d}: {e}")
            if c["graded"] or c.get("no_data"):
                gh.save_json(TOKEN, OWNER, REPO, path, log, commit_msg=f"{label} {d}: graded {c['graded']}")
                print(f"graded {c['graded']} {label} for {d}")


def minutes_since_last_run(now_et):
    """Minutes since the last successful scheduled snapshot (heartbeat file), or None if there is none."""
    t = load(f"{LOG_DIR}/last_run.json").get("t")
    try:
        return (now_et - datetime.fromisoformat(t)).total_seconds() / 60
    except Exception:  # noqa: BLE001
        return None


def send_alerts(plays, log, date, now_et, iso):
    """Telegram the best new plays once (needs the two secrets; the Automation page can switch it off)."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    if load("automation_config.json").get("jobs", {}).get("telegram_alerts") is False:
        print("Telegram alerts are switched off on the Automation page.")
        return
    picks = alerts.pick_alerts(plays, log, date, now_et)
    if picks and alerts.send_telegram(token, chat, alerts.format_alert(picks)):
        for pid, _ in picks:
            log[pid]["alerted"] = iso
        print(f"Telegram: sent {len(picks)} alert(s)")


def telegram_test():
    """One test message, no Odds API calls. Started from the workflow's 'send_test_message' input."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not both set as repo secrets.")
    if not alerts.send_telegram(token, chat, "✅ Test message from your Best Plays auto snapshot. "
                                "Telegram alerts are connected."):
        sys.exit("Telegram did not accept the message. Check the token and chat ID secrets, "
                 "and that you have sent your bot a message first.")
    print("Telegram test message sent.")


def main():
    if os.environ.get("TELEGRAM_TEST") == "true":
        telegram_test()
        return
    if not (ODDS_KEY and TOKEN):
        sys.exit("THE_ODDS_API_KEY and GITHUB_TOKEN are required")
    # The page's toggle (best_plays_log/auto_config.json). A manual "Run workflow" ignores it.
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch" and \
            load(f"{LOG_DIR}/auto_config.json").get("enabled", True) is False:
        print("Auto snapshot is switched off on the Best Plays page. Nothing to do.")
        return
    now_et = datetime.now(tz=EASTERN)
    date, hhmm, iso = now_et.strftime("%Y-%m-%d"), now_et.strftime("%H%M"), now_et.isoformat()
    # GitHub starts scheduled runs late or skips some, so the workflow is scheduled every 10 minutes and
    # each wake-up only does the work when the last good run is old enough. That keeps it about hourly
    # but lets the next wake-up catch up after a skipped slot. A manual "Run workflow" always runs.
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        ago = minutes_since_last_run(now_et)
        if ago is not None and ago < MIN_GAP_MINUTES:
            print(f"Last snapshot finished {ago:.0f} minutes ago (under {MIN_GAP_MINUTES}); skipping this wake-up.")
            return
    plays, pred_rows, snaps, errors, failed = [], [], {}, [], []
    for sport in bp.SPORTS:
        try:
            res, quota = bp.snapshot_sport(ODDS_KEY, sport, now_et)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sport}: {e}")
            failed.append(sport)
            continue
        print(f"{sport}: {'no games' if not res else str(res['n_games']) + ' games'} (quota left {quota})")
        if not res:
            continue
        snaps[sport] = res
        errors += res["errors"]
        roster_cache = {}
        try:
            teams = {t for g in res["tt_snap"]["games"] + res["props_snap"]["games"] for t in (g["away"], g["home"])}
            path = f"{LOG_DIR}/rosters/{sport}.json"
            roster_cache = load(path)
            roster_cache, _errs, changed = bp.espn_rosters(sport, teams, roster_cache)
            if changed:
                gh.save_json(TOKEN, OWNER, REPO, path, roster_cache, commit_msg=f"Roster cache {sport}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sport} rosters: {e}")
        try:
            first_tt = first_totals(date, sport)
        except Exception:  # noqa: BLE001
            first_tt = None
        first_tt = first_tt or res["tt_snap"]
        roster = bp.roster_lookup(roster_cache)
        plays += bp.annotate(bp.find_plays(sport, res["props_snap"], res["tt_snap"], bp.DEFAULT_TH), sport,
                             res["tt_snap"], first_tt, res["game_snap"], roster)
        loose = bp.annotate(bp.find_plays(sport, res["props_snap"], res["tt_snap"], LOOSE_TH), sport,
                            res["tt_snap"], first_tt, res["game_snap"], roster)
        pred_rows += bp.predict_rows(sport, bp.build_game_rows(
            sport, res["tt_snap"], first_tt, res["game_snap"], res["props_snap"], loose, now_et))
    plays.sort(key=lambda p: -p["adj"])

    if snaps:
        for sport, res in snaps.items():
            for kind, snap in (("props", res["props_snap"]), ("totals", res["tt_snap"]), ("game", res["game_snap"])):
                gh.save_json(TOKEN, OWNER, REPO, f"{LOG_DIR}/snapshots/{date}/{sport}_{hhmm}_{kind}.json", snap,
                             commit_msg=f"Best plays snapshot {sport} {kind} {date} {hhmm}")
        log = load(f"{LOG_DIR}/picks/{date}.json")
        new = bp.update_log(log, plays, iso, date)
        checked = sum(bp.add_checks(log, r["props_snap"], r["tt_snap"], iso) for r in snaps.values())
        send_alerts(plays, log, date, now_et, iso)
        gh.save_json(TOKEN, OWNER, REPO, f"{LOG_DIR}/picks/{date}.json", log,
                     commit_msg=f"Best plays log {date}: {new} new picks, {checked} checks")
        plog = load(f"{LOG_DIR}/predictions/{date}.json")
        n_pred = bp.update_predictions(plog, pred_rows, iso, date)
        gh.save_json(TOKEN, OWNER, REPO, f"{LOG_DIR}/predictions/{date}.json", plog,
                     commit_msg=f"Game predictions {date}: {n_pred} new, {len(pred_rows)} updated")
        print(f"saved: {new} new picks, {checked} checks, {n_pred} new predictions")
    try:
        grade_recent(now_et)
    except Exception as e:  # noqa: BLE001
        errors.append(f"grading: {e}")
    for e in errors:
        print("note:", e)
    if not failed:
        gh.save_json(TOKEN, OWNER, REPO, f"{LOG_DIR}/last_run.json", {"t": iso},
                     commit_msg=f"Auto snapshot heartbeat {hhmm}")
    if failed:
        # Show the run red in GitHub (e.g. a bad Odds API key gives 401 for every sport), after saving what we could.
        sys.exit(f"Snapshot failed for: {', '.join(failed)}")


if __name__ == "__main__":
    main()
