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
    """Reshape a fundamentals pick into the settler's expected schema.
    Works for batter props (xwoba/hard_hit/barrel) AND pitcher K props
    (xwoba_against/k_per_9). Fields differ between the two — use .get()."""
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
        "pitcher_id":  p.get("pitcher_id"),
        # Display fields
        "selection":   f'{p["name"]} {p["market"]} {p["side"]} {p.get("point","")}'.strip(),
        "market":      p["market"],
        "game":        p["matchup"],
        # Framework context (some only relevant for batters, others for pitchers)
        "composite":       p["score"],
        "xwoba":           p.get("xwoba"),
        "hard_hit":        p.get("hard_hit"),
        "barrel":          p.get("barrel"),
        "xwoba_against":   p.get("xwoba_against"),
        "k_per_9":         p.get("k_per_9"),
        "consensus_pct":   p["consensus_pct"],
        "cross_book_pp":   p["cross_book_pp"],
        "edge_pp":         p["edge_pp"],
        "best_book":       p["best_book"],
        "opp_pitcher":     p.get("opp_pitcher"),  # only batter picks have this
        "opp_team":        p.get("opp_team"),     # only pitcher picks have this
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
    print(f"  Elite batters: {result['n_elite_batters']}, pitchers: {result['n_elite_pitchers']}")
    print(f"  Offers analyzed: {result['n_plays']} hitter / {result['n_pitcher_plays']} pitcher K")

    # ---- 1) HITTER plays (Hits / TB / RBIs / Runs) ----
    hitter_actionable = [p for p in result["hitter_plays"]
                         if p["score"] >= MIN_COMPOSITE
                         and p["consensus_pct"] >= MIN_CONSENSUS
                         and p["cross_book_pp"] >= MIN_XBOOK
                         and p.get("stat_key") is not None
                         and p.get("away_team") is not None]
    seen = set()
    hitter_unique = []
    for p in hitter_actionable:
        key = (p["name"], p["market"].split("*")[0], p["side"], p["point"])
        if key in seen: continue
        seen.add(key)
        hitter_unique.append(p)
    hitter_unique.sort(key=lambda x: -(x["score"]/100 * x["consensus_pct"]
                                       * (1 + x["cross_book_pp"]/10)))
    hitter_final = hitter_unique[:15]

    # ---- 2) PITCHER K plays (relaxed consensus — K's often >55%) ----
    MIN_K_CONS = 55
    pitcher_actionable = [p for p in result["pitcher_plays"]
                          if p["score"] >= 65
                          and p["consensus_pct"] >= MIN_K_CONS
                          and p["cross_book_pp"] >= 1.5
                          and p.get("away_team") is not None]
    seen_p = set()
    pitcher_unique = []
    for p in pitcher_actionable:
        key = (p["name"], p["market"].split("*")[0], p["side"], p["point"])
        if key in seen_p: continue
        seen_p.add(key)
        pitcher_unique.append(p)
    pitcher_unique.sort(key=lambda x: -(x["score"]/100 * x["consensus_pct"]
                                        * (1 + x["cross_book_pp"]/10)))
    pitcher_final = pitcher_unique[:10]

    # ---- 3) HR longshots (Barrel-driven, low consensus expected) ----
    MIN_HR_CONS = 10
    hr_actionable = [p for p in result["hr_plays"]
                     if p["score"] >= 85          # elite Barrel/xSLG only
                     and p["consensus_pct"] >= MIN_HR_CONS
                     and p["cross_book_pp"] >= 1.0
                     and p.get("away_team") is not None
                     and p["side"] in ("Yes", "Over")]
    seen_hr = set()
    hr_unique = []
    for p in hr_actionable:
        key = (p["name"], p["point"])
        if key in seen_hr: continue
        seen_hr.add(key)
        hr_unique.append(p)
    hr_unique.sort(key=lambda x: -(x["score"] * x["consensus_pct"] / 100))
    hr_final = hr_unique[:8]

    print(f"  Actionable: {len(hitter_final)} hitter + {len(pitcher_final)} pitcher K + {len(hr_final)} HR longshot")

    picks = ([to_settler_pick(p) for p in hitter_final]
             + [to_settler_pick(p) for p in pitcher_final]
             + [to_settler_pick(p) for p in hr_final])

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
