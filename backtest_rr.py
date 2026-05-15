"""
ROUND ROBIN PAGE — GAME MODEL BACKTEST  (Apr 28 – May 11, 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tests the three markets the Round Robin page uses:
  1. Moneyline  (h2h)
  2. Run line   (spreads ±1.5)
  3. Standard totals (Over/Under)

Model: Poisson run-expectation (same 2025 RPG/RAPG baseline as the
alt-under strategy, plus pitcher ERA adjustment). Win/cover/total
probabilities derived analytically from the Poisson distribution.

For each qualifying pick (model edge >= MIN_EDGE pp):
  - Did it win?
  - P&L at $100/bet using real DK prices

Usage:
  python backtest_rr.py           → fetch + run (first time)
  python backtest_rr.py --cached  → re-run on cached odds (free)
"""

import sys, os, json, math, time, urllib.request, urllib.parse
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_KEY   = '328b6a86d5bd586ca057559e63a2d006'
BASE       = 'https://api.the-odds-api.com/v4'
CACHE_FILE = 'cache/bt_rr_odds.json'
FLAT_BET   = 100
MIN_EDGE   = 3.0     # minimum pp edge to count as a "recommended" bet
LEAGUE_ERA = 4.30

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

def p_win(lam_a, lam_b, max_runs=20):
    """P(away wins) — Poisson, ignoring ties."""
    p = 0.0
    for a in range(max_runs):
        pa = pmf(a, lam_a)
        for b in range(a):           # b < a → away wins
            p += pa * pmf(b, lam_b)
    return p

def p_tie(lam_a, lam_b, max_runs=20):
    return sum(pmf(k, lam_a) * pmf(k, lam_b) for k in range(max_runs))

def p_ml_win(lam_a, lam_b, side='away'):
    """Win probability for a side, excluding ties (standard ML contract)."""
    raw_a = p_win(lam_a, lam_b)
    raw_b = 1 - raw_a - p_tie(lam_a, lam_b)
    tie   = p_tie(lam_a, lam_b)
    # Redistribute ties proportionally (standard sportsbook treatment)
    if tie < 0.9999:
        adj_a = raw_a / (1 - tie)
        adj_b = raw_b / (1 - tie)
    else:
        adj_a = adj_b = 0.5
    return adj_a if side == 'away' else adj_b

def p_rl_cover(lam_a, lam_b, rl=1.5, side='away', max_runs=20):
    """
    P(side covers run line of rl).
    side='away' with rl=1.5 → away wins by 2+ (gives 1.5 runs)
    side='away' with rl=-1.5 → away wins or loses by 1 (gets 1.5 runs)
    """
    p = 0.0
    for a in range(max_runs):
        pa = pmf(a, lam_a)
        for b in range(max_runs):
            pb = pmf(b, lam_b)
            diff = (a - b) if side == 'away' else (b - a)
            if side == 'away':
                if rl > 0 and diff > rl:    p += pa * pb   # giving rl → must win by more
                elif rl < 0 and diff > rl:  p += pa * pb   # getting rl → must not lose by more
            else:
                diff = b - a
                if rl > 0 and diff > rl:    p += pa * pb
                elif rl < 0 and diff > rl:  p += pa * pb
    return p

def p_total_under(lam_total, line):
    return sum(pmf(k, lam_total) for k in range(int(line) + 1))

def p_total_over(lam_total, line):
    return 1.0 - p_total_under(lam_total, line) + pmf(int(line), lam_total)

def implied(odds):
    if odds == 0: return 0.5
    return abs(odds)/(abs(odds)+100.0) if odds < 0 else 100.0/(odds+100.0)

def am_to_dec(odds):
    return 1.0 + 100.0/abs(odds) if odds < 0 else 1.0 + odds/100.0

def fair_am(p):
    p = max(0.001, min(p, 0.9999))
    return int(-p/(1-p)*100) if p >= 0.5 else int((1-p)/p*100)


# ── Fetch historical DK odds ──────────────────────────────────────────────────
def fetch_dk_for_date(date_str, verbose=True):
    """Fetch DK h2h + spreads + totals for a date. Returns dict keyed AT@HT."""
    for snap in ['18:00:00Z', '22:00:00Z']:
        params = urllib.parse.urlencode({
            'apiKey':     ODDS_KEY,
            'date':       f'{date_str}T{snap}',
            'regions':    'us',
            'markets':    'h2h,spreads,totals',
            'bookmakers': 'draftkings',
            'oddsFormat': 'american',
        })
        url = f'{BASE}/historical/sports/baseball_mlb/odds?{params}'
        req = urllib.request.Request(url, headers={'User-Agent':'mlb/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=12, context=_UNVERIFIED_SSL) as r:
                remaining = r.headers.get('x-requests-remaining','?')
                games = json.loads(r.read()).get('data', [])
            if verbose:
                print(f'    {date_str}@{snap[:5]}: {len(games)} games  '
                      f'(credits left: {remaining})')
            if not games:
                time.sleep(0.3); continue
            out = {}
            for g in games:
                at = FULL_TO_ABBR.get(g['away_team'])
                ht = FULL_TO_ABBR.get(g['home_team'])
                if not at or not ht: continue
                key = f'{at}@{ht}'
                entry = {'ml_away': None, 'ml_home': None,
                         'rl_away_pt': None, 'rl_away_odds': None,
                         'rl_home_pt': None, 'rl_home_odds': None,
                         'total_line': None, 'total_under': None, 'total_over': None}
                for bk in g.get('bookmakers', []):
                    if bk['key'] != 'draftkings': continue
                    for mkt in bk.get('markets', []):
                        if mkt['key'] == 'h2h':
                            for o in mkt['outcomes']:
                                ab = FULL_TO_ABBR.get(o['name'])
                                if ab == at:   entry['ml_away'] = int(o['price'])
                                elif ab == ht: entry['ml_home'] = int(o['price'])
                        elif mkt['key'] == 'spreads':
                            for o in mkt['outcomes']:
                                ab = FULL_TO_ABBR.get(o['name'])
                                if ab == at:
                                    entry['rl_away_pt']   = float(o['point'])
                                    entry['rl_away_odds'] = int(o['price'])
                                elif ab == ht:
                                    entry['rl_home_pt']   = float(o['point'])
                                    entry['rl_home_odds'] = int(o['price'])
                        elif mkt['key'] == 'totals':
                            for o in mkt['outcomes']:
                                if o['name'] == 'Under':
                                    entry['total_line']  = float(o['point'])
                                    entry['total_under'] = int(o['price'])
                                elif o['name'] == 'Over':
                                    entry['total_over']  = int(o['price'])
                out[key] = entry
            return out
        except Exception as e:
            if verbose: print(f'    {date_str}: error — {e}')
    return {}


# ── Load bt data ──────────────────────────────────────────────────────────────
def load_bt():
    profile = os.environ.get('USERPROFILE', 'C:/Users/17146')
    with open(os.path.join(profile, 'bt_games.json')) as f:
        games = json.load(f)
    with open(os.path.join(profile, 'bt_pit_stats.json')) as f:
        pit_raw = json.load(f)
    pit_stats = {int(k): v for k, v in pit_raw.items()}
    return games, pit_stats


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    use_cache = '--cached' in sys.argv
    os.makedirs('cache', exist_ok=True)

    print(f"\n{'='*76}")
    print(f"  ROUND ROBIN GAME MODEL BACKTEST  (Apr 28 – May 11, 2026)")
    print(f"  Markets: Moneyline · Run Line · Totals | Real DK prices")
    print(f"{'='*76}")

    # ── Fetch or load odds ────────────────────────────────────────────────────
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            all_dk = json.load(f)
        print(f"  Loaded cached DK odds for {len(all_dk)} dates.")
    else:
        all_dk = {}

    games, pit_stats = load_bt()
    by_date = defaultdict(list)
    for g in games:
        by_date[g['date']].append(g)

    if not use_cache:
        dates_missing = [d for d in sorted(by_date) if d not in all_dk]
        if dates_missing:
            print(f"  Fetching DK odds for {len(dates_missing)} dates "
                  f"(h2h + spreads + totals)...")
            for d in dates_missing:
                all_dk[d] = fetch_dk_for_date(d)
                time.sleep(0.4)
            with open(CACHE_FILE, 'w') as f:
                json.dump(all_dk, f, indent=2)
            print(f"  Cached to {CACHE_FILE}")
        else:
            print(f"  All dates cached — no API calls made.")

    # ── Build bets ────────────────────────────────────────────────────────────
    all_bets = []

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
            continue  # no odds for this game

        actual_away = g.get('away_runs')
        actual_home = g.get('home_runs')
        actual_total = g.get('total')
        if actual_away is None or actual_home is None:
            continue

        # ── 1. MONEYLINE ──────────────────────────────────────────────────────
        for side in ('away', 'home'):
            odds = dk.get(f'ml_{side}')
            if not odds: continue
            mp = p_ml_win(lam_a, lam_b, side=side)
            imp = implied(odds)
            edge = (mp - imp) * 100
            if abs(edge) < 0.1: continue  # skip near-zero
            won = (actual_away > actual_home) if side == 'away' else (actual_home > actual_away)
            dec = am_to_dec(odds)
            pnl = FLAT_BET*(dec-1) if won else -FLAT_BET
            all_bets.append({
                'date': g['date'], 'game': f'{away}@{home}',
                'market': 'ML', 'side': side.upper(),
                'label': f'ML {side.upper()} {odds:+d}',
                'model_p': mp, 'imp': imp, 'edge': edge,
                'odds': odds, 'won': won, 'pnl': pnl,
                'actual': f'{actual_away}-{actual_home}',
            })

        # ── 2. RUN LINE ───────────────────────────────────────────────────────
        for side, pt_key, odds_key in [
            ('away', 'rl_away_pt', 'rl_away_odds'),
            ('home', 'rl_home_pt', 'rl_home_odds'),
        ]:
            pt = dk.get(pt_key); odds = dk.get(odds_key)
            if pt is None or not odds: continue
            # For away side: positive pt = giving runs (fav), negative = getting runs (dog)
            # p_rl_cover convention: rl > 0 means side must win by more than rl
            margin = abs(pt)
            if side == 'away':
                # away rl = pt (e.g. -1.5 means away is fav giving 1.5)
                if pt < 0:   # away is favorite, giving runs
                    mp = p_ml_win(lam_a, lam_b, 'away')
                    # Need to win by 2+ (cover -1.5)
                    mp = sum(pmf(a, lam_a) * sum(pmf(b, lam_b)
                             for b in range(max(0, a - int(margin))))
                             for a in range(25))
                else:        # away is dog, getting runs
                    mp = sum(pmf(a, lam_a) * sum(pmf(b, lam_b)
                             for b in range(min(25, a + int(margin) + 1)))
                             for a in range(25))
                won = (actual_away - actual_home > margin) if pt < 0 \
                      else (actual_away - actual_home > -margin)
            else:
                if pt < 0:   # home is fav, giving runs
                    mp = sum(pmf(b, lam_b) * sum(pmf(a, lam_a)
                             for a in range(max(0, b - int(margin))))
                             for b in range(25))
                else:        # home is dog, getting runs
                    mp = sum(pmf(b, lam_b) * sum(pmf(a, lam_a)
                             for a in range(min(25, b + int(margin) + 1)))
                             for b in range(25))
                won = (actual_home - actual_away > margin) if pt < 0 \
                      else (actual_home - actual_away > -margin)

            imp  = implied(odds)
            edge = (mp - imp) * 100
            if abs(edge) < 0.1: continue
            dec  = am_to_dec(odds)
            pnl  = FLAT_BET*(dec-1) if won else -FLAT_BET
            all_bets.append({
                'date': g['date'], 'game': f'{away}@{home}',
                'market': 'RL', 'side': f'{side.upper()} {pt:+.1f}',
                'label': f'RL {side.upper()} {pt:+.1f} ({odds:+d})',
                'model_p': mp, 'imp': imp, 'edge': edge,
                'odds': odds, 'won': won, 'pnl': pnl,
                'actual': f'{actual_away}-{actual_home}',
            })

        # ── 3. TOTALS ─────────────────────────────────────────────────────────
        line = dk.get('total_line')
        if line and actual_total is not None:
            for side, odds_key, prob_fn in [
                ('UNDER', 'total_under', lambda l: p_total_under(exp_tot, l)),
                ('OVER',  'total_over',  lambda l: p_total_over(exp_tot, l)),
            ]:
                odds = dk.get(odds_key)
                if not odds: continue
                mp   = prob_fn(line)
                imp  = implied(odds)
                edge = (mp - imp) * 100
                if abs(edge) < 0.1: continue
                won  = (actual_total <= line) if side == 'UNDER' else (actual_total > line)
                dec  = am_to_dec(odds)
                pnl  = FLAT_BET*(dec-1) if won else -FLAT_BET
                all_bets.append({
                    'date': g['date'], 'game': f'{away}@{home}',
                    'market': 'TOT', 'side': f'{side} {line}',
                    'label': f'Total {side} {line} ({odds:+d})',
                    'model_p': mp, 'imp': imp, 'edge': edge,
                    'odds': odds, 'won': won, 'pnl': pnl,
                    'actual': str(actual_total),
                })

    if not all_bets:
        print("  No bets generated — check odds cache.")
        return

    # ── Summary by market ─────────────────────────────────────────────────────
    def stats(subset, label):
        if not subset: return
        n    = len(subset)
        w    = sum(1 for b in subset if b['won'])
        pnl  = sum(b['pnl'] for b in subset)
        roi  = pnl / (n * FLAT_BET) * 100
        hr   = w / n * 100
        aedge = sum(b['edge'] for b in subset) / n
        aodds = sum(b['odds'] for b in subset) / n
        print(f"  {label:<25} {n:>4} bets  {w:>3}W/{n-w:<3}L  "
              f"{hr:>5.1f}% hit  avg edge {aedge:>+5.1f}pp  "
              f"avg odds {aodds:>+5.0f}  P&L ${pnl:>+7,.0f}  ROI {roi:>+6.1f}%")

    # Split by market and by edge tier
    ml_bets   = [b for b in all_bets if b['market'] == 'ML']
    rl_bets   = [b for b in all_bets if b['market'] == 'RL']
    tot_bets  = [b for b in all_bets if b['market'] == 'TOT']

    # Only positive-edge bets (what the round robin page recommends)
    pos_ml    = [b for b in ml_bets  if b['edge'] >= MIN_EDGE]
    pos_rl    = [b for b in rl_bets  if b['edge'] >= MIN_EDGE]
    pos_tot   = [b for b in tot_bets if b['edge'] >= MIN_EDGE]
    all_pos   = pos_ml + pos_rl + pos_tot

    print()
    print(f"  ALL OUTCOMES (every game, both sides priced by DK):")
    print(f"  {'-'*80}")
    stats(ml_bets,  'Moneyline — all sides')
    stats(rl_bets,  'Run Line  — all sides')
    stats(tot_bets, 'Totals    — both O/U')

    print()
    print(f"  POSITIVE-EDGE PICKS (edge >= +{MIN_EDGE}pp — what the app recommends):")
    print(f"  {'-'*80}")
    stats(pos_ml,   'ML   positive edge')
    stats(pos_rl,   'RL   positive edge')
    stats(pos_tot,  'TOT  positive edge')
    stats(all_pos,  'ALL  positive edge')

    # ── Edge bucket calibration ───────────────────────────────────────────────
    print()
    print(f"  EDGE BUCKET BREAKDOWN  (positive-edge bets only):")
    print(f"  {'Edge bucket':<18} {'N':>4}  {'W':>3}/{'{L}':<4}  "
          f"{'Hit%':>6}  {'Model%':>7}  {'Diff':>6}  {'P&L':>8}  {'ROI':>7}")
    print(f"  {'-'*75}")
    buckets = [(3,5),(5,8),(8,12),(12,20),(20,99)]
    for lo, hi in buckets:
        grp = [b for b in all_pos if lo <= b['edge'] < hi]
        if not grp: continue
        n = len(grp); w = sum(1 for b in grp if b['won'])
        pnl = sum(b['pnl'] for b in grp)
        roi = pnl/(n*FLAT_BET)*100
        hr  = w/n*100
        mdl = sum(b['model_p'] for b in grp)/n*100
        print(f"  +{lo:>2}pp to +{hi:<2}pp        {n:>4}  {w:>3}/{n-w:<4}  "
              f"{hr:>5.1f}%  {mdl:>6.1f}%  {hr-mdl:>+5.1f}pp  "
              f"${pnl:>+7,.0f}  {roi:>+6.1f}%")

    # ── By market: edge calibration ───────────────────────────────────────────
    print()
    print(f"  BY MARKET — edge calibration (positive-edge only):")
    for mkt_label, subset in [('ML', pos_ml), ('RL', pos_rl), ('TOT', pos_tot)]:
        if not subset: continue
        print(f"\n  {mkt_label}:")
        print(f"  {'Edge bucket':<18} {'N':>4}  {'Hit%':>6}  {'Model%':>7}  {'P&L':>8}  {'ROI':>7}")
        for lo, hi in [(3,5),(5,8),(8,12),(12,99)]:
            grp = [b for b in subset if lo <= b['edge'] < hi]
            if not grp: continue
            n = len(grp); w = sum(1 for b in grp if b['won'])
            pnl = sum(b['pnl'] for b in grp)
            roi = pnl/(n*FLAT_BET)*100
            hr  = w/n*100
            mdl = sum(b['model_p'] for b in grp)/n*100
            print(f"  +{lo:>2}pp to +{hi:<2}pp        {n:>4}  "
                  f"{hr:>5.1f}%  {mdl:>6.1f}%  ${pnl:>+7,.0f}  {roi:>+6.1f}%")

    # ── Day-by-day summary ────────────────────────────────────────────────────
    print()
    print(f"  DAY-BY-DAY  (positive-edge picks only):")
    print(f"  {'Date':<12} {'ML':>5} {'RL':>5} {'TOT':>5} {'P&L':>8}")
    print(f"  {'-'*40}")
    cum = 0.0
    for d in sorted(by_date):
        dm = [b for b in pos_ml  if b['date']==d]
        dr = [b for b in pos_rl  if b['date']==d]
        dt = [b for b in pos_tot if b['date']==d]
        if not (dm or dr or dt): continue
        dpnl = sum(b['pnl'] for b in dm+dr+dt)
        cum += dpnl
        def ws(lst):
            if not lst: return ' — '
            w = sum(1 for b in lst if b['won'])
            return f'{w}W/{len(lst)-w}L'
        sign = '+' if dpnl >= 0 else ''
        print(f"  {d:<12} {ws(dm):>5} {ws(dr):>5} {ws(dt):>5} "
              f"{sign}${dpnl:>6.0f}  (cum {'+' if cum>=0 else ''}${cum:.0f})")

    # ── Final verdict ─────────────────────────────────────────────────────────
    print()
    print(f"  {'='*60}")
    n = len(all_pos)
    w = sum(1 for b in all_pos if b['won'])
    pnl = sum(b['pnl'] for b in all_pos)
    roi = pnl/(n*FLAT_BET)*100 if n else 0
    print(f"  VERDICT — {n} positive-edge bets across all 3 markets")
    print(f"  Win/Loss : {w}W – {n-w}L  ({w/n*100:.1f}% hit rate)" if n else "  No bets.")
    print(f"  Net P&L  : ${pnl:+,.0f}  on ${n*FLAT_BET:,} wagered")
    print(f"  ROI      : {roi:+.1f}%")
    avg_edge = sum(b['edge'] for b in all_pos)/n if n else 0
    print(f"  Avg edge : {avg_edge:+.1f}pp  (model showed this as edge — did it show up?)")
    print(f"  {'='*60}")


if __name__ == '__main__':
    run()
