"""
HR Tracker — REWRITTEN 7/9 to use the Fundamentals HR strategy.

OLD APPROACH (deleted): physics-based multiplicative model
  base × Bskill × Mpit × Fpark × Ftemp × Fhumid × Fwind
  Historical: 118 picks, 18.6% hit, -30.6% ROI. Broken.

NEW APPROACH: Fundamentals Barrel% / xSLG composite score.
  - Elite Statcast hitters (composite 90-94 = sweet spot per backtest)
  - Price band +300 to +399 (moderate longshots, not extreme)
  - Cross-book edge >= 1pp
  - Consensus implied >= 10%
  Historical: 55 picks in composite 90-94 sweet spot returned +19.5% ROI.
              66 picks in +300-399 price bucket returned +14.6% ROI.
              Intersection (~35 picks) projected at +15-20% ROI.

The physics HR model is untouched — models/hr_model.py still exists so
other code paths (backtest, calibration analysis) can reference it, but
this tracker no longer uses it. All model-based fields removed from the
saved snapshot.
"""
from __future__ import annotations

import json
import os
import ssl as _ssl
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.fundamentals_scanner import scan as _fund_scan  # noqa: E402

_SSL = _ssl._create_unverified_context()
EASTERN = ZoneInfo("America/New_York")

# --- Sweet-spot thresholds (backtest-driven, refined 7/9 v2) ---
# v1 (25% ROI): required composite 88-92 AND price 300-399 → 14 picks
# v2 backtest: dropping the price band adds ~2× volume with equivalent ROI:
#   comp 88-92 + price 300-399:  14 picks, 42.9% hit, +88.9% ROI
#   comp 88-92 no price filter:  30 picks, 40.0% hit, +82.2% ROI  <-- v2
# Price bracket was doing almost no filtering work — the composite +
# consensus + cross_book gates already capture the signal. Removing it
# roughly doubles daily volume while preserving hit rate and ROI.
SWEET_COMPOSITE_MIN = 88
SWEET_COMPOSITE_MAX = 92
# Price filter dropped — no bracket

# --- Broader-tier thresholds (marginal composite bands, fills volume) ---
# comp 85-87 or 93-94 with all other gates: breakeven historically. Kept
# in the file for volume during Round Robin construction, but tagged "A"
# not "A+" so users know these are the marginal-quality picks.
BROAD_COMPOSITE_MIN = 85
BROAD_COMPOSITE_MAX = 94
# No price filter on A tier either

# --- Universal gates (7/9: tightened based on win/loss analysis) ---
MIN_CONSENSUS_PCT   = 20.0
MIN_CROSS_BOOK_PP   = 1.5


def _classify_tier(p):
    """A+ (composite sweet spot) or A (composite marginal). Price no longer
    factors in — backtest showed the price bracket wasn't doing filtering
    work on top of the composite + universal gates."""
    comp = p.get("score") or 0
    if SWEET_COMPOSITE_MIN <= comp <= SWEET_COMPOSITE_MAX:
        return "A+"
    if BROAD_COMPOSITE_MIN <= comp <= BROAD_COMPOSITE_MAX:
        return "A"
    return None


def _to_pick(p, tier):
    """Reshape a Fundamentals HR play into the HR Tracker output format."""
    return {
        # Settle-required
        "stat_key":    "hr",
        "side":        p.get("side", "Over"),
        "point":       p.get("point", 0.5),
        "away_team":   p.get("away_team"),
        "home_team":   p.get("home_team"),
        "first_pitch": p.get("first_pitch"),
        "best_price":  p.get("best_price"),
        "player":      p.get("player"),
        "batter":      p.get("player"),
        "batter_id":   p.get("batter_id"),
        # Display / context
        "team":        p.get("away_team") if p.get("player") in str(p.get("away_team",""))
                       else None,  # best-effort; fundamentals doesn't always have team
        "matchup":     p.get("game"),
        "game":        p.get("game"),
        "vs_sp":       p.get("opp_pitcher") or "TBD",
        "park":        None,
        "best_odds":   p.get("best_price"),
        "best_book":   p.get("best_book"),
        # Fundamentals context (this IS the model now)
        "composite":         p.get("score"),
        "xwoba":             p.get("xwoba"),
        "hard_hit":          p.get("hard_hit"),
        "barrel":            p.get("barrel"),
        "consensus_pct":     p.get("consensus_pct"),
        "cross_book_pp":     p.get("cross_book_pp"),
        "edge_pp":           p.get("edge_pp"),
        # Cross-tracker compat aliases
        "consensus_implied_pct": p.get("consensus_pct"),
        "implied_pct":           p.get("consensus_pct"),
        "n_books":               p.get("n_books"),
        # Model-based fields removed intentionally — the composite score
        # IS the model now. These are set to None so any downstream code
        # reading them gets a clear "not applicable" signal.
        "model_p":     None,
        "model_p_pct": None,
        "confidence":  int(p.get("score", 0)),   # composite becomes confidence
        "confidence_tier": tier,
    }


def generate_hr_picks(odds_key, season=None, top_n=8, strict=True):
    """Generate HR picks using the Fundamentals strategy — MERGE-WITH-
    PERSISTENCE (7/9): if today's file already has picks, merge new
    picks with existing ones instead of overwriting.

    Fixes the "seen a pick in the morning, gone by game time" bug: prices
    move, lineups get confirmed, and composite scores recalculate, causing
    yesterday's morning A+ picks to drop out of subsequent scans. Merging
    preserves anything that ever qualified so users see the full day's
    opportunity set.

    Returns: dict with date, saved_at, top_n, n_games, n_total, picks
    (ready to pass to gh.save_json).
    """
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    if season is None:
        season = int(today[:4])

    result = _fund_scan(odds_key, season=season, min_composite=80)
    all_hr = result.get("hr_plays") or []
    print(f"  Fundamentals produced {len(all_hr)} HR candidates")

    # Apply universal gates (consensus & cross-book), then classify tier
    filtered = []
    for p in all_hr:
        cons = p.get("consensus_pct") or 0
        xbk = p.get("cross_book_pp") or 0
        if cons < MIN_CONSENSUS_PCT: continue
        if xbk < MIN_CROSS_BOOK_PP: continue
        if p.get("side") not in ("Yes", "Over"): continue
        if p.get("away_team") is None: continue
        tier = _classify_tier(p)
        if tier is None: continue
        p["_tier"] = tier
        filtered.append(p)

    print(f"  {sum(1 for p in filtered if p['_tier']=='A+')} A+ picks "
          f"(composite {SWEET_COMPOSITE_MIN}-{SWEET_COMPOSITE_MAX}, "
          f"any price)")
    print(f"  {sum(1 for p in filtered if p['_tier']=='A')} A picks "
          f"(composite {BROAD_COMPOSITE_MIN}-{BROAD_COMPOSITE_MAX} "
          f"excluding A+, any price)")

    # Rank: A+ before A, then by composite descending, tiebreak by consensus
    def _rank(p):
        tier_rank = 0 if p["_tier"] == "A+" else 1
        return (tier_rank, -(p.get("score") or 0), -(p.get("consensus_pct") or 0))
    filtered.sort(key=_rank)

    if strict:
        new_picks = [_to_pick(p, p["_tier"]) for p in filtered[:top_n]]
    else:
        new_picks = [_to_pick(p, p.get("_tier")) for p in filtered]

    # --- MERGE WITH EXISTING FILE (7/9) ---
    # Preserve any picks saved earlier today that no longer pass the current
    # scan (price moved, lineup changed, etc). Track n_appearances so users
    # can see which picks were persistently qualifying vs one-time flashes.
    try:
        from scripts.snapshot_merger import race_safe_git_pull, load_existing, merge_picks
        race_safe_git_pull(ROOT)
        out_path = os.path.join(ROOT, "hr_tracker", f"{today}.json")
        existing = load_existing(out_path)
        # Key by batter — same batter is the same "pick" even if price changes
        def _key(p):
            return (p.get("batter_id") or p.get("player") or p.get("batter"),
                    p.get("game") or p.get("matchup"))
        merged, added, rehit = merge_picks(
            existing, new_picks, _key,
            rolling_fields=("best_price", "best_book", "composite",
                            "consensus_pct", "cross_book_pp", "edge_pp",
                            "confidence", "confidence_tier"),
            history_fields={"comp": "composite", "cons": "consensus_pct",
                            "price": "best_price", "tier": "confidence_tier"},
        )
        # Sort merged list so current A+ picks stay at the top; picks that
        # HAVE been A+ at any point today (via history) come before A picks
        def _merged_rank(p):
            # Find best tier ever seen (from history)
            hist = p.get("scan_history") or []
            tiers = [h.get("tier") for h in hist] + [p.get("confidence_tier")]
            best_tier = "A+" if "A+" in tiers else "A"
            tier_rank = 0 if best_tier == "A+" else 1
            n_app = p.get("n_appearances") or 1
            return (tier_rank, -n_app, -(p.get("composite") or 0))
        merged.sort(key=_merged_rank)
        picks_out = merged
        print(f"  Merged: {added} new + {rehit} re-appearances "
              f"(had {len(existing)}, total {len(merged)})")
    except Exception as e:
        print(f"  Merge failed ({e}) — falling back to overwrite mode")
        picks_out = new_picks

    return {
        "date":     today,
        "saved_at": datetime.now(tz=EASTERN).isoformat(),
        "top_n":    top_n,
        "strategy": "fundamentals_hr_v2_no_price_filter",
        "filter": {
            "sweet_composite":  [SWEET_COMPOSITE_MIN, SWEET_COMPOSITE_MAX],
            "broad_composite":  [BROAD_COMPOSITE_MIN, BROAD_COMPOSITE_MAX],
            "min_cross_book_pp": MIN_CROSS_BOOK_PP,
            "min_consensus_pct": MIN_CONSENSUS_PCT,
            "price_filter":     "none (v2)",
        },
        "n_games":  result.get("n_games", 0),
        "n_total":  len(filtered),
        "n_a_plus": sum(1 for p in filtered if p["_tier"] == "A+"),
        "n_a":      sum(1 for p in filtered if p["_tier"] == "A"),
        "n_picks":  len(picks_out),
        "picks":    picks_out,
    }
