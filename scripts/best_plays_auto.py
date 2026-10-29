"""
Headless version of the Best Plays 📸 button, for the scheduled GitHub Action.

Snapshots every sport with a game today, saves the snapshot files, logs the picks and game
predictions, and grades anything that finished. Same logic and same files as the page.
Needs THE_ODDS_API_KEY and GITHUB_TOKEN in the environment.
"""
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data import github_storage as gh  # noqa: E402
from scripts import best_plays as bp  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
OWNER, REPO = "Theo1984-ai", "MLB-DASHBOARD-"
LOG_DIR = "best_plays_log"
LOOSE_TH = {"min_edge": -1.0, "min_stale": 1, "min_gap": 0.5, "max_juice": 1000}
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


def main():
    if not (ODDS_KEY and TOKEN):
        sys.exit("THE_ODDS_API_KEY and GITHUB_TOKEN are required")
    # The page's toggle (best_plays_log/auto_config.json). A manual "Run workflow" ignores it.
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch" and \
            load(f"{LOG_DIR}/auto_config.json").get("enabled", True) is False:
        print("Auto snapshot is switched off on the Best Plays page. Nothing to do.")
        return
    now_et = datetime.now(tz=EASTERN)
    date, hhmm, iso = now_et.strftime("%Y-%m-%d"), now_et.strftime("%H%M"), now_et.isoformat()
    plays, pred_rows, snaps, errors = [], [], {}, []
    for sport in bp.SPORTS:
        try:
            res, quota = bp.snapshot_sport(ODDS_KEY, sport, now_et)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sport}: {e}")
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


if __name__ == "__main__":
    main()
