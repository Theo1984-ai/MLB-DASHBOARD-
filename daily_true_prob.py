"""
Local daily True Probability runner — backup for the GitHub Actions cron.

Run this from Windows Task Scheduler (or just manually each day) the same
way you run daily_hr_tracker.py. It does both:
  1. Settles yesterday's snapshot (W/L/Push) via MLB Stats API.
  2. Takes today's snapshot of 75%+ true probability picks.

It writes to true_prob_history/ and does NOT push to GitHub itself — pair
this with `git add true_prob_history/ && git commit && git push` (or run
it manually each morning when you check the dashboard).

Usage (cmd / PowerShell):
    python daily_true_prob.py

Optional: also auto-push if invoked with --push:
    python daily_true_prob.py --push
"""
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    # Load API key the same way the rest of the project does.
    secrets_path = os.path.join(ROOT, ".streamlit", "secrets.toml")
    if not os.path.exists(secrets_path):
        print("ERROR: .streamlit/secrets.toml not found", file=sys.stderr)
        sys.exit(1)
    with open(secrets_path, "rb") as f:
        cfg = tomllib.load(f)
    os.environ["THE_ODDS_API_KEY"] = cfg["THE_ODDS_API_KEY"]

    today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    yest_et = (datetime.now(tz=EASTERN) - timedelta(days=1)).strftime("%Y-%m-%d")

    # 1) Settle yesterday's snapshot (no-op if no file)
    yest_path = os.path.join(ROOT, "true_prob_history", f"{yest_et}.json")
    if os.path.exists(yest_path):
        print(f"[1/2] Settling yesterday's snapshot ({yest_et})...")
        subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "true_prob_settler.py"), yest_et],
            cwd=ROOT, check=False,
        )
    else:
        print(f"[1/2] No snapshot found for {yest_et} — nothing to settle")

    # 2) Take today's snapshot
    print(f"\n[2/2] Taking today's snapshot ({today_et})...")
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "true_prob_snapshot.py")],
        cwd=ROOT, check=False,
    )

    # Optional: auto-push
    if "--push" in sys.argv:
        print("\n[push] Committing + pushing to GitHub...")
        subprocess.run(["git", "add", "true_prob_history/"], cwd=ROOT, check=False)
        subprocess.run(
            ["git", "commit", "-m", f"True Prob daily: settle {yest_et} + snapshot {today_et}"],
            cwd=ROOT, check=False,
        )
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=ROOT, check=False)
        subprocess.run(["git", "push", "origin", "main"], cwd=ROOT, check=False)


if __name__ == "__main__":
    main()
