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
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

print(f"\n✅ Wrote {len(payload.get('top_picks', []))} top picks "
      f"+ {len(payload.get('concentration_plays', []))} concentration plays")
print(f"   → {out_path}")
if payload.get("top_picks"):
    p = payload["top_picks"][0]
    print(f"   🌟 {p['player']} {p['market']} {p['side']} {p['line']} "
          f"@ {p['price']:+d} ({p['book']}) — EV ${p['ev']:+.2f}")
