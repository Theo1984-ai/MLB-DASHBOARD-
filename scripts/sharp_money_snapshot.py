"""
Sharp Money daily snapshot — saves actionable cross-venue plays.

Captures only plays passing the strict filter:
  - edge >= 3pp (real edge, not vig-eaten noise)
  - skew_strength >= 70 (real sharp conviction)
  - liquidity >= $10K (real Polymarket money behind it)
  - sportsbook match found (so the play is actionable)

Output: sharp_money_history/YYYY-MM-DD.json
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.polymarket_sharp import scan as pm_scan       # noqa: E402
from scripts.sportsbook_matcher import match_signals       # noqa: E402

EASTERN = ZoneInfo("America/New_York")

# Same threshold as the page's "Top Actionable Plays" section
MIN_EDGE_PP = 3.0
MIN_SKEW = 70.0
MIN_LIQUIDITY = 10000


def to_settler_pick(p):
    """Reshape a sharp money pick into the settler's expected schema."""
    return {
        # Settle-required fields
        "stat_key":    p.get("bet_stat_key"),
        "side":        p.get("bet_side"),
        "point":       p.get("bet_point"),
        "away_team":   p.get("sb_away_team"),
        "home_team":   p.get("sb_home_team"),
        "first_pitch": p.get("first_pitch"),
        "best_price":  p.get("best_price"),
        "player":      None,    # game-level plays have no player
        # Display fields
        "selection":   p.get("play", ""),
        "market":      p.get("category", ""),
        "sharp_pick":  p.get("sharp_pick", ""),
        "game":        p.get("event", ""),
        "question":    p.get("question", ""),
        # Sharp money context
        "edge_pp":           p.get("edge_pp"),
        "skew_strength":     p.get("skew_strength"),
        "skew_side":         p.get("skew_side"),
        "liquidity":         p.get("liquidity"),
        "volume":            p.get("volume"),
        "yes_bid_depth":     p.get("yes_bid_depth"),
        "no_bid_depth":      p.get("no_bid_depth"),
        "pm_mid":            p.get("mid"),
        "pm_implied_pct":    (p.get("mid", 0) * 100 if p.get("skew_side")=="YES"
                              else (1 - (p.get("mid", 0))) * 100),
        "sb_book":           p.get("sb_book"),
        "sb_implied_pct":    p.get("sb_implied_pct"),
    }


def main():
    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
        # Try secrets.toml as fallback (local runs)
        try:
            import tomllib
            with open(os.path.join(ROOT, ".streamlit", "secrets.toml"), "rb") as f:
                api_key = tomllib.load(f).get("THE_ODDS_API_KEY")
        except Exception:
            pass
    if not api_key:
        print("ERROR: THE_ODDS_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    out_dir = os.path.join(ROOT, "sharp_money_history")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{today_et}.json")

    print(f"Running Sharp Money scan for {today_et}...")
    pm_rows, _debug = pm_scan(top_n=50)
    print(f"  Polymarket scan: {len(pm_rows)} markets")

    matched = match_signals(pm_rows, api_key)
    print(f"  Sportsbook matched: {sum(1 for m in matched if m.get('sb_best_price') is not None)} of {len(matched)}")

    # Filter to actionable only
    actionable = [m for m in matched
                  if m.get("edge_pp") is not None and m["edge_pp"] >= MIN_EDGE_PP
                  and m.get("skew_strength", 0) >= MIN_SKEW
                  and (m.get("liquidity") or 0) >= MIN_LIQUIDITY
                  and m.get("sb_best_price") is not None
                  and m.get("bet_stat_key") is not None]
    actionable.sort(key=lambda r: -r["edge_pp"])

    print(f"  Actionable (edge>={MIN_EDGE_PP}pp, skew>={MIN_SKEW}%, liq>=${MIN_LIQUIDITY}): {len(actionable)}")

    # Convert to settler-compatible format
    picks = [to_settler_pick(p) for p in actionable]

    payload = {
        "date":        today_et,
        "snapshot_at": datetime.now(tz=EASTERN).isoformat(),
        "n_picks":     len(picks),
        "filter": {
            "min_edge_pp":   MIN_EDGE_PP,
            "min_skew_pct":  MIN_SKEW,
            "min_liquidity": MIN_LIQUIDITY,
        },
        "picks": picks,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"  Saved -> {out_path}")


if __name__ == "__main__":
    main()
