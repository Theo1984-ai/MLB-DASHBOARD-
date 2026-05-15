"""
LIVE-PRICE BACKTEST  (Apr 28 – May 11, 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Enhances the existing 14-day backtest by fetching REAL DraftKings
standard total lines from the Odds API historical endpoint.

Instead of assuming flat -215 for every alt-under bet, we derive a
realistic market price for each alt line using the real DK total line:

    1. DK standard: Under T @ P  →  dk_implied = abs(P)/(abs(P)+100)
    2. Model fair at our alt line L: hist_p = P(Under L | lambda)
    3. Market vig factor = dk_implied(T) / model_p(T)
    4. Estimated real DK alt price = hist_p(L) × vig_factor
       (this anchors to the real market instead of a flat guess)

Historical alt_totals are NOT available on any Odds API plan,
so this is the best achievable real-price backtest from historical data.

Usage:
    python backtest_live.py           → full run (fetches API, caches results)
    python backtest_live.py --cached  → use cached data only (no API calls)
"""

import sys, os, json, math, time, urllib.request, urllib.parse
from datetime import date, timedelta
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
ODDS_KEY   = '328b6a86d5bd586ca057559e63a2d006'
BASE       = 'https://api.the-odds-api.com/v4'
CACHE_DIR  = 'cache'
CACHE_FILE = os.path.join(CACHE_DIR, 'bt_historical_odds.json')

FLAT_BET      = 100
MIN_ODDS      = -400
MAX_ODDS      = -200
EXP_THRESHOLD = 9.0
MIN_BUFFER    = 2.0
MAX_BUFFER    = 3.5
EDGE_MIN      = 3.0
LEAGUE_ERA    = 4.30

# ── Team stats (2025 baseline — matches main strategy) ────────────────────────
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
def poisson_pmf(k, lam):
    if k < 0 or lam <= 0: return 0.0
    return (lam**k * math.exp(-lam)) / math.factorial(k)

def p_under(lam, line):
    return sum(poisson_pmf(k, lam) for k in range(int(line) + 1))

def exp_runs(off, def_r, sp_era):
    return max(1.5, (off + def_r) / 2.0 + (sp_era - LEAGUE_ERA) * 0.22)

def american_to_dec(odds):
    return 1.0 + 100.0/abs(odds) if odds < 0 else 1.0 + odds/100.0

def implied(odds):
    return abs(odds)/(abs(odds)+100.0) if odds < 0 else 100.0/(odds+100.0)

def fair_american(p):
    """Convert probability to American odds (no vig)."""
    p = max(0.001, min(p, 0.9999))
    if p >= 0.5:
        return int(round(-p / (1.0 - p) * 100))
    return int(round((1.0 - p) / p * 100))

def best_under_lines(exp_total):
    lines = set()
    for buf in (2.5, 3.0, 3.5, 4.0):
        raw     = exp_total + buf
        rounded = math.floor(raw * 2) / 2.0
        lines.add(rounded)
        lines.add(rounded + 0.5)
    return sorted(lines)


# ── Derive real alt-line price from DK standard total ────────────────────────
def real_alt_price(dk_total_line, dk_under_price, alt_line, lam_total):
    """
    Derive a realistic DK price for Under alt_line using the real DK standard total.

    Method:
      1. dk_vig_factor = dk_implied(dk_under_price) / model_p(dk_total_line)
         → captures how much juice DK applies vs fair value at standard line
      2. model_p_alt = P(Under alt_line | lambda)
      3. fair_alt_price = -p/(1-p)*100
      4. DK alt price ≈ fair_alt_price × dk_vig_factor  (American → probability space)
    """
    model_at_dk = p_under(lam_total, dk_total_line)
    if model_at_dk <= 0.01:
        return None  # degenerate

    dk_imp_standard = implied(dk_under_price)
    vig_factor = dk_imp_standard / model_at_dk  # typically 1.03–1.10

    model_p_alt = p_under(lam_total, alt_line)
    if model_p_alt >= 0.9999:
        return MIN_ODDS  # essentially certain

    # Apply vig in probability space → convert back to American odds
    adj_p = min(model_p_alt * vig_factor, 0.9999)
    raw   = fair_american(adj_p)

    # Clamp to realistic alt-total range
    if raw > -150: raw = -150    # never cheaper than -150 for an alt under
    if raw < -900: raw = -900    # never more expensive than -900
    return raw


# ── Fetch historical DK totals for a date ────────────────────────────────────
def fetch_dk_totals_for_date(date_str, verbose=True):
    """
    Fetch DraftKings standard Under prices for all MLB games on date_str.
    Returns dict: {(away_abbr, home_abbr): (dk_total_line, dk_under_price)}

    Uses the nearest 18:00 UTC snapshot (covers most day games + evening).
    Falls back to 23:00 if no games at 18:00.
    """
    snapshots = ['18:00:00Z', '22:00:00Z']
    for snap in snapshots:
        ts  = f"{date_str}T{snap}"
        params = urllib.parse.urlencode({
            'apiKey':   ODDS_KEY,
            'date':     ts,
            'regions':  'us',
            'markets':  'totals',
            'bookmakers': 'draftkings',
            'oddsFormat': 'american',
        })
        url = f'{BASE}/historical/sports/baseball_mlb/odds?{params}'
        req = urllib.request.Request(url, headers={'User-Agent': 'mlb/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=12, context=_UNVERIFIED_SSL) as r:
                remaining = r.headers.get('x-requests-remaining', '?')
                data = json.loads(r.read())
            games = data.get('data', [])
            if verbose:
                print(f"    {date_str} @ {snap[:5]}: {len(games)} games  (credits left: {remaining})")
            result = {}
            for g in games:
                at = FULL_TO_ABBR.get(g['away_team'])
                ht = FULL_TO_ABBR.get(g['home_team'])
                if not at or not ht:
                    continue
                for bk in g.get('bookmakers', []):
                    if bk['key'] != 'draftkings':
                        continue
                    for mkt in bk.get('markets', []):
                        if mkt['key'] != 'totals':
                            continue
                        for o in mkt.get('outcomes', []):
                            if o['name'] == 'Under':
                                result[(at, ht)] = (float(o['point']), int(o['price']))
            if result:
                return result
            time.sleep(0.3)
        except Exception as e:
            if verbose:
                print(f"    {date_str}: API error — {e}")
    return {}


# ── Load backtest game data ────────────────────────────────────────────────────
def load_bt_data():
    profile    = os.environ.get('USERPROFILE', 'C:/Users/17146')
    games_path = os.path.join(profile, 'bt_games.json')
    pits_path  = os.path.join(profile, 'bt_pit_stats.json')
    if not os.path.exists(games_path):
        print("  ERROR: bt_games.json not found. Run fetch scripts first.")
        sys.exit(1)
    with open(games_path) as f:
        all_games = json.load(f)
    with open(pits_path) as f:
        pit_raw = json.load(f)
    pit_stats = {int(k): v for k, v in pit_raw.items()}
    return all_games, pit_stats


# ── Main backtest ─────────────────────────────────────────────────────────────
def run():
    use_cache_only = '--cached' in sys.argv
    os.makedirs(CACHE_DIR, exist_ok=True)

    print(f"\n{'='*76}")
    print(f"  LIVE-PRICE BACKTEST  (Apr 28 – May 11, 2026)")
    print(f"  Real DraftKings total lines · alt price derived from market vig")
    print(f"{'='*76}")

    # ── Load or fetch historical DK totals ────────────────────────────────────
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            dk_by_date = json.load(f)
        print(f"  Loaded cached DK odds for {len(dk_by_date)} dates.")
    else:
        dk_by_date = {}

    all_games, pit_stats = load_bt_data()
    by_date = defaultdict(list)
    for g in all_games:
        by_date[g['date']].append(g)

    if not use_cache_only:
        dates_to_fetch = [d for d in sorted(by_date) if d not in dk_by_date]
        if dates_to_fetch:
            print(f"  Fetching DK odds for {len(dates_to_fetch)} dates…")
            for d in dates_to_fetch:
                odds = fetch_dk_totals_for_date(d)
                # Serialize tuple keys as "AT@HT"
                dk_by_date[d] = {f'{at}@{ht}': [line, price]
                                  for (at, ht), (line, price) in odds.items()}
                time.sleep(0.4)
            with open(CACHE_FILE, 'w') as f:
                json.dump(dk_by_date, f, indent=2)
            print(f"  Cached to {CACHE_FILE}")
        else:
            print(f"  All dates already cached — no API calls made.")

    # ── Build bets with real-derived prices ──────────────────────────────────
    all_bets = []

    for d in sorted(by_date):
        day_games = by_date[d]
        dk_today  = {tuple(k.split('@')): v
                     for k, v in dk_by_date.get(d, {}).items()}

        for g in day_games:
            away = g['away']; home = g['home']
            if away not in rpg25 or home not in rpg25:
                continue

            apid = g.get('away_pit_id'); hpid = g.get('home_pit_id')
            away_era = pit_stats.get(apid, LEAGUE_ERA) if apid else LEAGUE_ERA
            home_era = pit_stats.get(hpid, LEAGUE_ERA) if hpid else LEAGUE_ERA

            lam_a     = exp_runs(rpg25[away], rapg25[home], home_era)
            lam_b     = exp_runs(rpg25[home], rapg25[away], away_era)
            exp_total = lam_a + lam_b

            if exp_total > EXP_THRESHOLD:
                continue

            actual_total = g.get('total')
            if actual_total is None:
                continue  # skip games without results

            # Real DK line data for this game
            dk_entry   = dk_today.get((away, home))
            dk_line    = float(dk_entry[0]) if dk_entry else None
            dk_u_price = int(dk_entry[1]) if dk_entry else None

            best = None
            for line in best_under_lines(exp_total):
                buf = line - exp_total
                if buf < MIN_BUFFER or buf > MAX_BUFFER:
                    continue

                hist_p = p_under(exp_total, line)

                # Derive price: real DK data when available, else estimated
                if dk_line is not None and dk_u_price is not None:
                    real_price = real_alt_price(dk_line, dk_u_price, line, exp_total)
                    price_source = 'real'
                else:
                    # Fallback: original flat estimate
                    if buf <= 2.0:   real_price = -900
                    elif buf <= 2.5: real_price = -400
                    elif buf <= 3.0: real_price = -260
                    elif buf <= 3.5: real_price = -215
                    else:            real_price = -200
                    price_source = 'est'

                if real_price is None or real_price < MIN_ODDS or real_price > MAX_ODDS:
                    continue

                imp  = implied(real_price)
                edge = (hist_p - imp) * 100

                if edge < EDGE_MIN:
                    continue

                if best is None or edge > best['edge']:
                    best = dict(
                        date=d, game=f'{away}@{home}',
                        away=away, home=home,
                        away_era=away_era, home_era=home_era,
                        exp_total=round(exp_total, 2),
                        dk_line=dk_line, dk_u_price=dk_u_price,
                        line=line, bet=f'Under {line}',
                        odds=real_price, price_src=price_source,
                        implied=imp, hist_p=hist_p,
                        edge=round(edge, 1),
                        actual_total=actual_total,
                        hit=actual_total <= line,
                    )
            if best:
                best['pnl'] = (FLAT_BET * american_to_dec(best['odds']) - FLAT_BET
                               if best['hit'] else -FLAT_BET)
                all_bets.append(best)

    if not all_bets:
        print("  No qualifying bets found.")
        return

    # ── Day-by-day log ────────────────────────────────────────────────────────
    print()
    print(f"  DAY-BY-DAY RESULTS")
    hdr = f"  {'Date':<12} {'B':>2} {'W':>2} {'P&L':>7}   Bets (actual/line  src)"
    print(hdr)
    print(f"  {'-'*80}")

    cum = 0.0
    for d in sorted(by_date):
        day_bets = [b for b in all_bets if b['date'] == d]
        if not day_bets:
            print(f"  {d:<12}  —  no qualifying games")
            continue
        day_pnl = sum(b['pnl'] for b in day_bets)
        cum    += day_pnl
        wins    = sum(1 for b in day_bets if b['hit'])
        sign    = '+' if day_pnl >= 0 else ''
        bet_strs = '  '.join(
            f"{b['game']} {b['bet']} {b['odds']:+d}"
            f"({'W' if b['hit'] else 'L'},{b['actual_total']}/{b['line']})[{b['price_src']}]"
            for b in day_bets
        )
        print(f"  {d:<12} {len(day_bets):>2} {wins:>2} {sign}${day_pnl:>5.0f}   {bet_strs}")

    # ── Summary ───────────────────────────────────────────────────────────────
    n          = len(all_bets)
    wins       = sum(1 for b in all_bets if b['hit'])
    wagered    = n * FLAT_BET
    net_pnl    = sum(b['pnl'] for b in all_bets)
    roi        = net_pnl / wagered * 100
    hit_rate   = wins / n * 100
    avg_mdl    = sum(b['hist_p'] for b in all_bets) / n * 100
    avg_imp    = sum(b['implied'] for b in all_bets) / n * 100
    avg_edge   = avg_mdl - avg_imp
    real_count = sum(1 for b in all_bets if b['price_src'] == 'real')
    est_count  = n - real_count
    avg_odds   = sum(b['odds'] for b in all_bets) / n

    print()
    print(f"  {'='*60}")
    print(f"  SUMMARY — {n} bets · {wins}W {n-wins}L · {len(by_date)} dates")
    print(f"  {'='*60}")
    print(f"  Actual hit rate    : {hit_rate:.1f}%")
    print(f"  Model projected    : {avg_mdl:.1f}%")
    print(f"  Book implied avg   : {avg_imp:.1f}%")
    print(f"  Average edge       : {avg_edge:+.1f}pp")
    print(f"  Average odds       : {avg_odds:+.0f}  (real-derived or est)")
    print()
    print(f"  Total wagered      : ${wagered:,.0f}  (${FLAT_BET}/bet)")
    print(f"  Net P&L            : ${net_pnl:+,.0f}")
    print(f"  ROI                : {roi:+.1f}%")
    print()
    print(f"  Price sources      : {real_count} real DK-derived · {est_count} estimated fallback")

    # ── Comparison: real vs estimated prices ──────────────────────────────────
    real_bets = [b for b in all_bets if b['price_src'] == 'real']
    est_bets  = [b for b in all_bets if b['price_src'] == 'est']

    if real_bets and est_bets:
        print()
        print(f"  REAL vs ESTIMATED PRICE SPLIT:")
        for label, subset in [('Real DK-derived', real_bets), ('Estimated flat', est_bets)]:
            sw = sum(1 for b in subset if b['hit'])
            sp = sum(b['pnl'] for b in subset)
            sr = sp / (len(subset) * FLAT_BET) * 100 if subset else 0
            hr = sw / len(subset) * 100 if subset else 0
            ao = sum(b['odds'] for b in subset) / len(subset)
            print(f"  {label:<20}: {len(subset):>3} bets  {sw}W/{len(subset)-sw}L  "
                  f"{hr:.1f}% hit  ${sp:+,.0f}  ROI {sr:+.1f}%  avg odds {ao:+.0f}")

    # ── By line breakdown ─────────────────────────────────────────────────────
    print()
    print(f"  BY BET TYPE:")
    print(f"  {'Bet':<14} {'N':>4} {'W':>4} {'Hit%':>7} {'AvgOdds':>9} {'P&L':>8} {'ROI':>7}")
    print(f"  {'-'*62}")
    by_line = defaultdict(lambda: {'bets':0,'wins':0,'pnl':0,'odds_sum':0})
    for b in all_bets:
        r = by_line[b['bet']]
        r['bets']+=1; r['wins']+=int(b['hit']); r['pnl']+=b['pnl']; r['odds_sum']+=b['odds']
    for lbl, r in sorted(by_line.items()):
        hr  = r['wins']/r['bets']*100
        roi_l = r['pnl']/(r['bets']*FLAT_BET)*100
        ao  = r['odds_sum']/r['bets']
        print(f"  {lbl:<14} {r['bets']:>4} {r['wins']:>4} {hr:>6.1f}%   {ao:>+6.0f}   ${r['pnl']:>+6,.0f}  {roi_l:>+6.1f}%")

    # ── Calibration ───────────────────────────────────────────────────────────
    print()
    print(f"  CALIBRATION  (model probability vs actual hit rate):")
    print(f"  {'Bucket':<18} {'N':>4} {'Model%':>8} {'Actual%':>8} {'Diff':>7}")
    buckets = [(0.60,0.70),(0.70,0.75),(0.75,0.80),(0.80,0.85),(0.85,0.95)]
    for lo, hi in buckets:
        g = [b for b in all_bets if lo <= b['hist_p'] < hi]
        if not g: continue
        mdl = sum(b['hist_p'] for b in g)/len(g)*100
        act = sum(int(b['hit']) for b in g)/len(g)*100
        print(f"  {lo:.0%}–{hi:.0%}            {len(g):>4} {mdl:>7.1f}%  {act:>7.1f}%  {act-mdl:>+6.1f}pp")

    # ── Real DK line vs model line comparison ─────────────────────────────────
    print()
    print(f"  DK TOTAL LINE vs MODEL LINE (qualifying games only):")
    print(f"  {'Game':<13} {'DK Line':>7} {'Model Exp':>10} {'Our Bet':>9} {'DK U-price':>10}  Result")
    print(f"  {'-'*65}")
    for b in sorted(all_bets, key=lambda x: x['date']):
        if b['dk_line'] is None: continue
        res = f"{'HIT' if b['hit'] else 'MISS'} {b['actual_total']}"
        dk_str = f"{b['dk_line']:.1f} ({b['dk_u_price']:+d})" if b['dk_line'] else "—"
        print(f"  {b['game']:<13} {dk_str:>14}  {b['exp_total']:>9.2f}  "
              f"{b['bet']:>9}  {res}")

    print()
    print(f"  Done. {real_count} real DK-derived prices used.")


if __name__ == '__main__':
    run()
