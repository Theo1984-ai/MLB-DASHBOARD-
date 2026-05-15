"""
5-leg max parlay builder from today's value_bets output.
Rules: 1 leg per game, half-point lines only (no push risk),
       known starters only, optimize by Kelly growth.
"""
import math
from itertools import combinations

# ── Today's positive-edge plays (from value_bets.py run, known starters only)
# book_imp = model_p - edge_pp/100  (back-calculated from calibration)
# Half-point lines only (eliminates push)

def book_dec(model_p, edge_pp):
    """Estimated decimal odds the book is offering for this alt line."""
    bk_imp = model_p - edge_pp / 100.0
    bk_imp = max(0.50, min(bk_imp, 0.98))
    return 1.0 / bk_imp

def am(dec):
    if dec >= 2.0:
        return int((dec - 1) * 100)
    return int(-100 / (dec - 1))

# Best half-point tier per game — pick the one with highest edge
# (game, label, model_p, edge_pp)
CANDIDATE_GAMES = {
    'SEA@HOU': [
        ('SEA@HOU U12.5',  0.874, 13.8),   # tier 1: highest edge
        ('SEA@HOU U11.5',  0.801, 12.7),   # tier 2: lower prob but bigger payout
        ('SEA@HOU U10.5',  0.704, 11.1),   # tier 3
    ],
    'MIA@MIN': [
        ('MIA@MIN U12.5',  0.874, 13.7),
        ('MIA@MIN U11.5',  0.801, 12.6),
        ('MIA@MIN U10.5',  0.703, 11.0),
    ],
    'PHI@BOS': [
        ('PHI@BOS U12.5',  0.880, 13.5),
        ('PHI@BOS U11.5',  0.809, 12.4),
        ('PHI@BOS U10.5',  0.713, 10.9),
    ],
    'AZ@TEX': [
        ('AZ@TEX U12.5',   0.879, 13.3),
        ('AZ@TEX U11.5',   0.807, 12.2),
        ('AZ@TEX U10.5',   0.711, 10.8),
    ],
    'CHC@ATL': [
        ('CHC@ATL U11.5',  0.880, 13.0),
        ('CHC@ATL U10.5',  0.806, 11.9),
        ('CHC@ATL U9.5',   0.704, 10.4),
    ],
    'COL@PIT': [
        ('COL@PIT U11.5',  0.890, 12.3),
        ('COL@PIT U10.5',  0.819, 11.4),
        ('COL@PIT U9.5',   0.721, 10.0),
    ],
    'TB@TOR': [
        ('TB@TOR U11.5',   0.853,  4.3),
        ('TB@TOR U10.5',   0.769,  3.9),
    ],
    'DET@NYM': [
        ('DET@NYM U11.5',  0.852,  4.0),
        ('DET@NYM U10.5',  0.767,  3.6),
    ],
    'SD@MIL': [
        ('SD@MIL U10.5',   0.866,  2.7),
        ('SD@MIL U9.5',    0.781,  2.4),
    ],
    'SF@LAD': [
        ('SF@LAD U10.5',   0.864,  2.1),
        ('SF@LAD U9.5',    0.778,  1.9),
    ],
}

# Build flat list — best tier per game (top edge)
PLAYS = []
for game, tiers in CANDIDATE_GAMES.items():
    best = max(tiers, key=lambda x: x[1])   # highest edge
    label, model_p, edge_pp = best
    dec = book_dec(model_p, edge_pp)
    PLAYS.append({
        'game':    game,
        'label':   label,
        'model_p': model_p,
        'edge_pp': edge_pp,
        'dec':     dec,
        'am':      am(dec),
    })

PLAYS.sort(key=lambda x: -x['edge_pp'])

print()
print("=" * 90)
print("  TODAY'S VALUE PLAYS — ALT UNDER (known starters, half-point lines)")
print("  Edge = Model% minus calibrated book implied%")
print("=" * 90)
print(f"  {'Game':<13} {'Bet':<18} {'Model%':>7} {'Est.Odds':>9} {'Edge':>8}")
print(f"  {'-'*60}")
for p in PLAYS:
    print(f"  {p['game']:<13} {p['label']:<18} {p['model_p']*100:>6.1f}% "
          f"{p['am']:>+9d} {p['edge_pp']:>+7.1f}pp")

# ── Parlay optimizer: all combos 2–5 legs, 1 leg per game ────────────────────
def kelly_growth(p, dec):
    b = dec - 1
    if b <= 0: return -999
    k = max(0.0, (b * p - (1 - p)) / b)
    k = min(k, 1.0)
    gr = p * math.log(1 + k * b) + (1 - p) * math.log(max(1e-12, 1 - k))
    return gr

def parlay(legs):
    p   = 1.0
    dec = 1.0
    for leg in legs:
        p   *= leg['model_p']
        dec *= leg['dec']
    b      = dec - 1
    k      = max(0.0, (b * p - (1 - p)) / b)
    k      = min(k, 1.0)
    ev100  = p * b * 100 - (1 - p) * 100
    gr     = p * math.log(1 + k * b) + (1 - p) * math.log(max(1e-12, 1 - k))
    return {'p': p, 'dec': dec, 'am_odds': am(dec), 'ev100': ev100,
            'kelly': k, 'growth': gr}

print()
print("=" * 90)
print("  OPTIMAL PARLAY FINDER  (2-5 legs, 1 per game, ranked by Kelly growth)")
print("=" * 90)

best_combos = []
for n in range(2, 6):
    for combo in combinations(PLAYS, n):
        # Ensure 1 leg per game (already guaranteed since PLAYS has 1 per game)
        s = parlay(list(combo))
        best_combos.append((s['growth'], list(combo), s))

best_combos.sort(key=lambda x: -x[0])

print()
print("  Top 10 combos by Kelly growth rate:")
print()
for rank, (gr, legs, s) in enumerate(best_combos[:10], 1):
    print(f"  #{rank:2d}  {len(legs)}-leg  "
          f"Win:{s['p']*100:.1f}%  Payout:{s['am_odds']:+d}  "
          f"EV:${s['ev100']:+.0f}/$100  Kelly:{s['kelly']*100:.1f}%  "
          f"Growth:{s['growth']*100:.2f}%")
    for leg in legs:
        print(f"        {leg['label']:<22} model {leg['model_p']*100:.1f}%  "
              f"est. {leg['am']:+d}  edge +{leg['edge_pp']:.1f}pp")
    print()

# ── THE PLAY ──────────────────────────────────────────────────────────────────
best = best_combos[0]
legs, s = best[1], best[2]

print("=" * 90)
print("  THE PLAY")
print("=" * 90)
print()
print(f"  {len(legs)}-Leg Alt Under Parlay")
print()
for i, leg in enumerate(legs, 1):
    print(f"  {i}. {leg['label']:<22}  model {leg['model_p']*100:.1f}%  "
          f"edge +{leg['edge_pp']:.1f}pp  est. book {leg['am']:+d}")
print()
print(f"  Combined win probability : {s['p']*100:.1f}%")
print(f"  Est. payout              : {s['am_odds']:+d}")
print(f"  EV per $100 wagered      : ${s['ev100']:+.0f}")
print(f"  Kelly fraction           : {s['kelly']*100:.1f}%")
print(f"  Kelly growth rate        : {s['growth']*100:.2f}% per bet")
print()
for stake in [25, 50, 100]:
    win = stake * (s['dec'] - 1)
    print(f"  ${stake:3d} bet  -->  wins ${win:.0f}  (total ${stake + win:.0f})")
print()
print("  NOTE: 'Est. book' odds are the estimated prices based on calibration from")
print("        the standard line. Open your sportsbook, find the alt line for each")
print("        game and verify the actual price is at or LESS negative than shown.")
print("        If the book charges MORE (e.g. -500 vs est. -280), skip that leg.")
print("=" * 90)
