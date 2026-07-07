"""
Daily ALL-IN-ONE auto-save script.

Runs every daily task in one shot:
  1. Settle yesterday's True Probability snapshot (via MLB Stats API)
  2. Take today's True Probability snapshot (saves to true_prob_history/)
  3. Generate + save HR Tracker picks (STRICT filter, top 7) -> GitHub
  4. Generate + save H+R+R Tracker picks (STRICT filter, top 6) -> GitHub
  5. Commit + push true_prob_history/ to GitHub

REPLACES your daily manual clicks on the HR Tracker page Save button.

Usage:
    python daily_all.py            # Do everything; push HR + H+R+R via GH API,
                                   # commit + push true_prob_history/ via git.

    python daily_all.py --skip-hr  # Skip HR generation (just True Prob)
    python daily_all.py --skip-hrr # Skip H+R+R generation
    python daily_all.py --no-push  # Run locally only, don't push anything

Add to Windows Task Scheduler:
    Program:  python
    Arguments:  C:\\Users\\17146\\MLB homerun\\daily_all.py
    Start in:  C:\\Users\\17146\\MLB homerun
    Trigger:  Daily, 4:00 PM
"""
import os
import subprocess
import sys
import tomllib
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

OWNER = "Theo1984-ai"
REPO = "MLB-DASHBOARD-"
HR_DIR = "hr_tracker"
HRR_DIR = "hrr_tracker"


def load_secrets():
    """Load API keys from .streamlit/secrets.toml OR from environment."""
    odds_key = os.environ.get("THE_ODDS_API_KEY")
    gh_token = os.environ.get("GITHUB_TOKEN")
    secrets_path = os.path.join(ROOT, ".streamlit", "secrets.toml")
    if os.path.exists(secrets_path):
        with open(secrets_path, "rb") as f:
            cfg = tomllib.load(f)
        odds_key = odds_key or cfg.get("THE_ODDS_API_KEY")
        gh_token = gh_token or cfg.get("GITHUB_TOKEN")
    return odds_key, gh_token


def header(msg):
    bar = "=" * 60
    print(f"\n{bar}\n{msg}\n{bar}")


def step(name, fn, *args, **kwargs):
    """Run a step with timing + safe error handling."""
    print(f"\n[STEP] {name}...")
    t0 = datetime.now()
    try:
        result = fn(*args, **kwargs)
        dt = (datetime.now() - t0).total_seconds()
        print(f"[OK]   {name} done in {dt:.1f}s")
        return result
    except Exception as e:
        print(f"[FAIL] {name}: {e}")
        traceback.print_exc(limit=3)
        return None


def main():
    args = set(sys.argv[1:])
    do_push = "--no-push" not in args
    do_hr = "--skip-hr" not in args
    do_hrr = "--skip-hrr" not in args
    do_soft = "--skip-soft" not in args
    # --settle-only: only settle yesterday's picks; skip today's snapshots
    # entirely. Used by the early-morning cron so results are available
    # when you wake up, without burning API quota on premature scans.
    settle_only = "--settle-only" in args
    if settle_only:
        do_hr = do_hrr = do_soft = False
    # In CI (GitHub Actions), don't do git operations from inside the script.
    # The workflow YAML handles the commit + push so credentials are
    # configured correctly. Locally, we still git-push from here.
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true" or "--no-git" in args

    today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    yest_et = (datetime.now(tz=EASTERN) - timedelta(days=1)).strftime("%Y-%m-%d")

    header(f"Daily ALL Tracker run — {today_et}")
    print(f"Yesterday (to settle): {yest_et}")
    print(f"Mode:                  {'SETTLE-ONLY' if settle_only else 'FULL'}")
    print(f"Push to GitHub:        {do_push}")
    print(f"HR Tracker:            {'YES' if do_hr else 'SKIP'}")
    print(f"H+R+R Tracker:         {'YES' if do_hrr else 'SKIP'}")
    print(f"Soft Scanner:          {'YES' if do_soft else 'SKIP'}")

    odds_key, gh_token = load_secrets()
    if not odds_key:
        print("\nFATAL: THE_ODDS_API_KEY not set in env or .streamlit/secrets.toml")
        sys.exit(1)
    if do_push and not gh_token:
        print("\nWARN: GITHUB_TOKEN not found — HR/H+R+R won't push (True Prob still pushes via git).")

    os.environ["THE_ODDS_API_KEY"] = odds_key

    # ---------- Quota pre-check ----------
    # Cheap probe: 1 request, returns quota headers. If exhausted, force
    # settle-only mode so we still get yesterday's results without piling
    # failed-snapshot errors in the log.
    quota_ok = True
    try:
        import urllib.request, ssl as _ssl
        _ctx = _ssl._create_unverified_context()
        _r = urllib.request.urlopen(
            f"https://api.the-odds-api.com/v4/sports?apiKey={odds_key}",
            timeout=10, context=_ctx)
        used = _r.headers.get("x-requests-used", "?")
        remaining = _r.headers.get("x-requests-remaining", "?")
        print(f"\nOdds API quota: {used} used / {remaining} remaining")
        try:
            if int(remaining) < 100:  # tight margin — daily_all needs ~30-50
                quota_ok = False
                print("[QUOTA LOW] Forcing --settle-only mode.")
        except Exception:
            pass
    except Exception as e:
        # 401 = exhausted or invalid key
        quota_ok = False
        print(f"\n[QUOTA EXHAUSTED] {e} — running settle-only.")
    if not quota_ok and not settle_only:
        settle_only = True
        do_hr = do_hrr = do_soft = False

    # ---------- Step 1: Settle recent snapshots ----------
    # Re-settle the last 3 days, not just yesterday. Some picks (especially
    # Sharp Money) are forward-looking — taken on day N but for games on
    # day N+1 or N+2. Skip files that are already fully graded so it's cheap.
    SETTLE_WINDOW_DAYS = 3
    settle_dates = [
        (datetime.now(tz=EASTERN) - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(1, SETTLE_WINDOW_DAYS + 1)
    ]

    def _settle(history_dir, target_date):
        def fn():
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "true_prob_settler.py"),
                 target_date, history_dir],
                cwd=ROOT, capture_output=True, text=True,
            )
            print(r.stdout, end="")
            if r.returncode != 0:
                print("STDERR:", r.stderr[:500])
                raise RuntimeError(f"settler exit {r.returncode}")
            return True
        return fn

    import json as _json_check
    for d in settle_dates:
        for tracker in ("true_prob_history", "soft_scanner_history",
                        "hr_tracker", "hrr_tracker", "sharp_money_history",
                        "fundamentals_history"):
            path = os.path.join(ROOT, tracker, f"{d}.json")
            if not os.path.exists(path):
                continue
            # Skip if file is fully settled (every pick has a final result)
            try:
                with open(path, encoding="utf-8") as f:
                    snap = _json_check.load(f)
                picks = snap.get("picks", [])
                if picks and all(p.get("result") in ("WIN","LOSS","PUSH","VOID")
                                 for p in picks):
                    continue
            except Exception:
                pass
            step(f"Settle {tracker} {d}", _settle(tracker, d))

    # ---------- Step 2: Take today's True Prob snapshot ----------
    # Idempotent guard: if today's snapshot already exists and was taken
    # within FRESHNESS_HOURS, skip the re-scan to save API quota. Forced
    # re-run with --force.
    FRESHNESS_HOURS = 4
    force = "--force" in args

    def _is_fresh(filepath):
        """Fresh = file exists AND is < FRESHNESS_HOURS old AND has picks.
        An empty file (0 picks) is NOT considered fresh — the next cron
        run should re-scan. Prevents an early-AM run with 0 picks from
        blocking the noon re-scan that would actually find plays."""
        if force or not os.path.exists(filepath):
            return False
        try:
            with open(filepath) as f:
                payload = __import__("json").load(f)
            # Empty payload = re-scan
            n_picks = (payload.get("n_picks")
                       or len(payload.get("picks", []))
                       or 0)
            if n_picks == 0:
                return False
            ts_str = payload.get("snapshot_at") or payload.get("saved_at")
            if not ts_str:
                return False
            ts = datetime.fromisoformat(ts_str)
            age_hours = (datetime.now(tz=EASTERN) - ts).total_seconds() / 3600
            return age_hours < FRESHNESS_HOURS
        except Exception:
            return False

    # True Prob now merges (not overwrites), so freshness guard tightened
    # from 4h to 30 min. Multiple runs per day now capture more signals.
    TP_FRESHNESS_MIN = 30
    def _tp_is_fresh(filepath):
        if force or not os.path.exists(filepath): return False
        try:
            with open(filepath) as f:
                p = __import__("json").load(f)
            n_picks = p.get("n_picks") or len(p.get("picks", [])) or 0
            if n_picks == 0: return False
            ts_str = p.get("snapshot_at") or p.get("saved_at")
            if not ts_str: return False
            ts = datetime.fromisoformat(ts_str)
            age_min = (datetime.now(tz=EASTERN) - ts).total_seconds() / 60
            return age_min < TP_FRESHNESS_MIN
        except Exception:
            return False

    tp_today = os.path.join(ROOT, "true_prob_history", f"{today_et}.json")
    if settle_only:
        print(f"\n[SKIP] --settle-only: not taking True Prob snapshot for {today_et}.")
    elif _tp_is_fresh(tp_today):
        print(f"\n[SKIP] True Prob snapshot for {today_et} is < {TP_FRESHNESS_MIN}min old. "
              f"Use --force to re-run.")
    else:
        def _snapshot():
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "true_prob_snapshot.py")],
                cwd=ROOT, capture_output=True, text=True,
            )
            print(r.stdout, end="")
            if r.returncode != 0:
                print("STDERR:", r.stderr[:500])
                raise RuntimeError(f"snapshot exit {r.returncode}")
            return True
        step(f"Snapshot True Prob {today_et}", _snapshot)

    # ---------- Step 2b: Take today's Soft Scanner snapshot ----------
    # Soft Scanner now uses APPEND+PERSISTENCE (7/7): each cron run merges
    # new signals into the file with dedup by (game, market, player, side,
    # point). Prior behavior overwrote the file, losing morning-only
    # signals when the afternoon slate had different qualifiers.
    #
    # Freshness guard is now MUCH shorter (30 min) because we're merging,
    # not overwriting — no risk of double-counting, only benefit is
    # capturing more signals as sportsbook lines move through the day.
    soft_today = os.path.join(ROOT, "soft_scanner_history", f"{today_et}.json")
    SOFT_FRESHNESS_MIN = 30
    def _soft_is_fresh():
        if not os.path.exists(soft_today) or force:
            return False
        try:
            with open(soft_today) as f:
                payload = __import__("json").load(f)
            ts_str = payload.get("snapshot_at") or payload.get("saved_at")
            if not ts_str: return False
            ts = datetime.fromisoformat(ts_str)
            age_min = (datetime.now(tz=EASTERN) - ts).total_seconds() / 60
            return age_min < SOFT_FRESHNESS_MIN
        except Exception:
            return False
    if do_soft and _soft_is_fresh():
        print(f"[SKIP] Soft Scanner snapshot for {today_et} is < {SOFT_FRESHNESS_MIN}min old.")
    elif do_soft:
        def _soft_snap():
            from scripts.soft_scanner import scan as soft_scan
            from scripts.snapshot_merger import (
                race_safe_git_pull, load_existing, merge_picks, summary as _sm,
            )
            race_safe_git_pull(ROOT)
            new_picks = soft_scan(odds_key)
            existing = load_existing(soft_today)
            def _key(p):
                return (str(p.get("game","") or p.get("event","")),
                        str(p.get("market","")),
                        str(p.get("player","") or p.get("selection","")),
                        str(p.get("side","")),
                        p.get("point"))
            merged, added, rehit = merge_picks(
                existing, new_picks, _key,
                rolling_fields=("best_price","best_book","best_imp_pct",
                                "consensus_pct","edge_pp","ev_per_100",
                                "n_books","all_prices"),
                history_fields={"edge": "edge_pp", "price": "best_price",
                                "n_books": "n_books"},
            )
            print(f"  Soft-scanner: {added} new + {rehit} re-appearances "
                  f"(had {len(existing)}, total {len(merged)})")
            payload = {
                "date":        today_et,
                "snapshot_at": datetime.now(tz=EASTERN).isoformat(),
                "n_picks":     len(merged),
                "filter": {
                    "min_edge_pp": 5.0,
                    "min_books":   3,
                    "min_price":   -300,
                    "max_price":   300,
                    "top_n":       30,
                },
                "summary":     _sm(merged),
                "picks":       merged,
            }
            out_dir = os.path.join(ROOT, "soft_scanner_history")
            os.makedirs(out_dir, exist_ok=True)
            import json
            with open(soft_today, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            print(f"  Saved -> {soft_today}")
            return True
        step(f"Snapshot Soft Scanner {today_et}", _soft_snap)

    # ---------- Step 2c: Sharp Money snapshot — ALWAYS RUN (append + dedup) ----------
    # Sharp money signals are fleeting — different scans through the day
    # catch different plays. Skip only in settle-only mode.
    if settle_only:
        print(f"[SKIP] --settle-only: not taking Sharp Money snapshot for {today_et}.")
    else:
        def _sharp_snap():
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "sharp_money_snapshot.py")],
                cwd=ROOT, capture_output=True, text=True,
            )
            print(r.stdout, end="")
            if r.returncode != 0:
                print("STDERR:", r.stderr[:500])
                raise RuntimeError(f"sharp money snap exit {r.returncode}")
            return True
        step(f"Snapshot Sharp Money {today_et}", _sharp_snap)

    # ---------- Step 2d: Fundamentals Props snapshot ----------
    # Fundamentals also merges now (7/7). Same 30-min freshness as
    # Soft/True Prob so intraday scans are captured.
    fund_today = os.path.join(ROOT, "fundamentals_history", f"{today_et}.json")
    def _fund_is_fresh():
        if force or not os.path.exists(fund_today): return False
        try:
            with open(fund_today) as f:
                p = __import__("json").load(f)
            n_picks = p.get("n_picks") or len(p.get("picks", [])) or 0
            if n_picks == 0: return False
            ts_str = p.get("snapshot_at") or p.get("saved_at")
            if not ts_str: return False
            ts = datetime.fromisoformat(ts_str)
            age_min = (datetime.now(tz=EASTERN) - ts).total_seconds() / 60
            return age_min < 30
        except Exception:
            return False
    if settle_only:
        print(f"[SKIP] --settle-only: not taking Fundamentals snapshot for {today_et}.")
    elif _fund_is_fresh():
        print(f"[SKIP] Fundamentals snapshot for {today_et} is < 30min old.")
    else:
        def _fund_snap():
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "fundamentals_snapshot.py")],
                cwd=ROOT, capture_output=True, text=True,
            )
            print(r.stdout, end="")
            if r.returncode != 0:
                print("STDERR:", r.stderr[:500])
                raise RuntimeError(f"fundamentals snap exit {r.returncode}")
            return True
        step(f"Snapshot Fundamentals {today_et}", _fund_snap)

    # ---------- Step 3: HR Tracker ----------
    hr_today = os.path.join(ROOT, HR_DIR, f"{today_et}.json")
    if do_hr and _is_fresh(hr_today):
        print(f"[SKIP] HR Tracker snapshot for {today_et} is < {FRESHNESS_HOURS}h old.")
    elif do_hr:
        def _hr():
            from scripts.hr_tracker_scanner import generate_hr_picks
            payload = generate_hr_picks(odds_key)
            print(f"  Generated {len(payload['picks'])} HR picks from {payload['n_total']} predictions")
            if do_push and gh_token:
                from data import github_storage as gh
                path = f"{HR_DIR}/{today_et}.json"
                gh.save_json(gh_token, OWNER, REPO, path, payload,
                             commit_msg=f"HR tracker: top {payload['top_n']} for {today_et}")
                print(f"  Pushed to GitHub: {path}")
            else:
                # Write locally so the user can manually push
                import json
                os.makedirs(os.path.join(ROOT, HR_DIR), exist_ok=True)
                local_path = os.path.join(ROOT, HR_DIR, f"{today_et}.json")
                with open(local_path, "w") as f:
                    json.dump(payload, f, indent=2, default=str)
                print(f"  Saved locally (no push): {local_path}")
            return payload
        step("HR Tracker (generate + push)", _hr)

    # ---------- Step 4: H+R+R Tracker ----------
    hrr_today = os.path.join(ROOT, HRR_DIR, f"{today_et}.json")
    if do_hrr and _is_fresh(hrr_today):
        print(f"[SKIP] H+R+R Tracker snapshot for {today_et} is < {FRESHNESS_HOURS}h old.")
    elif do_hrr:
        def _hrr():
            from scripts.hrr_tracker_scanner import generate_hrr_picks
            payload = generate_hrr_picks(odds_key)
            print(f"  Generated {len(payload['picks'])} H+R+R picks from {payload['n_total']} offers")
            if do_push and gh_token:
                from data import github_storage as gh
                path = f"{HRR_DIR}/{today_et}.json"
                gh.save_json(gh_token, OWNER, REPO, path, payload,
                             commit_msg=f"H+R+R tracker: top {payload['top_n']} (O{payload['point']}) for {today_et}")
                print(f"  Pushed to GitHub: {path}")
            else:
                import json
                os.makedirs(os.path.join(ROOT, HRR_DIR), exist_ok=True)
                local_path = os.path.join(ROOT, HRR_DIR, f"{today_et}.json")
                with open(local_path, "w") as f:
                    json.dump(payload, f, indent=2, default=str)
                print(f"  Saved locally (no push): {local_path}")
            return payload
        step("H+R+R Tracker (generate + push)", _hrr)

    # ---------- Step 5: Commit + push forward-test history files ----------
    if do_push and not in_ci:
        # Pass git identity inline so Task Scheduler runs don't need
        # any global git config on the box.
        GIT_ID = ["-c", "user.email=theo1984@users.noreply.github.com",
                  "-c", "user.name=Theo1984-ai"]

        def _git_push():
            subprocess.run(["git"] + GIT_ID + ["add",
                            "true_prob_history/", "soft_scanner_history/",
                            "hr_tracker/", "hrr_tracker/",
                            "sharp_money_history/", "fundamentals_history/"],
                           cwd=ROOT, check=False)
            r = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=ROOT)
            if r.returncode == 0:
                print("  No history changes to commit.")
                return True
            subprocess.run(
                ["git"] + GIT_ID + ["commit", "-m",
                 f"Daily forward-test: settle {yest_et} + snapshot {today_et}"],
                cwd=ROOT, check=False,
            )
            # Pull rebase with retry. Use -X theirs to auto-resolve merge
            # conflicts by taking the REMOTE version — important because the
            # cron often races itself across machines/runs. Taking remote
            # means we get the newer settlement results without leaving
            # conflict markers in JSON files (which break the page parsers).
            for attempt in range(3):
                r1 = subprocess.run(
                    ["git"] + GIT_ID + ["pull", "--rebase", "-X", "theirs", "origin", "main"],
                    cwd=ROOT, capture_output=True, text=True)
                if r1.returncode == 0:
                    break
                err = (r1.stderr or "").lower()
                if "unstaged" in err or "uncommitted" in err or "your index contains" in err:
                    subprocess.run(["git", "stash", "--include-untracked"], cwd=ROOT, check=False)
                    continue
                # If a rebase is in progress with conflicts, abort cleanly
                if "conflict" in err or "could not apply" in err:
                    subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, check=False)
                    print(f"  rebase conflict — aborted, continuing without remote merge")
                    break
                print(f"  pull warning: {r1.stderr[:200]}")
                break
            r2 = subprocess.run(["git"] + GIT_ID + ["push", "origin", "main"], cwd=ROOT)
            if r2.returncode != 0:
                raise RuntimeError("git push failed")
            return True
        step("Commit + push history files", _git_push)
    elif do_push and in_ci:
        print("\n[CI] Skipping git push from script — workflow YAML handles it.")

    header("ALL DONE")
    print(f"Run finished at {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}")


if __name__ == "__main__":
    main()
