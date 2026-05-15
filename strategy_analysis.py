"""
Profitable strategy analysis — singles and parlays.
Simulates all major betting approaches against 16-day backtest data.
"""
import json, glob, os, time, urllib.request
from collections import defaultdict
from datetime import date, timedelta
from itertools import combinations

def _calibrate(p):
    return p * 0.75 if 0.15 <= p < 0.40 else p

def _get(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mlb-bt/1.0"})
        with urllib.request.urlopen(req, timeout=20, context=_UNVERIFIED_SSL) as r:
            return json.loads(r.read().decode())
    except:
        return {}

def get_hrs(game_pk):
    data = _get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore")
    result = {}
    for side in ("away", "home"):
        for _, pd in data.get("teams", {}).get(side, {}).get("players", {}).items():
            pid = pd.get("person", {}).get("id")
            hr  = pd.get("stats", {}).get("batting", {}).get("homeRuns", 0)
            if pid:
                result[pid] = hr
    return result

# ── Load predictions ─────────────────────────────────────────────────────────
cutoff = date.today() - timedelta(days=15)
rows = []
for path in sorted(glob.glob("predictions/2026-*.jsonl")):
    d = os.path.basename(path).replace(".jsonl", "")
    try:
        if date.fromisoformat(d) < cutoff:
            continue
    except:
        continue
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if not all(k in r for k in ("date", "batter_id", "p_at_least_one")):
                continue
            r["p_at_least_one"] = _calibrate(r["p_at_least_one"])
            rows.append(r)

print(f"Loaded {len(rows)} predictions. Fetching boxscores...")
cache = {}
unique_games = {p["game_pk"] for p in rows if p.get("game_pk")}
for i, gk in enumerate(sorted(unique_games)):
    cache[gk] = get_hrs(int(gk))
    if (i + 1) % 30 == 0:
        print(f"  {i+1}/{len(unique_games)}...")
    time.sleep(0.12)
print("Done.\n")

resolved = []
for p in rows:
    gk, bid = p.get("game_pk"), p.get("batter_id")
    if not gk or not bid:
        continue
    hr_map = cache.get(gk, {})
    if not hr_map:
        continue
    name = p.get("batter_name", "?")
    if "," in name:
        last, _, first = name.partition(",")
        name = f"{first.strip()} {last.strip()}"
    resolved.append({
        **p,
        "hit": 1 if hr_map.get(int(bid), 0) >= 1 else 0,
        "mp":  p["p_at_least_one"],
        "name": name,
    })

by_date = defaultdict(list)
for r in resolved:
    by_date[r["date"]].append(r)
dates = sorted(by_date.keys())


def market_dec(model_p, vig=0.07):
    """Fair decimal odds discounted by vig. Caps at market-realistic level."""
    mp = min(model_p, 0.60)
    return 1.0 / (mp * (1.0 + vig))


# ── SINGLE BET STRATEGIES ────────────────────────────────────────────────────
def flat_single(picks_per_day, threshold=0.0):
    w = ret = bets = wins = 0
    for d in dates:
        top = [r for r in sorted(by_date[d], key=lambda x: -x["mp"])
               if r["mp"] >= threshold][:picks_per_day]
        for r in top:
            dec = market_dec(r["mp"])
            w += 100
            ret += 100 * dec if r["hit"] else 0
            bets += 1
            if r["hit"]:
                wins += 1
    roi = (ret - w) / w * 100 if w else 0
    return bets, wins, roi, w, ret


print("=" * 72)
print("  SINGLE-BET STRATEGIES  (flat $100/bet, ~7% vig baked in)")
print("=" * 72)
print(f"  {'Strategy':<34} {'Bets':>5} {'Wins':>5} {'Hit%':>7} {'Wagered':>9} {'Return':>9} {'ROI':>8}")
print(f"  {'-'*75}")

singles = [
    ("Top 1 daily (no filter)",      1, 0.00),
    ("Top 2 daily (no filter)",      2, 0.00),
    ("Top 3 daily (no filter)",      3, 0.00),
    ("Top 5 daily (no filter)",      5, 0.00),
    ("Top 1 daily  model >= 30%",    1, 0.30),
    ("Top 2 daily  model >= 30%",    2, 0.30),
    ("Top 3 daily  model >= 30%",    3, 0.30),
    ("Top 1 daily  model >= 40%",    1, 0.40),
    ("Top 2 daily  model >= 40%",    2, 0.40),
    ("Top 3 daily  model >= 40%",    3, 0.40),
    ("All picks    model >= 40%",   99, 0.40),
    ("All picks    model >= 35%",   99, 0.35),
    ("All picks    model >= 30%",   99, 0.30),
    ("All picks    model >= 25%",   99, 0.25),
]
for label, n, thresh in singles:
    b, w2, roi, wag, ret = flat_single(n, thresh)
    hr = w2 / b * 100 if b else 0
    print(f"  {label:<34} {b:>5} {w2:>5} {hr:>6.1f}%  ${wag:>7,.0f}  ${ret:>7,.0f}  {roi:>+7.1f}%")


# ── PARLAY STRATEGIES ────────────────────────────────────────────────────────
def run_parlay(legs, threshold=0.0, top_n=99):
    w = ret = parlays = wins = 0
    for d in dates:
        pool = [r for r in sorted(by_date[d], key=lambda x: -x["mp"])
                if r["mp"] >= threshold][:top_n]
        if len(pool) < legs:
            continue
        combo = pool[:legs]
        dec = 1.0
        for r in combo:
            dec *= market_dec(r["mp"])
        all_hit = all(r["hit"] for r in combo)
        w += 100
        ret += 100 * dec if all_hit else 0
        parlays += 1
        if all_hit:
            wins += 1
    roi = (ret - w) / w * 100 if w else 0
    return parlays, wins, roi, w, ret


print()
print("=" * 72)
print("  PARLAY STRATEGIES  (flat $100/parlay, top picks combined, ~7% vig)")
print("=" * 72)
print(f"  {'Strategy':<34} {'Days':>5} {'Wins':>5} {'Hit%':>7} {'Wagered':>9} {'Return':>9} {'ROI':>8}")
print(f"  {'-'*75}")

parlays = [
    ("2-leg: top 2 daily",          2, 0.00, 2),
    ("2-leg: top 2  model >= 30%",  2, 0.30, 2),
    ("2-leg: top 2  model >= 40%",  2, 0.40, 2),
    ("3-leg: top 3 daily",          3, 0.00, 3),
    ("3-leg: top 3  model >= 30%",  3, 0.30, 3),
    ("3-leg: top 3  model >= 40%",  3, 0.40, 3),
    ("4-leg: top 4 daily",          4, 0.00, 4),
    ("4-leg: top 4  model >= 30%",  4, 0.30, 4),
    ("5-leg: top 5 daily",          5, 0.00, 5),
    ("5-leg: top 5  model >= 30%",  5, 0.30, 5),
]
for label, legs, thresh, top in parlays:
    p, w2, roi, wag, ret = run_parlay(legs, thresh, top)
    hr = w2 / p * 100 if p else 0
    print(f"  {label:<34} {p:>5} {w2:>5} {hr:>6.1f}%  ${wag:>7,.0f}  ${ret:>7,.0f}  {roi:>+7.1f}%")


# ── ROUND ROBIN STRATEGIES ───────────────────────────────────────────────────
def run_rr(legs, pool_size, threshold=0.0):
    w = ret = total_parlays = wins = days_played = 0
    for d in dates:
        pool = [r for r in sorted(by_date[d], key=lambda x: -x["mp"])
                if r["mp"] >= threshold][:pool_size]
        if len(pool) < legs:
            continue
        days_played += 1
        for combo in combinations(pool, legs):
            dec = 1.0
            for r in combo:
                dec *= market_dec(r["mp"])
            all_hit = all(r["hit"] for r in combo)
            w += 100
            ret += 100 * dec if all_hit else 0
            total_parlays += 1
            if all_hit:
                wins += 1
    roi = (ret - w) / w * 100 if w else 0
    return total_parlays, wins, roi, w, ret, days_played


print()
print("=" * 72)
print("  ROUND ROBIN  (all combos per day, $100/combo, ~7% vig)")
print("=" * 72)
print(f"  {'Strategy':<38} {'Days':>5} {'Combos':>7} {'Wins':>5} {'Wagered':>9} {'Return':>9} {'ROI':>8}")
print(f"  {'-'*78}")

rr_strats = [
    ("RR 2-of-3  top 3 daily",        2, 3, 0.00),
    ("RR 2-of-3  top 3  >= 30%",      2, 3, 0.30),
    ("RR 2-of-3  top 3  >= 40%",      2, 3, 0.40),
    ("RR 2-of-4  top 4 daily",        2, 4, 0.00),
    ("RR 2-of-5  top 5 daily",        2, 5, 0.00),
    ("RR 3-of-4  top 4 daily",        3, 4, 0.00),
    ("RR 3-of-4  top 4  >= 30%",      3, 4, 0.30),
    ("RR 3-of-5  top 5 daily",        3, 5, 0.00),
    ("RR 3-of-5  top 5  >= 30%",      3, 5, 0.30),
    ("RR 4-of-5  top 5 daily",        4, 5, 0.00),
]
for label, legs, pool, thresh in rr_strats:
    p, w2, roi, wag, ret, dplayed = run_rr(legs, pool, thresh)
    hr = w2 / p * 100 if p else 0
    print(f"  {label:<38} {dplayed:>5} {p:>7} {w2:>5} {hr:>6.1f}%  ${wag:>7,.0f}  ${ret:>7,.0f}  {roi:>+7.1f}%")


# ── BEST STRATEGY DEEP DIVE ──────────────────────────────────────────────────
print()
print("=" * 72)
print("  COMBINED STRATEGY: Single + Parlay hedge")
print("  $50 flat single on #1 pick  +  $50 2-leg parlay (#1 + #2)")
print("=" * 72)
hedge_w = hedge_ret = hedge_bets = 0
for d in dates:
    top = sorted(by_date[d], key=lambda x: -x["mp"])[:2]
    if len(top) < 2:
        continue
    r1, r2 = top[0], top[1]
    # Single on #1
    hedge_w += 50
    hedge_ret += 50 * market_dec(r1["mp"]) if r1["hit"] else 0
    # 2-leg parlay #1 + #2
    dec_parlay = market_dec(r1["mp"]) * market_dec(r2["mp"])
    hedge_w += 50
    hedge_ret += 50 * dec_parlay if (r1["hit"] and r2["hit"]) else 0
    hedge_bets += 1

hedge_roi = (hedge_ret - hedge_w) / hedge_w * 100 if hedge_w else 0
print(f"  Days played  : {hedge_bets}")
print(f"  Total wagered: ${hedge_w:,.0f}  (${hedge_w/hedge_bets:.0f}/day)")
print(f"  Total return : ${hedge_ret:,.0f}")
print(f"  ROI          : {hedge_roi:+.1f}%")
print()
