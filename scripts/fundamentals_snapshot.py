"""
Fundamentals Props daily snapshot — saves actionable picks from the
Statcast framework so we can track its real W/L over time.

Filter (actionable):
  - composite_score >= 80 (top 20% of MLB hitters)
  - consensus_pct >= 60 (sportsbooks see real probability)
  - cross_book_pp >= 2 (some line dispersion / sharp signal)
  - 3+ books pricing it

Output: fundamentals_history/YYYY-MM-DD.json
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.fundamentals_scanner import scan  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

# Thresholds for "actionable" — matches the page's 🟡 Moderate tier minimum
MIN_COMPOSITE = 80
MIN_CONSENSUS = 60
MIN_XBOOK = 2.0


def to_settler_pick(p):
    """Reshape a fundamentals pick into the settler's expected schema."""
    return {
        # Settle-required
        "stat_key":    p["stat_key"],
        "side":        p["side"],
        "point":       p["point"],
        "away_team":   p["away_team"],
        "home_team":   p["home_team"],
        "first_pitch": p["first_pitch"],
        "best_price":  p["best_price"],
        "player":      p["player"],
        "batter_id":   p.get("batter_id"),
        # Display fields
        "selection":   f'{p["name"]} {p["market"]} {p["side"]} {p.get("point","")}'.strip(),
        "market":      p["market"],
        "game":        p["matchup"],
        # Framework context
        "composite":      p["score"],
        "xwoba":          p.get("xwoba"),
        "hard_hit":       p.get("hard_hit"),
        "barrel":         p.get("barrel"),
        "consensus_pct":  p["consensus_pct"],
        "cross_book_pp":  p["cross_book_pp"],
        "edge_pp":        p["edge_pp"],
        "best_book":      p["best_book"],
        "opp_pitcher":    p["opp_pitcher"],
    }


def main():
    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
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
    out_dir = os.path.join(ROOT, "fundamentals_history")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{today_et}.json")

    print(f"Running Fundamentals scan for {today_et}...")
    result = scan(api_key)
    print(f"  Elite batters facing today: {result['n_elite_batters']}")
    print(f"  Prop offers analyzed: {result['n_plays']}")

    # Filter to actionable + dedupe (one pick per batter+market+side+point)
    actionable = [p for p in result["plays"]
                  if p["score"] >= MIN_COMPOSITE
                  and p["consensus_pct"] >= MIN_CONSENSUS
                  and p["cross_book_pp"] >= MIN_XBOOK
                  and p.get("stat_key") is not None
                  and p.get("away_team") is not None]
    # Dedupe — best play per batter+market+side
    seen = set()
    unique = []
    for p in actionable:
        key = (p["name"], p["market"].split("*")[0], p["side"], p["point"])
        if key in seen: continue
        seen.add(key)
        unique.append(p)

    # Sort by combined ranking score
    unique.sort(key=lambda x: -(x["score"]/100 * x["consensus_pct"]
                                 * (1 + x["cross_book_pp"]/10)))

    # Cap at top 20 to avoid bloat
    final = unique[:20]
    print(f"  Actionable (composite>={MIN_COMPOSITE}, cons>={MIN_CONSENSUS}%, "
          f"X-book>={MIN_XBOOK}pp): {len(final)} picks")

    picks = [to_settler_pick(p) for p in final]

    payload = {
        "date":        today_et,
        "snapshot_at": datetime.now(tz=EASTERN).isoformat(),
        "n_picks":     len(picks),
        "filter": {
            "min_composite":     MIN_COMPOSITE,
            "min_consensus_pct": MIN_CONSENSUS,
            "min_cross_book_pp": MIN_XBOOK,
            "top_n":             20,
        },
        "scanner_stats": {
            "n_games":          result["n_games"],
            "n_elite_batters":  result["n_elite_batters"],
            "n_offers":         result["n_plays"],
        },
        "picks": picks,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"  Saved -> {out_path}")


if __name__ == "__main__":
    main()
