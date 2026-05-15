"""
HR model backtest — compares saved predictions vs actual MLB game results.

Usage:
    python backtest.py [--days N]   (default: last 15 days with data)

Fetches boxscores from the MLB Stats API for each game_pk in the prediction
logs, extracts per-batter HR counts, and computes:
  - Calibration: predicted % vs actual HR rate by probability bucket
  - Hit rate for top-N picks
  - Brier score (proper scoring rule for probabilities)
  - Simulated ROI at typical sportsbook odds
"""

import json
import glob
import os
import sys
import time
import urllib.request
import ssl as _ssl_compat
_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
import urllib.error
from collections import defaultdict
from datetime import date, timedelta

# Apply the same post-hoc calibration used in the live model
# (stored JSONL files have un-calibrated values; this makes the backtest
#  reflect what the calibrated model would have predicted)
def _calibrate(p: float) -> float:
    if 0.15 <= p < 0.40:
        return p * 0.75
    return p


# ── MLB Stats API helpers ────────────────────────────────────────────────────

MLB_BASE = "https://statsapi.mlb.com/api/v1"


def _get(url: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mlb-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=20, context=_UNVERIFIED_SSL) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt)
                continue
            return {}
        except Exception:
            return {}
    return {}


def get_boxscore_hrs(game_pk: int) -> dict[int, int]:
    """
    Returns dict mapping batter_id → HR count for a completed game.
    Returns {} if game not found or not yet final.
    """
    data = _get(f"{MLB_BASE}/game/{game_pk}/boxscore")
    teams = data.get("teams", {})
    result: dict[int, int] = {}
    for side in ("away", "home"):
        players = teams.get(side, {}).get("players", {})
        for pkey, pdata in players.items():
            pid = pdata.get("person", {}).get("id")
            stats = pdata.get("stats", {}).get("batting", {})
            hr = stats.get("homeRuns", 0)
            if pid is not None:
                result[pid] = hr
    return result


def get_game_status(game_pk: int) -> str:
    """Returns game status string (e.g. 'Final', 'Scheduled')."""
    data = _get(f"{MLB_BASE}/game/{game_pk}/linescore")
    return data.get("gameStatus", {}) or {}


# ── Load predictions ─────────────────────────────────────────────────────────

def _norm_name(name: str) -> str:
    """Lowercase, strip accents, keep letters and spaces only for fuzzy matching."""
    import unicodedata
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    return name.lower().strip()


def load_odds_snapshot(date_str: str) -> dict[str, dict]:
    """Load saved sportsbook odds for a date. Returns {} if file missing."""
    path = os.path.join("odds", f"{date_str}.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    # Normalize keys for matching
    return {_norm_name(k): v for k, v in raw.items()}


def load_predictions(days: int = 15) -> list[dict]:
    cutoff = date.today() - timedelta(days=days)
    rows = []
    for path in sorted(glob.glob("predictions/2026-*.jsonl")):
        fname = os.path.basename(path)
        d = fname.replace(".jsonl", "")
        try:
            if date.fromisoformat(d) < cutoff:
                continue
        except ValueError:
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    # Skip malformed records missing required fields
                    if "date" not in r or "batter_id" not in r or "p_at_least_one" not in r:
                        continue
                    # Apply calibration correction to stored (un-calibrated) values
                    r["p_at_least_one"] = _calibrate(r["p_at_least_one"])
                    rows.append(r)
    return rows


# ── Main backtest ─────────────────────────────────────────────────────────────

def run_backtest(days: int = 15):
    print(f"\n{'='*60}")
    print(f"  MLB HR Model Backtest — last {days} days")
    print(f"{'='*60}\n")

    preds = load_predictions(days)
    if not preds:
        print("No predictions found.")
        return

    print(f"Loaded {len(preds)} predictions across {len({p['date'] for p in preds})} days\n")

    # ── Fetch actual results ─────────────────────────────────────────────────
    # Cache by game_pk to avoid duplicate API calls
    game_hr_cache: dict[int, dict[int, int]] = {}
    unique_games = {p["game_pk"] for p in preds if p.get("game_pk")}

    print(f"Fetching boxscores for {len(unique_games)} games...")
    for i, gk in enumerate(sorted(unique_games)):
        if gk not in game_hr_cache:
            hrs = get_boxscore_hrs(int(gk))
            game_hr_cache[gk] = hrs
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(unique_games)} games fetched...")
        time.sleep(0.15)  # be nice to the API
    print(f"  Done.\n")

    # ── Attach actual outcome to each prediction ─────────────────────────────
    resolved = []
    skipped = 0
    for p in preds:
        gk = p.get("game_pk")
        bid = p.get("batter_id")
        if not gk or not bid:
            skipped += 1
            continue
        hr_map = game_hr_cache.get(gk, {})
        if not hr_map:
            skipped += 1
            continue  # game not yet played / no boxscore
        actual_hr = hr_map.get(int(bid), 0)
        hit = 1 if actual_hr >= 1 else 0
        resolved.append({
            **p,
            "actual_hr":  actual_hr,
            "hit":        hit,
            "model_p":    p.get("p_at_least_one", 0),
        })

    print(f"Resolved: {len(resolved)} predictions  |  Skipped (no result): {skipped}\n")
    if not resolved:
        print("No resolved predictions — games may not be final yet.")
        return

    total = len(resolved)
    total_hits = sum(r["hit"] for r in resolved)
    actual_rate = total_hits / total
    avg_model_p = sum(r["model_p"] for r in resolved) / total

    print(f"Overall Stats")
    print(f"  Predictions resolved : {total}")
    print(f"  Actual HR rate       : {actual_rate*100:.2f}%  ({total_hits} HRs)")
    print(f"  Avg model probability: {avg_model_p*100:.2f}%")
    print(f"  Over-confidence      : {(avg_model_p - actual_rate)*100:+.2f}pp\n")

    # ── Brier score ──────────────────────────────────────────────────────────
    brier = sum((r["model_p"] - r["hit"]) ** 2 for r in resolved) / total
    # Baseline Brier (always predict league avg HR rate)
    baseline_brier = sum((actual_rate - r["hit"]) ** 2 for r in resolved) / total
    brier_skill = 1 - (brier / baseline_brier) if baseline_brier else 0

    print(f"Brier Score (lower = better)")
    print(f"  Model Brier score    : {brier:.4f}")
    print(f"  Baseline Brier score : {baseline_brier:.4f}  (always predict {actual_rate*100:.1f}%)")
    print(f"  Brier Skill Score    : {brier_skill*100:.1f}%  (0%=no skill, 100%=perfect)\n")

    # ── Calibration by probability bucket ────────────────────────────────────
    buckets = [
        (0.00, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
        (0.20, 0.25), (0.25, 0.30), (0.30, 0.40), (0.40, 1.00),
    ]
    print(f"Calibration by Probability Bucket")
    print(f"  {'Bucket':<14} {'N':>5} {'Avg Pred':>10} {'Actual HR%':>12} {'Diff':>8}")
    print(f"  {'-'*52}")
    for lo, hi in buckets:
        bucket_rows = [r for r in resolved if lo <= r["model_p"] < hi]
        n = len(bucket_rows)
        if n == 0:
            continue
        avg_p  = sum(r["model_p"] for r in bucket_rows) / n
        act    = sum(r["hit"] for r in bucket_rows) / n
        diff   = avg_p - act
        flag   = "  << over" if diff > 0.04 else ("  << under" if diff < -0.04 else "")
        print(f"  {lo*100:.0f}–{hi*100:.0f}%{'':<8} {n:>5} {avg_p*100:>9.1f}% {act*100:>11.1f}% {diff*100:>+7.1f}pp{flag}")
    print()

    # ── Top-N hit rate ────────────────────────────────────────────────────────
    # Group by date, take top N picks per day, check hit rate
    by_date: dict[str, list] = defaultdict(list)
    for r in resolved:
        by_date[r["date"]].append(r)

    print(f"Top-N Pick Hit Rate (per-day top picks)")
    print(f"  {'Top N':<8} {'Days':>6} {'Picks':>7} {'Hits':>6} {'Hit Rate':>10} {'Avg Odds (est)':>15}")
    print(f"  {'-'*54}")
    for top_n in [1, 2, 3, 5, 10]:
        days_data, picks, hits = [], [], []
        for d, rows in by_date.items():
            sorted_rows = sorted(rows, key=lambda x: -x["model_p"])
            top = sorted_rows[:top_n]
            if not top:
                continue
            days_data.append(d)
            picks.extend(top)
            hits.extend([r for r in top if r["hit"]])
        n_picks = len(picks)
        n_hits  = len(hits)
        hr = n_hits / n_picks if n_picks else 0
        # Estimate market implied: median model_p for these picks
        med_p = sorted(r["model_p"] for r in picks)[len(picks)//2] if picks else 0
        # Typical sportsbook HR prop at ~med_p: american odds ≈ (100/p - 100) for underdogs
        est_odds = int((1/med_p - 1) * 100) if med_p > 0 else 0
        print(f"  Top {top_n:<4} {len(days_data):>6} {n_picks:>7} {n_hits:>6} {hr*100:>9.1f}%   +{est_odds:>5}")
    print()

    # ── Day-by-day top pick ───────────────────────────────────────────────────
    print(f"Day-by-Day Top Pick")
    print(f"  {'Date':<12} {'Batter':<22} {'Team':<20} {'Model%':>8} {'vs SP':<22} {'HR?':>5}")
    print(f"  {'-'*95}")
    for d in sorted(by_date.keys()):
        rows = sorted(by_date[d], key=lambda x: -x["model_p"])
        if not rows:
            continue
        top = rows[0]
        hit_str = "YES" if top["hit"] else "no"
        bname = top.get("batter_name", "?")
        if "," in bname:
            last, _, first = bname.partition(",")
            bname = f"{first.strip()} {last.strip()}"
        sp = top.get("opp_pitcher", "?")
        print(f"  {d:<12} {bname:<22} {top.get('team','?'):<20} {top['model_p']*100:>7.1f}%  {sp:<22} {hit_str:>5}")

    # ── Simulated betting ROI (flat $100 bets on top-3 daily) ────────────────
    # Load real sportsbook odds snapshots (saved when Fetch HR Odds is clicked)
    odds_by_date: dict[str, dict] = {}
    for d in by_date:
        odds_by_date[d] = load_odds_snapshot(d)
    days_with_real_odds = sum(1 for v in odds_by_date.values() if v)

    print(f"\nSimulated Flat-Bet ROI — Top 3 daily, $100/bet")
    if days_with_real_odds:
        print(f"  Real sportsbook odds available for {days_with_real_odds}/{len(by_date)} days")
        print(f"  (Using best book odds when available, fair odds as fallback)")
    else:
        print(f"  (No sportsbook odds snapshots found — using fair odds 1/p as proxy)")
        print(f"  Tip: click 'Fetch HR Odds' in the dashboard to save real book prices")

    w_fair = w_real = r_fair = r_real = bets_fair = bets_real = wins = 0
    real_odds_matched = 0

    for d in sorted(by_date.keys()):
        day_odds = odds_by_date.get(d, {})
        rows = sorted(by_date[d], key=lambda x: -x["model_p"])
        for r in rows[:3]:
            p = r["model_p"]
            if p <= 0:
                continue
            hit = r["hit"]

            # Fair-odds calc (always available)
            fair_dec = 1.0 / p
            w_fair += 100
            r_fair += 100.0 * fair_dec if hit else 0.0
            bets_fair += 1
            if hit:
                wins += 1

            # Real-odds calc (use snapshot if player name matched)
            bname = r.get("batter_name", "")
            if "," in bname:
                last, _, first = bname.partition(",")
                bname = f"{first.strip()} {last.strip()}"
            norm = _norm_name(bname)
            snap = day_odds.get(norm)
            if snap:
                real_odds_matched += 1
                am = snap["best_odds"]
                # American odds → decimal payout per $100
                if am >= 0:
                    real_dec = am / 100.0 + 1.0
                else:
                    real_dec = 100.0 / abs(am) + 1.0
                w_real += 100
                r_real += 100.0 * real_dec if hit else 0.0
                bets_real += 1

    roi_fair = (r_fair - w_fair) / w_fair * 100 if w_fair else 0
    roi_real = (r_real - w_real) / w_real * 100 if w_real else 0

    print(f"\n  -- Fair odds (1/model_p, no vig) --")
    print(f"  Total bets    : {bets_fair}  ({wins} winners)")
    print(f"  Total wagered : ${w_fair:,.0f}")
    print(f"  Total returned: ${r_fair:,.0f}")
    print(f"  ROI (vs fair) : {roi_fair:+.1f}%")

    if bets_real > 0:
        roi_real_val = (r_real - w_real) / w_real * 100
        print(f"\n  -- Real sportsbook odds ({real_odds_matched} bets matched) --")
        print(f"  Total bets    : {bets_real}  (winners from above)")
        print(f"  Total wagered : ${w_real:,.0f}")
        print(f"  Total returned: ${r_real:,.0f}")
        print(f"  ROI (real bk) : {roi_real_val:+.1f}%")
    else:
        print(f"\n  -- Real sportsbook odds --")
        print(f"  No odds snapshots found. Fetch HR Odds in the dashboard to enable this.")

    # ── Model precision at high confidence ───────────────────────────────────
    print(f"\nHigh-Confidence Picks (model >= 25%)")
    high_conf = [r for r in resolved if r["model_p"] >= 0.25]
    if high_conf:
        hc_hits = sum(r["hit"] for r in high_conf)
        hc_rate = hc_hits / len(high_conf)
        avg_p = sum(r["model_p"] for r in high_conf) / len(high_conf)
        print(f"  Count         : {len(high_conf)}")
        print(f"  Avg model prob: {avg_p*100:.1f}%")
        print(f"  Actual HR rate: {hc_rate*100:.1f}%")
        print(f"  Calibration   : {(avg_p - hc_rate)*100:+.1f}pp")
    else:
        print("  None found.")

    # ── Best and worst predicted days ─────────────────────────────────────────
    print(f"\nBest & Worst Days (by top-5 hit rate)")
    day_rates = []
    for d, rows in by_date.items():
        top5 = sorted(rows, key=lambda x: -x["model_p"])[:5]
        if len(top5) >= 3:
            hr = sum(r["hit"] for r in top5) / len(top5)
            day_rates.append((d, hr, len(top5), sum(r["hit"] for r in top5)))
    day_rates.sort(key=lambda x: -x[1])
    print(f"  Best days:")
    for d, hr, n, h in day_rates[:3]:
        print(f"    {d}: {h}/{n} top picks hit  ({hr*100:.0f}%)")
    print(f"  Worst days:")
    for d, hr, n, h in day_rates[-3:]:
        print(f"    {d}: {h}/{n} top picks hit  ({hr*100:.0f}%)")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    days = 15
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--days" and i + 1 < len(sys.argv) - 1:
            days = int(sys.argv[i + 2])
        elif arg.isdigit():
            days = int(arg)
    run_backtest(days)
