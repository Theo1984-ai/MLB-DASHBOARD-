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

    # ---------- Step 1: Settle yesterday's snapshots ----------
    def _settle(history_dir):
        def fn():
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "scripts", "true_prob_settler.py"),
                 yest_et, history_dir],
                cwd=ROOT, capture_output=True, text=True,
            )
            print(r.stdout, end="")
            if r.returncode != 0:
                print("STDERR:", r.stderr[:500])
                raise RuntimeError(f"settler exit {r.returncode}")
            return True
        return fn

    yest_tp = os.path.join(ROOT, "true_prob_history", f"{yest_et}.json")
    if os.path.exists(yest_tp):
        step(f"Settle True Prob {yest_et}", _settle("true_prob_history"))
    else:
        print(f"\n[SKIP] No True Prob snapshot for {yest_et} — nothing to settle.")

    yest_soft = os.path.join(ROOT, "soft_scanner_history", f"{yest_et}.json")
    if os.path.exists(yest_soft):
        step(f"Settle Soft Scanner {yest_et}", _settle("soft_scanner_history"))
    else:
        print(f"[SKIP] No Soft Scanner snapshot for {yest_et} — nothing to settle.")

    yest_hr = os.path.join(ROOT, "hr_tracker", f"{yest_et}.json")
    if os.path.exists(yest_hr):
        step(f"Settle HR Tracker {yest_et}", _settle("hr_tracker"))
    else:
        print(f"[SKIP] No HR Tracker snapshot for {yest_et} — nothing to settle.")

    yest_hrr = os.path.join(ROOT, "hrr_tracker", f"{yest_et}.json")
    if os.path.exists(yest_hrr):
        step(f"Settle H+R+R Tracker {yest_et}", _settle("hrr_tracker"))
    else:
        print(f"[SKIP] No H+R+R Tracker snapshot for {yest_et} — nothing to settle.")

    # ---------- Step 2: Take today's True Prob snapshot ----------
    # Idempotent guard: if today's snapshot already exists and was taken
    # within FRESHNESS_HOURS, skip the re-scan to save API quota. Forced
    # re-run with --force.
    FRESHNESS_HOURS = 4
    force = "--force" in args

    def _is_fresh(filepath):
        if force or not os.path.exists(filepath):
            return False
        try:
            with open(filepath) as f:
                payload = __import__("json").load(f)
            ts_str = payload.get("snapshot_at") or payload.get("saved_at")
            if not ts_str:
                return False
            ts = datetime.fromisoformat(ts_str)
            age_hours = (datetime.now(tz=EASTERN) - ts).total_seconds() / 3600
            return age_hours < FRESHNESS_HOURS
        except Exception:
            return False

    tp_today = os.path.join(ROOT, "true_prob_history", f"{today_et}.json")
    if settle_only:
        print(f"\n[SKIP] --settle-only: not taking True Prob snapshot for {today_et}.")
    elif _is_fresh(tp_today):
        print(f"\n[SKIP] True Prob snapshot for {today_et} is < {FRESHNESS_HOURS}h old. "
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
    soft_today = os.path.join(ROOT, "soft_scanner_history", f"{today_et}.json")
    if do_soft and _is_fresh(soft_today):
        print(f"[SKIP] Soft Scanner snapshot for {today_et} is < {FRESHNESS_HOURS}h old.")
    elif do_soft:
        def _soft_snap():
            from scripts.soft_scanner import scan as soft_scan
            picks = soft_scan(odds_key)
            print(f"  Found {len(picks)} soft-price plays (edge >= 5pp, 3+ books)")
            payload = {
                "date":        today_et,
                "snapshot_at": datetime.now(tz=EASTERN).isoformat(),
                "n_picks":     len(picks),
                "filter": {
                    "min_edge_pp": 5.0,
                    "min_books":   3,
                    "min_price":   -300,
                    "max_price":   300,
                    "top_n":       30,
                },
                "picks": picks,
            }
            out_dir = os.path.join(ROOT, "soft_scanner_history")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"{today_et}.json")
            import json
            with open(out_path, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            print(f"  Saved -> {out_path}")
            return True
        step(f"Snapshot Soft Scanner {today_et}", _soft_snap)

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
        def _git_push():
            subprocess.run(["git", "add",
                            "true_prob_history/", "soft_scanner_history/",
                            "hr_tracker/", "hrr_tracker/"],
                           cwd=ROOT, check=False)
            r = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=ROOT)
            if r.returncode == 0:
                print("  No history changes to commit.")
                return True
            subprocess.run(
                ["git", "commit", "-m",
                 f"Daily forward-test: settle {yest_et} + snapshot {today_et}"],
                cwd=ROOT, check=False,
            )
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT, check=False)
            r2 = subprocess.run(["git", "push", "origin", "main"], cwd=ROOT)
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
