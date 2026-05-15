"""
BLEND BACKTEST  —  Pure Model  vs  Devigged Market  vs  Blended
════════════════════════════════════════════════════════════════
Tests whether blending Poisson model probability with devigged market
probability improves edge calibration and ROI compared to using the
pure model alone.

Devigging removes the sportsbook's vig to get their TRUE implied
probability, then we blend:
    blended_p = alpha * model_p  +  (1 - alpha) * fair_market_p

Edge (what drives bet selection):
    edge = blended_p  -  vigged_market_implied

Alpha=1.0  →  pure model (baseline, same as backtest_rr.py)
Alpha=0.0  →  pure market (devigged fair line minus vig → always negative, no bets)

Reuses cached odds from  cache/bt_rr_odds.json  (no new API calls).

Usage:
    python backtest_blend.py
    python backtest_blend.py --mkt ML       (one market only)
    python backtest_blend.py --min-edge 5   (change minimum edge threshold)
"""

import sys, os, json, math
from collections import defaultdict

CACHE_FILE = 'cache/bt_rr_odds.json'
FLAT_BET   = 100
LEAGUE_ERA = 4.30

# Alpha values to test: 1.0 = pure model, lower = more market weight
ALPHAS = [1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.2]

# ── 2025 team baselines ───────────────────────────────────────────────────────
rpg25 = {
    'WSH':4.24,'CIN':4.42,'NYY':5.24,'BAL':4.18,'PHI':4.80,'BOS':4.85,
    'COL':3.69,'PIT':3.60,'TB':4.41,'TOR':4.93,'DET':4.68,'NYM':4.73,
    'CHC':4.89,'ATL':4.47,'KC':4.02,'CWS':3.99,'MIA':4.38,'MIN':4.18,
    'SD':4.33,'MIL':4.97,'AZ':4.88,'TEX':4.22,'SEA':4.73,'HOU':4.24,
    'STL':4.25,'ATH':4.53,'SF':4.35,'LAD':5.09,'LAA':4.15,'CLE':3.97,
}
rapg25 = {
    'WSH':5.22,'CIN':3.80,'NYY':3.86,'BAL':4.53,'PHI':3.75,'BOS':3.68,
    'COL':5.76,'PIT':3.69,'TB':3.87,'TOR':4.13,'DET':3.89,'NYM':3.96,
    'CHC':3.73,'ATL':4.30,'KC':3.68,'CWS':4.14,'MIA':4.55,'MIN':4.45,
    'SD':3.57,'MIL':3.54,'AZ':4.45,'TEX':3.44,'SEA':3.88,'HOU':3.82,
    'STL':4.21,'ATH':4.63,'SF':3.76,'LAD':3.91,'LAA':4.80,'CLE':3.66,
}
FULL_TO_ABBR = {
    'Los Angeles Angels':'LAA','Cleveland Guardians':'CLE',
    'New York Yankees':'NYY','Baltimore Orioles':'BAL',
    'Washington Nationals':'WSH','Cincinnati Reds':'CIN',
    'Colorado Rockies':'COL','Pittsburgh Pirates':'PIT',
    'Philadelphia Phillies':'PHI','Boston Red Sox':'BOS',
    'Tampa Bay Rays':'TB','Toronto Blue Jays':'TOR',
    'Detroit Tigers':'DET','New York Mets':'NYM',
    'Chicago Cubs':'CHC','Atlanta Braves':'ATL',
    'Kansas City Royals':'KC','Chicago White Sox':'CWS',
    'Miami Marlins':'MIA','Minnesota Twins':'MIN',
    'San Diego Padres':'SD','Milwaukee Brewers':'MIL',
    'Arizona Diamondbacks':'AZ','Texas Rangers':'TEX',
    'Seattle Mariners':'SEA','Houston Astros':'HOU',
    'St. Louis Cardinals':'STL','Athletics':'ATH',
    'San Francisco Giants':'SF','Los Angeles Dodgers':'LAD',
}

# ── Math helpers ──────────────────────────────────────────────────────────────
def pmf(k, lam):
    if k < 0 or lam <= 0: return 0.0
    return (lam**k * math.exp(-lam)) / math.factorial(k)

def exp_runs(off, def_r, sp_era):
    return max(1.5, (off + def_r) / 2.0 + (sp_era - LEAGUE_ERA) * 0.22)

def p_win_raw(lam_a, lam_b, max_runs=20):
    p = 0.0
    for a in range(max_runs):
        pa = pmf(a, lam_a)
        for b in range(a):
            p += pa * pmf(b, lam_b)
    return p

def p_tie(lam_a, lam_b, max_runs=20):
    return sum(pmf(k, lam_a) * pmf(k, lam_b) for k in range(max_runs))

def p_ml_win(lam_a, lam_b, side='away'):
    raw_a = p_win_raw(lam_a, lam_b)
    tie   = p_tie(lam_a, lam_b)
    raw_b = 1 - raw_a - tie
    if tie < 0.9999:
        adj_a = raw_a / (1 - tie)
        adj_b = raw_b / (1 - tie)
    else:
        adj_a = adj_b = 0.5
    return adj_a if side == 'away' else adj_b

def p_rl_side(lam_a, lam_b, pt, side):
    margin = abs(pt)
    if side == 'away':
        if pt < 0:
            return sum(pmf(a, lam_a) * sum(pmf(b, lam_b)
                       for b in range(max(0, a - int(margin))))
                       for a in range(25))
        else:
            return sum(pmf(a, lam_a) * sum(pmf(b, lam_b)
                       for b in range(min(25, a + int(margin) + 1)))
                       for a in range(25))
    else:
        if pt < 0:
            return sum(pmf(b, lam_b) * sum(pmf(a, lam_a)
                       for a in range(max(0, b - int(margin))))
                       for b in range(25))
        else:
            return sum(pmf(b, lam_b) * sum(pmf(a, lam_a)
                       for a in range(min(25, b + int(margin) + 1)))
                       for b in range(25))

def p_total_under(lam_total, line):
    return sum(pmf(k, lam_total) for k in range(int(line) + 1))

def p_total_over(lam_total, line):
    return 1.0 - p_total_under(lam_total, line) + pmf(int(line), lam_total)

def implied(odds):
    if odds == 0: return 0.5
    return abs(odds)/(abs(odds)+100.0) if odds < 0 else 100.0/(odds+100.0)

def am_to_dec(odds):
    return 1.0 + 100.0/abs(odds) if odds < 0 else 1.0 + odds/100.0

def devig(imp_a, imp_b):
    """Remove vig from two-sided market. Returns (fair_a, fair_b)."""
    total = imp_a + imp_b
    if total <= 0: return 0.5, 0.5
    return imp_a / total, imp_b / total


# ── Build all bets for every alpha ────────────────────────────────────────────
def build_bets(games, pit_stats, all_dk, only_mkt=None):
    """
    Returns dict: {alpha -> [bet_dict, ...]}
    Each bet_dict has model_p, fair_p, blended_p, vigged_imp, edge, won, pnl.
    """
    bets_by_alpha = {a: [] for a in ALPHAS}

    for g in games:
        away = g['away']; home = g['home']
        if away not in rpg25 or home not in rpg25:
            continue

        apid = g.get('away_pit_id'); hpid = g.get('home_pit_id')
        away_era = pit_stats.get(apid, LEAGUE_ERA) if apid else LEAGUE_ERA
        home_era = pit_stats.get(hpid, LEAGUE_ERA) if hpid else LEAGUE_ERA

        lam_a = exp_runs(rpg25[away], rapg25[home], home_era)
        lam_b = exp_runs(rpg25[home], rapg25[away], away_era)
        exp_tot = lam_a + lam_b

        dk_day = all_dk.get(g['date'], {})
        dk = dk_day.get(f'{away}@{home}')
        if not dk:
            continue

        actual_away = g.get('away_runs')
        actual_home = g.get('home_runs')
        actual_total = g.get('total')
        if actual_away is None or actual_home is None:
            continue

        def add(market, side_label, model_p, fair_p, vigged_imp, odds, won):
            dec = am_to_dec(odds)
            pnl_won  = FLAT_BET * (dec - 1)
            pnl_lost = -FLAT_BET
            for alpha in ALPHAS:
                blended = alpha * model_p + (1 - alpha) * fair_p
                edge    = (blended - vigged_imp) * 100
                pnl = pnl_won if won else pnl_lost
                bets_by_alpha[alpha].append({
                    'date':      g['date'],
                    'game':      f'{away}@{home}',
                    'market':    market,
                    'side':      side_label,
                    'alpha':     alpha,
                    'model_p':   model_p,
                    'fair_p':    fair_p,
                    'blended_p': blended,
                    'vigged_imp': vigged_imp,
                    'edge':      edge,
                    'odds':      odds,
                    'won':       won,
                    'pnl':       pnl,
                    'actual':    f'{actual_away}-{actual_home}',
                })

        # ── MONEYLINE ─────────────────────────────────────────────────────────
        if only_mkt in (None, 'ML'):
            ml_a = dk.get('ml_away'); ml_h = dk.get('ml_home')
            if ml_a and ml_h:
                imp_a = implied(ml_a); imp_h = implied(ml_h)
                fair_a, fair_h = devig(imp_a, imp_h)
                mp_a = p_ml_win(lam_a, lam_b, 'away')
                mp_h = p_ml_win(lam_a, lam_b, 'home')
                add('ML', 'AWAY', mp_a, fair_a, imp_a, ml_a,
                    actual_away > actual_home)
                add('ML', 'HOME', mp_h, fair_h, imp_h, ml_h,
                    actual_home > actual_away)

        # ── RUN LINE ──────────────────────────────────────────────────────────
        if only_mkt in (None, 'RL'):
            rl_a_pt  = dk.get('rl_away_pt');  rl_a_odds = dk.get('rl_away_odds')
            rl_h_pt  = dk.get('rl_home_pt');  rl_h_odds = dk.get('rl_home_odds')
            if None not in (rl_a_pt, rl_a_odds, rl_h_pt, rl_h_odds):
                margin = abs(rl_a_pt)
                imp_ra = implied(rl_a_odds); imp_rh = implied(rl_h_odds)
                fair_ra, fair_rh = devig(imp_ra, imp_rh)
                mp_ra = p_rl_side(lam_a, lam_b, rl_a_pt, 'away')
                mp_rh = p_rl_side(lam_a, lam_b, rl_h_pt, 'home')
                won_ra = (actual_away - actual_home >  margin) if rl_a_pt < 0 \
                    else (actual_away - actual_home > -margin)
                won_rh = (actual_home - actual_away >  margin) if rl_h_pt < 0 \
                    else (actual_home - actual_away > -margin)
                add('RL', f'AWAY {rl_a_pt:+.1f}', mp_ra, fair_ra, imp_ra,
                    rl_a_odds, won_ra)
                add('RL', f'HOME {rl_h_pt:+.1f}', mp_rh, fair_rh, imp_rh,
                    rl_h_odds, won_rh)

        # ── TOTALS ────────────────────────────────────────────────────────────
        if only_mkt in (None, 'TOT'):
            line = dk.get('total_line')
            t_u  = dk.get('total_under'); t_o = dk.get('total_over')
            if line and t_u and t_o and actual_total is not None:
                imp_u = implied(t_u); imp_o = implied(t_o)
                fair_u, fair_o = devig(imp_u, imp_o)
                mp_u = p_total_under(exp_tot, line)
                mp_o = p_total_over(exp_tot, line)
                add('TOT', f'UNDER {line}', mp_u, fair_u, imp_u, t_u,
                    actual_total <= line)
                add('TOT', f'OVER {line}',  mp_o, fair_o, imp_o, t_o,
                    actual_total > line)

    return bets_by_alpha


# ── Stats helper ──────────────────────────────────────────────────────────────
def stats(bets, min_edge):
    pos = [b for b in bets if b['edge'] >= min_edge]
    if not pos:
        return None
    n   = len(pos)
    w   = sum(1 for b in pos if b['won'])
    pnl = sum(b['pnl'] for b in pos)
    roi = pnl / (n * FLAT_BET) * 100
    hr  = w / n * 100
    avg_edge   = sum(b['edge']    for b in pos) / n
    avg_model  = sum(b['model_p'] for b in pos) / n * 100
    avg_fair   = sum(b['fair_p']  for b in pos) / n * 100
    avg_blend  = sum(b['blended_p'] for b in pos) / n * 100
    return dict(n=n, w=w, pnl=pnl, roi=roi, hr=hr,
                avg_edge=avg_edge, avg_model=avg_model,
                avg_fair=avg_fair, avg_blend=avg_blend)


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    # Parse args
    only_mkt  = None
    min_edge  = 3.0
    for i, arg in enumerate(sys.argv[1:]):
        if arg == '--mkt' and i+2 <= len(sys.argv)-1:
            only_mkt = sys.argv[i+2].upper()
        if arg == '--min-edge' and i+2 <= len(sys.argv)-1:
            min_edge = float(sys.argv[i+2])

    print(f"\n{'='*76}")
    print(f"  BLEND BACKTEST  —  Apr 28 – May 11, 2026  |  $100 flat bet")
    print(f"  Markets: {'ALL' if not only_mkt else only_mkt}  |  Min edge: +{min_edge}pp")
    print(f"  Model weight (alpha) tested: {ALPHAS}")
    print(f"{'='*76}")

    # Load cached odds
    if not os.path.exists(CACHE_FILE):
        print(f"  ERROR: {CACHE_FILE} not found. Run backtest_rr.py first.")
        return
    with open(CACHE_FILE) as f:
        all_dk = json.load(f)
    print(f"  Cached DK odds: {len(all_dk)} dates loaded.")

    # Load game results + pitcher stats
    profile = os.environ.get('USERPROFILE', 'C:/Users/17146')
    try:
        with open(os.path.join(profile, 'bt_games.json')) as f:
            games = json.load(f)
        with open(os.path.join(profile, 'bt_pit_stats.json')) as f:
            pit_stats = {int(k): v for k, v in json.load(f).items()}
    except FileNotFoundError as e:
        print(f"  ERROR: {e}")
        return

    print(f"  Games in dataset: {len(games)}")

    bets_by_alpha = build_bets(games, pit_stats, all_dk, only_mkt)

    # ── Overall comparison across alpha values ────────────────────────────────
    print()
    print(f"  ALL MARKETS — ROI comparison by blend weight")
    print(f"  (only positive-edge bets >= +{min_edge}pp)")
    print()
    print(f"  {'Alpha':>6}  {'Mkt Wt':>6}  {'Bets':>5}  {'Hit%':>6}  "
          f"{'Avg Model%':>10}  {'Avg Fair%':>9}  {'Avg Blend%':>10}  "
          f"{'Avg Edge':>9}  {'P&L':>9}  {'ROI':>7}")
    print(f"  {'-'*95}")

    best_roi  = -999
    best_alpha = 1.0
    results = {}

    for alpha in ALPHAS:
        s = stats(bets_by_alpha[alpha], min_edge)
        if not s:
            print(f"  {alpha:>6.1f}  {1-alpha:>6.1f}  {'—':>5}")
            continue
        mkt_w = f'{(1-alpha)*100:.0f}%'
        mod_w = f'{alpha*100:.0f}%'
        results[alpha] = s
        marker = ' <-- BEST' if s['roi'] > best_roi else ''
        if s['roi'] > best_roi:
            best_roi = s['roi']
            best_alpha = alpha
        print(f"  {alpha:>6.1f}  {mkt_w:>6}  {s['n']:>5}  {s['hr']:>5.1f}%  "
              f"{s['avg_model']:>9.1f}%  {s['avg_fair']:>8.1f}%  "
              f"{s['avg_blend']:>9.1f}%  {s['avg_edge']:>+8.1f}pp  "
              f"${s['pnl']:>+8,.0f}  {s['roi']:>+6.1f}%{marker}")

    # ── By market breakdown for best alpha ────────────────────────────────────
    print()
    print(f"  BEST ALPHA = {best_alpha:.1f}  ({(1-best_alpha)*100:.0f}% market weight)")
    print(f"  Breakdown by market at alpha={best_alpha:.1f}:")
    print()
    print(f"  {'Market':<8}  {'Bets':>5}  {'Hit%':>6}  {'Avg Edge':>9}  "
          f"{'P&L':>9}  {'ROI':>7}")
    print(f"  {'-'*55}")

    best_bets = bets_by_alpha[best_alpha]
    for mkt in ['ML', 'RL', 'TOT']:
        sub = [b for b in best_bets if b['market'] == mkt and b['edge'] >= min_edge]
        if not sub: continue
        n = len(sub); w = sum(1 for b in sub if b['won'])
        pnl = sum(b['pnl'] for b in sub)
        roi = pnl/(n*FLAT_BET)*100
        avg_e = sum(b['edge'] for b in sub)/n
        print(f"  {mkt:<8}  {n:>5}  {w/n*100:>5.1f}%  {avg_e:>+8.1f}pp  "
              f"${pnl:>+8,.0f}  {roi:>+6.1f}%")

    # ── Edge calibration: model vs blend vs reality ───────────────────────────
    print()
    print(f"  EDGE CALIBRATION — Pure Model (alpha=1.0) vs Best Blend (alpha={best_alpha:.1f})")
    print(f"  Did blending fix the overcalibration in high-edge buckets?")
    print()

    buckets = [(3,5),(5,8),(8,12),(12,20),(20,99)]

    # Pure model
    pure_bets = [b for b in bets_by_alpha[1.0] if b['edge'] >= min_edge]
    blend_bets = [b for b in bets_by_alpha[best_alpha] if b['edge'] >= min_edge]

    def show_calibration(bets, label, alphaval):
        print(f"  {label}  (alpha={alphaval:.1f})")
        print(f"  {'Bucket':<14}  {'N':>4}  {'Hit%':>6}  "
              f"{'Model%':>7}  {'Blend%':>7}  {'Diff':>7}  {'ROI':>7}")
        print(f"  {'-'*65}")
        for lo, hi in buckets:
            grp = [b for b in bets if lo <= b['edge'] < hi]
            if not grp: continue
            n = len(grp); w = sum(1 for b in grp if b['won'])
            pnl = sum(b['pnl'] for b in grp)
            roi = pnl/(n*FLAT_BET)*100
            hr  = w/n*100
            mdl = sum(b['model_p'] for b in grp)/n*100
            blnd = sum(b['blended_p'] for b in grp)/n*100
            print(f"  +{lo:>2}pp to +{hi:<2}pp  {n:>4}  {hr:>5.1f}%  "
                  f"{mdl:>6.1f}%  {blnd:>6.1f}%  {hr-blnd:>+6.1f}pp  "
                  f"{roi:>+6.1f}%")

    show_calibration(pure_bets, 'PURE MODEL', 1.0)
    print()
    show_calibration(blend_bets, f'BLENDED (alpha={best_alpha:.1f})', best_alpha)

    # ── Market overcalibration fix ────────────────────────────────────────────
    print()
    print(f"  TOTALS OVERCALIBRATION CHECK  (the key problem from backtest_rr.py)")
    print(f"  At pure model, 12+pp Totals edges showed 73% model but only 56% actual.")
    print()

    for alpha in [1.0, best_alpha]:
        tot_high = [b for b in bets_by_alpha[alpha]
                    if b['market']=='TOT' and b['edge'] >= 12]
        if not tot_high: continue
        n = len(tot_high); w = sum(1 for b in tot_high if b['won'])
        pnl = sum(b['pnl'] for b in tot_high)
        roi = pnl/(n*FLAT_BET)*100
        hr  = w/n*100
        avg_mdl  = sum(b['model_p'] for b in tot_high)/n*100
        avg_blnd = sum(b['blended_p'] for b in tot_high)/n*100
        print(f"  alpha={alpha:.1f}: TOT >=12pp edge  "
              f"N={n}  Hit={hr:.1f}%  Model={avg_mdl:.1f}%  "
              f"Blend={avg_blnd:.1f}%  ROI={roi:+.1f}%")

    # ── Final verdict ─────────────────────────────────────────────────────────
    print()
    print(f"  {'='*60}")
    print(f"  CONCLUSION")
    print(f"  {'='*60}")

    pure  = results.get(1.0)
    blend = results.get(best_alpha)
    if pure and blend:
        delta_roi  = blend['roi']  - pure['roi']
        delta_hr   = blend['hr']   - pure['hr']
        delta_edge = blend['avg_edge'] - pure['avg_edge']
        delta_n    = blend['n']    - pure['n']
        print(f"  Pure model  (alpha=1.0): {pure['n']:>3} bets  "
              f"{pure['hr']:.1f}% hit  {pure['roi']:+.1f}% ROI")
        print(f"  Best blend  (alpha={best_alpha:.1f}): {blend['n']:>3} bets  "
              f"{blend['hr']:.1f}% hit  {blend['roi']:+.1f}% ROI")
        print()
        print(f"  ROI change   : {delta_roi:+.1f}pp")
        print(f"  Hit% change  : {delta_hr:+.1f}pp")
        print(f"  Bet count    : {delta_n:+d}  (blending reduces big fake edges -> fewer bets)")
        print(f"  Avg edge chg : {delta_edge:+.1f}pp  (should be lower — market pulls down inflated edges)")
        print()
        if delta_roi > 0:
            print(f"  VERDICT: Blending HELPS — use alpha={best_alpha:.1f} ({(1-best_alpha)*100:.0f}% market weight)")
        elif delta_roi > -2:
            print(f"  VERDICT: Blending is NEUTRAL — minimal difference, keep pure model")
        else:
            print(f"  VERDICT: Blending HURTS — pure model is better, skip blending")
    print(f"  {'='*60}")
    print()


if __name__ == '__main__':
    run()
