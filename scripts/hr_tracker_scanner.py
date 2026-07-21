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

# --- A+ tier DROPPED 7/21 based on live performance ---
# Backtest projected 88-92 composite as the "sweet spot" but 66 live
# settled picks (7/11-7/20) showed:
#   A+ (comp 88-92): 31 picks, 1 win, 3.2% hit, -85.8% ROI, -$266 P&L
#   A  (comp 85-87 + 93-94): 35 picks, 25.7% hit, +9.9% ROI, +$34 P&L
# The market has apparently caught on to the Barrel-composite signal in
# the 88-92 band. The "marginal" A tier (composite bands that used to be
# considered less predictive) is now the profitable one.
#
# Fix: exclude composite 88-92 entirely. Only take A tier: 85-87 or 93-94.
# Kept SWEET_ constants for reference / eventual re-enable if data changes.
SWEET_COMPOSITE_MIN = 88   # (dropped tier)
SWEET_COMPOSITE_MAX = 92   # (dropped tier)
SWEET_TIER_ENABLED  = False   # <-- flip to True to re-enable A+ if perf recovers

# --- A tier: composite 85-87 or 93-94 (skipping the dead 88-92 zone) ---
BROAD_COMPOSITE_MIN = 85
BROAD_COMPOSITE_MAX = 94

# --- Universal gates (7/9: tightened based on win/loss analysis) ---
MIN_CONSENSUS_PCT   = 20.0
MIN_CROSS_BOOK_PP   = 1.5


def _classify_tier(p):
    """Return 'A' for composite in the profitable bands (85-87 or 93-94),
    or None. The A+ 'sweet spot' (composite 88-92) is DROPPED as of 7/21
    after 31 live picks went 1-30 (3.2% hit, -85.8% ROI).

    If SWEET_TIER_ENABLED is flipped back to True, A+ takes precedence
    over A for composite in 88-92 (original behavior)."""
    comp = p.get("score") or 0
    if SWEET_TIER_ENABLED:
        if SWEET_COMPOSITE_MIN <= comp <= SWEET_COMPOSITE_MAX:
            return "A+"
    else:
        # A+ dropped: composite 88-92 explicitly EXCLUDED (not just renamed).
        if SWEET_COMPOSITE_MIN <= comp <= SWEET_COMPOSITE_MAX:
            return None
    if BROAD_COMPOSITE_MIN <= comp <= BROAD_COMPOSITE_MAX:
        return "A"
    return None


def _to_pick(p, tier, game_pk_map=None):
    """Reshape a Fundamentals HR play into the HR Tracker output format.

    game_pk_map: optional dict of (away_team, home_team) → game_pk (int)
                 used to populate the game_pk field. Required so the page's
                 history/results section can grade past picks against MLB
                 boxscore data.
    """
    game_pk = None
    if game_pk_map:
        game_pk = game_pk_map.get((p.get("away_team"), p.get("home_team")))
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
        "game_pk":     game_pk,   # needed by page for HR grading
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

    # Build (away, home) -> game_pk map from today's MLB schedule so we can
    # attach game_pk to each pick. Without this the page's history/results
    # section KeyErrors when grading picks.
    game_pk_map = {}
    try:
        from data import mlb_api
        for g in mlb_api.get_schedule(today):
            away = g.get("teams", {}).get("away", {}).get("team", {}).get("name")
            home = g.get("teams", {}).get("home", {}).get("team", {}).get("name")
            gpk = g.get("gamePk")
            if away and home and gpk:
                game_pk_map[(away, home)] = int(gpk)
    except Exception as e:
        print(f"  Warning: game_pk lookup failed ({e}) — picks will lack game_pk")

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

    if SWEET_TIER_ENABLED:
        print(f"  {sum(1 for p in filtered if p['_tier']=='A+')} A+ picks "
              f"(composite {SWEET_COMPOSITE_MIN}-{SWEET_COMPOSITE_MAX}, "
              f"any price)")
    else:
        print(f"  A+ tier DISABLED (composite "
              f"{SWEET_COMPOSITE_MIN}-{SWEET_COMPOSITE_MAX} excluded — "
              f"1-30 record in live picks 7/11-7/20)")
    print(f"  {sum(1 for p in filtered if p['_tier']=='A')} A picks "
          f"(composite {BROAD_COMPOSITE_MIN}-{BROAD_COMPOSITE_MAX} "
          f"excluding 88-92 dead zone, any price)")

    # Rank: A+ before A, then by composite descending, tiebreak by consensus
    def _rank(p):
        tier_rank = 0 if p["_tier"] == "A+" else 1
        return (tier_rank, -(p.get("score") or 0), -(p.get("consensus_pct") or 0))
    filtered.sort(key=_rank)

    if strict:
        new_picks = [_to_pick(p, p["_tier"], game_pk_map) for p in filtered[:top_n]]
    else:
        new_picks = [_to_pick(p, p.get("_tier"), game_pk_map) for p in filtered]

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
        "strategy": "fundamentals_hr_v3_a_only",
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
