"""
Daily picks auto-updater — GitHub Actions cron entry point.
Delegates the actual scan logic to scripts/picks_updater.py so the same
code path is shared with the Streamlit "Update Now" button.
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

KEY = os.environ.get("THE_ODDS_API_KEY")
if not KEY:
    print("ERROR: THE_ODDS_API_KEY env var not set")
    sys.exit(1)

from scripts.picks_updater import run_scan

print(f"=== Daily picks update ===")
payload = run_scan(KEY)

out_path = os.path.join(ROOT, "tonight_picks", "latest.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)

# Guard: don't overwrite a good file with an empty one.
# Early-AM cron runs find 0 picks because sportsbooks haven't posted full
# lines yet. If today's file already exists with picks, keep it and exit
# cleanly so users don't see an empty Tonight's Picks page.
new_top = len(payload.get("top_picks", []))
new_safe = len(payload.get("safe_locks", []))
new_total = new_top + new_safe
today_date = payload.get("date") or ""

should_write = True
if new_total == 0 and os.path.exists(out_path):
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        existing_total = (len(existing.get("top_picks", []))
                          + len(existing.get("safe_locks", [])))
        if existing.get("date") == today_date and existing_total > 0:
            should_write = False
            print(f"\n⚠ Scan returned 0 picks — keeping existing file with "
                  f"{existing_total} picks for {today_date}.")
            print(f"   (Sportsbooks likely haven't fully posted lines — "
                  f"next cron will retry.)")
    except Exception:
        pass

if should_write:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n✅ Wrote {new_top} top picks "
          f"+ {len(payload.get('concentration_plays', []))} concentration plays "
          f"+ {new_safe} safe locks")
    print(f"   → {out_path}")
    if payload.get("top_picks"):
        p = payload["top_picks"][0]
        print(f"   🌟 {p['player']} {p['market']} {p['side']} {p['line']} "
              f"@ {p['price']:+d} ({p['book']}) — EV ${p['ev']:+.2f}")
