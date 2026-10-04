"""
NHL Sharp Money daily snapshot — mirrors sharp_money_snapshot.py exactly.

Uses nhl_polymarket_sharp.scan() + sportsbook_matcher.match_signals(sport=nhl).
Saves actionable cross-venue plays to nhl_sharp_money_history/YYYY-MM-DD.json
with full persistence tracking (n_appearances, skew_history).
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.nhl_polymarket_sharp import scan as pm_scan    # noqa: E402
from scripts.sportsbook_matcher import match_signals        # noqa: E402

EASTERN = ZoneInfo("America/New_York")
SPORT = "icehockey_nhl"

MIN_EDGE_PP   = 3.0
MIN_SKEW      = 60.0
MIN_LIQUIDITY = 5000   # lower than MLB — NHL Polymarket markets are thinner


def to_settler_pick(p):
    ys = p.get("yes_bid_depth", 0) or 0
    ns = p.get("no_bid_depth", 0) or 0
    sharp_d = ys if p.get("skew_side") == "YES" else ns
    other_d = ns if p.get("skew_side") == "YES" else ys
    depth_ratio = round(sharp_d / other_d, 2) if other_d > 0 else None
    return {
        "stat_key":    p.get("bet_stat_key"),
        "side":        p.get("bet_side"),
        "point":       p.get("bet_point"),
        "away_team":   p.get("sb_away_team"),
        "home_team":   p.get("sb_home_team"),
        "first_pitch": p.get("first_pitch"),
        "best_price":  p.get("best_price"),
        "player":      None,
        "selection":   p.get("play", ""),
        "market":      p.get("category", ""),
        "sharp_pick":  p.get("sharp_pick", ""),
        "game":        p.get("event", ""),
        "question":    p.get("question", ""),
        "edge_pp":                p.get("edge_pp"),
        "edge_best_pp":           p.get("edge_best_pp"),
        "skew_strength":          p.get("skew_strength"),
        "skew_side":              p.get("skew_side"),
        "liquidity":              p.get("liquidity"),
        "volume":                 p.get("volume"),
        "yes_bid_depth":          ys,
        "no_bid_depth":           ns,
        "depth_ratio":            depth_ratio,
        "pm_mid":                 p.get("mid"),
        "pm_implied_pct":         (p.get("mid", 0) * 100 if p.get("skew_side") == "YES"
                                   else (1 - p.get("mid", 0)) * 100),
        "sb_book":                p.get("sb_book"),
        "sb_implied_pct":         p.get("sb_implied_pct"),
        "sb_consensus_implied_pct": p.get("sb_consensus_implied_pct"),
        "sb_n_books":             p.get("sb_n_books"),
        "sharp_n_bids":           p.get("sharp_n_bids"),
        "sharp_largest_bid":      p.get("sharp_largest_bid"),
        "sharp_whale_share":      p.get("sharp_whale_share"),
    }


def _play_key(pick):
    return (pick.get("game", ""), pick.get("market", ""),
            pick.get("side", ""), pick.get("point"))


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
    out_dir  = os.path.join(ROOT, "nhl_sharp_money_history")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{today_et}.json")

    print(f"Running NHL Sharp Money scan for {today_et}...")
    pm_rows, _debug = pm_scan(top_n=50)
    print(f"  Polymarket NHL scan: {len(pm_rows)} markets")

    matched = match_signals(pm_rows, api_key, sport=SPORT)
    print(f"  Sportsbook matched: {sum(1 for m in matched if m.get('sb_best_price') is not None)} of {len(matched)}")

    actionable = [m for m in matched
                  if m.get("edge_pp") is not None and m["edge_pp"] >= MIN_EDGE_PP
                  and m.get("skew_strength", 0) >= MIN_SKEW
                  and (m.get("liquidity") or 0) >= MIN_LIQUIDITY
                  and m.get("sb_best_price") is not None
                  and m.get("bet_stat_key") is not None]
    actionable.sort(key=lambda r: -r["edge_pp"])
    print(f"  Actionable (edge>={MIN_EDGE_PP}pp, skew>={MIN_SKEW}%, liq>=${MIN_LIQUIDITY}): {len(actionable)}")

    new_picks = [to_settler_pick(p) for p in actionable]
    now_iso = datetime.now(tz=EASTERN).isoformat()
    for p in new_picks:
        p["captured_at"] = now_iso

    # Race-safe git pull before reading existing file
    if os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            import subprocess as _sp
            _sp.run(["git", "pull", "--rebase", "-X", "theirs", "origin", "main"],
                    cwd=ROOT, capture_output=True, text=True, timeout=60)
        except Exception:
            pass

    existing_picks = []
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                prev = json.load(f)
            existing_picks = prev.get("picks", []) or []
        except Exception:
            pass

    by_key = {_play_key(p): p for p in existing_picks}
    added = new_appearances = 0
    for p in new_picks:
        k = _play_key(p)
        history_entry = {"t": now_iso, "skew": p.get("skew_strength"),
                         "mid": p.get("pm_mid"), "edge": p.get("edge_pp"),
                         "ratio": p.get("depth_ratio")}
        if k not in by_key:
            p["n_appearances"] = 1
            p["first_seen_at"] = now_iso
            p["last_seen_at"]  = now_iso
            p["skew_history"]  = [history_entry]
            p["captured_at"]   = now_iso
            by_key[k] = p
            added += 1
        else:
            prev_pick = by_key[k]
            prev_pick["n_appearances"] = (prev_pick.get("n_appearances") or 1) + 1
            prev_pick["last_seen_at"]  = now_iso
            prev_pick.setdefault("first_seen_at", prev_pick.get("captured_at") or now_iso)
            hist = prev_pick.get("skew_history") or []
            hist.append(history_entry)
            prev_pick["skew_history"] = hist
            for field in ("skew_strength", "yes_bid_depth", "no_bid_depth",
                          "depth_ratio", "pm_mid", "pm_implied_pct",
                          "edge_pp", "edge_best_pp", "liquidity", "volume",
                          "sb_best_price", "sb_book", "sb_implied_pct",
                          "sb_consensus_implied_pct", "sb_n_books"):
                if p.get(field) is not None:
                    prev_pick[field] = p[field]
            new_appearances += 1

    merged = list(by_key.values())
    merged.sort(key=lambda x: (-min(x.get("n_appearances") or 1, 5), -(x.get("edge_pp") or 0)))
    print(f"  Merged: {added} new + {new_appearances} re-appearances "
          f"(had {len(existing_picks)} previously, total {len(merged)})")

    payload = {
        "date":        today_et,
        "snapshot_at": now_iso,
        "n_picks":     len(merged),
        "filter": {"min_edge_pp": MIN_EDGE_PP, "min_skew_pct": MIN_SKEW,
                   "min_liquidity": MIN_LIQUIDITY},
        "picks": merged,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"  Saved -> {out_path}")


if __name__ == "__main__":
    main()
