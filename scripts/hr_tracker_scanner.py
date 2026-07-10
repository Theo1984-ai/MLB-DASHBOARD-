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

# --- Sweet-spot thresholds (backtest-driven, refined 7/9) ---
# Round 1 backtest (55 picks, comp 90-94 at +300-399): 27.3% hit / +19.5% ROI
# Round 2 win/loss analysis on 173 settled picks revealed:
#   - composite 92-95 was a TRAP: 19.4% hit (36 picks) — too high a score
#     often means the model is over-rating a batter who's cold or
#     matchup-disadvantaged
#   - composite 90-92 was pure gold: 42.1% hit (19 picks)
#   - consensus 18-20% was a dead zone: 0% hit on 14 picks (!)
#   - consensus 20%+ was consistently strong: 27%+ hit at all sub-buckets
# So narrow the composite window and require higher consensus floor.
SWEET_COMPOSITE_MIN = 88
SWEET_COMPOSITE_MAX = 92
SWEET_PRICE_MIN     = 300
SWEET_PRICE_MAX     = 399

# --- Broader-tier thresholds (fallback when sweet spot is empty) ---
BROAD_COMPOSITE_MIN = 85
BROAD_COMPOSITE_MAX = 94    # cap at 94 - 95+ underperforms
BROAD_PRICE_MIN     = 250
BROAD_PRICE_MAX     = 499

# --- Universal gates (7/9: tightened based on win/loss analysis) ---
# consensus 18-20% went 0-14 in backtest; 20%+ is where wins live
MIN_CONSENSUS_PCT   = 20.0     # was 10.0
# 1.0-1.5pp cross_book had only 17.6% hit; 1.5+ jumps to 25-35%
MIN_CROSS_BOOK_PP   = 1.5      # was 1.0


def _classify_tier(p):
    """A+ (sweet spot), A (broader), or None."""
    comp = p.get("score") or 0
    am = p.get("best_price") or 0
    if (SWEET_COMPOSITE_MIN <= comp <= SWEET_COMPOSITE_MAX
            and SWEET_PRICE_MIN <= am <= SWEET_PRICE_MAX):
        return "A+"
    if (BROAD_COMPOSITE_MIN <= comp <= BROAD_COMPOSITE_MAX
            and BROAD_PRICE_MIN <= am <= BROAD_PRICE_MAX):
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
    """Generate HR picks using the Fundamentals strategy.

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
          f"price +{SWEET_PRICE_MIN} to +{SWEET_PRICE_MAX})")
    print(f"  {sum(1 for p in filtered if p['_tier']=='A')} A picks "
          f"(composite {BROAD_COMPOSITE_MIN}+, price +{BROAD_PRICE_MIN} to "
          f"+{BROAD_PRICE_MAX}, excluding A+)")

    # Rank: A+ before A, then by composite descending, tiebreak by consensus
    def _rank(p):
        tier_rank = 0 if p["_tier"] == "A+" else 1
        return (tier_rank, -(p.get("score") or 0), -(p.get("consensus_pct") or 0))
    filtered.sort(key=_rank)

    if strict:
        picks = [_to_pick(p, p["_tier"]) for p in filtered[:top_n]]
    else:
        picks = [_to_pick(p, p.get("_tier")) for p in filtered]

    return {
        "date":     today,
        "saved_at": datetime.now(tz=EASTERN).isoformat(),
        "top_n":    top_n,
        "strategy": "fundamentals_hr_v1",
        "filter": {
            "sweet_composite":  [SWEET_COMPOSITE_MIN, SWEET_COMPOSITE_MAX],
            "sweet_price":      [SWEET_PRICE_MIN, SWEET_PRICE_MAX],
            "broad_composite":  BROAD_COMPOSITE_MIN,
            "broad_price":      [BROAD_PRICE_MIN, BROAD_PRICE_MAX],
            "min_cross_book_pp": MIN_CROSS_BOOK_PP,
            "min_consensus_pct": MIN_CONSENSUS_PCT,
        },
        "n_games":  result.get("n_games", 0),
        "n_total":  len(filtered),
        "n_a_plus": sum(1 for p in filtered if p["_tier"] == "A+"),
        "n_a":      sum(1 for p in filtered if p["_tier"] == "A"),
        "picks":    picks,
    }
