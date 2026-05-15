"""
ALT UNDER STRATEGY — Full implementation + 14-day backtest.

Strategy rules:
  1. Calculate model expected total (2025 team offense/defense + pitcher ERA adj)
  2. Qualify game if expected total <= EXP_THRESHOLD (default 9.0)
  3. Bet: Alt Under at LINE = floor(expected + OVER_BUFFER + 0.5) rounded to nearest .5
     e.g. exp=7.9 → Under 11.0 (7.9 + 3.1)
  4. Only bet if edge (actual_prob - implied) > EDGE_MIN
  5. Odds range: -200 to -1000 only

Usage:
  python strategy_alt_under.py           → today's picks + backtest summary
  python strategy_alt_under.py --today   → today's picks only
  python strategy_alt_under.py --bt      → backtest only
"""
import sys, os, json, math, time, urllib.request, urllib.parse
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, '.')
from data.odds import _fetch, BASE, SPORT_KEY, american_to_implied

# ── Strategy parameters ──────────────────────────────────────────────────────
EXP_THRESHOLD  = 9.0    # only bet when model expects <= this many total runs
OVER_BUFFER    = 2.5    # Under line = expected + OVER_BUFFER (rounded to nearest .5)
MIN_BUFFER     = 2.0    # minimum buffer above expected
MAX_BUFFER     = 3.5    # cap: don't take crazy-expensive lines (Under exp+4 @ -900)
MIN_ODDS       = -400   # cap: don't pay more than -400 (edge evaporates at tight lines)
MAX_ODDS       = -200   # minimum: don't go cheaper than -200 (not enough probability)
EDGE_MIN       = 3.0    # minimum pp edge vs 2025 baseline to place bet
FLAT_BET       = 100    # $ per game

ODDS_KEY = '328b6a86d5bd586ca057559e63a2d006'

# ── Team stats ───────────────────────────────────────────────────────────────
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
rpg26 = {
    'WSH':5.29,'CIN':4.00,'NYY':5.17,'BAL':4.38,'PHI':4.15,'BOS':3.90,
    'COL':4.27,'PIT':5.02,'TB':4.47,'TOR':4.12,'DET':4.27,'NYM':3.48,
    'CHC':5.24,'ATL':5.56,'KC':4.12,'CWS':4.28,'MIA':4.29,'MIN':4.73,
    'SD':4.25,'MIL':5.13,'AZ':4.25,'TEX':3.63,'SEA':4.05,'HOU':4.69,
    'STL':4.65,'ATH':4.40,'SF':3.39,'LAD':5.02,'LAA':4.26,'CLE':4.19,
}
rapg26 = {
    'WSH':4.80,'CIN':4.49,'NYY':3.10,'BAL':4.59,'PHI':4.54,'BOS':3.92,
    'COL':4.76,'PIT':3.78,'TB':3.48,'TOR':4.07,'DET':3.76,'NYM':3.83,
    'CHC':3.78,'ATL':3.10,'KC':4.22,'CWS':4.17,'MIA':3.98,'MIN':4.51,
    'SD':4.05,'MIL':3.40,'AZ':4.30,'TEX':3.49,'SEA':3.67,'HOU':5.33,
    'STL':4.25,'ATH':4.38,'SF':4.00,'LAD':3.34,'LAA':4.52,'CLE':3.81,
}

LEAGUE_ERA  = 4.30   # raw ERA league average — backtest shows raw ERA > xERA for this strategy
FALLBACK    = 4.30

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

def mlb_fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent':'mlb/1.0'})
    with urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
        return json.loads(r.read().decode())

def exp_runs(off, def_r, sp_era):
    base = (off + def_r) / 2.0
    adj  = (sp_era - LEAGUE_ERA) * 0.22
    return max(1.5, base + adj)

def poisson_pmf(k, lam):
    if k < 0 or lam <= 0: return 0.0
    return (lam ** k * math.exp(-lam)) / math.factorial(k)

def p_under(lam, line):
    return sum(poisson_pmf(k, lam) for k in range(int(line) + 1))

def best_under_line(exp_total, buffers=(2.5, 3.0, 3.5, 4.0)):
    """Return candidate Under lines by rounding exp+buffer to nearest .5"""
    lines = set()
    for buf in buffers:
        raw = exp_total + buf
        rounded = math.floor(raw * 2) / 2.0  # round down to nearest .5
        lines.add(rounded)
        lines.add(rounded + 0.5)
    return sorted(lines)

_era_cache: dict = {}

def get_pitcher_quality(pid, xera_lookup=None, cache=None):
    """
    Return the best available ERA proxy for a pitcher.
    Priority: xERA (from Baseball Savant) > raw ERA (from MLB API) > FALLBACK.
    xERA strips out BABIP luck and strand-rate luck, making it a better predictor.
    """
    if cache is None:
        cache = _era_cache
    if pid is None:
        return FALLBACK
    if pid in cache:
        return cache[pid]

    # Check xERA lookup first (pre-built from Baseball Savant)
    if xera_lookup and pid in xera_lookup:
        val = xera_lookup[pid]
        if 0.5 < val < 10.0:
            cache[pid] = val
            return val

    # Fall back to raw ERA from MLB Stats API
    try:
        d = mlb_fetch(
            f'https://statsapi.mlb.com/api/v1/people/{pid}/stats'
            f'?stats=season&season=2026&group=pitching&gameType=R'
        )
        splits = d.get('stats', [{}])[0].get('splits', [])
        if splits:
            s   = splits[0].get('stat', {})
            ip  = float(s.get('inningsPitched', '0') or '0')
            era = float(s.get('era', '0') or '0')
            cache[pid] = era if (ip >= 5 and 0.5 < era < 10.0) else FALLBACK
        else:
            cache[pid] = FALLBACK
    except Exception:
        cache[pid] = FALLBACK
    return cache[pid]


# Keep old name as alias so backtest code still works
def get_pitcher_era(pid, cache={}):
    return get_pitcher_quality(pid, xera_lookup=None, cache=cache)

def qualify_and_build_bets(
    games_for_date,
    pit_stats=None,
    use_live_odds=False,
    event_id_map=None,
    season='2026',
    xera_lookup=None,   # kept for signature compat — backtest showed raw ERA > xERA here
    ump_data=None,      # {game_pk: {name, impact, ...}} — display only, not used for filtering
    fatigue=None,       # {team_abbr: {rating, ...}} — display only, not used for filtering
):
    """
    For a list of games, apply the strategy filter and return qualifying bets.

    games_for_date: list of dicts with keys: away, home, away_pit_id, home_pit_id
                    and optionally: away_runs, home_runs, total (for backtest),
                    game_pk (required for umpire lookup)

    Filtering uses RAW ERA only (backtest Apr 28–May 11 showed raw ERA outperforms
    xERA for game selection: baseline 85.7% hit rate vs xERA 50.0% on disagreements).
    Bullpen fatigue excluded 72% of games that would have WON — reverted to display-only.
    Umpire data is shown as context but does NOT adjust the qualifying threshold.

    Returns list of bet dicts sorted by edge (best first).
    """
    bets = []

    for g in games_for_date:
        away = g['away']
        home = g['home']
        if away not in rpg25 or home not in rpg25:
            continue

        apid = g.get('away_pit_id')
        hpid = g.get('home_pit_id')

        # Always use raw ERA — xERA reverted after backtest showed it hurt performance
        if pit_stats:
            away_era = pit_stats.get(apid, FALLBACK) if apid else FALLBACK
            home_era = pit_stats.get(hpid, FALLBACK) if hpid else FALLBACK
        else:
            away_era = get_pitcher_quality(apid, xera_lookup=None)
            home_era = get_pitcher_quality(hpid, xera_lookup=None)
            time.sleep(0.08)

        # Use 2025 stats as baseline (matches backtest design)
        lam_a     = exp_runs(rpg25[away], rapg25[home], home_era)
        lam_b     = exp_runs(rpg25[home], rapg25[away], away_era)
        exp_total = lam_a + lam_b

        # ── Umpire: display info only, no threshold adjustment ────────────────
        ump_info = {}
        ump_adj  = 1.0
        if ump_data:
            gk = g.get('game_pk')
            if gk and gk in ump_data:
                ump_info = ump_data[gk]
                ump_adj  = ump_info.get('run_adj', 1.0)

        # ── Bullpen fatigue: display info only, no threshold adjustment ───────
        bp_away_rating = 'N/A'
        bp_home_rating = 'N/A'
        bp_away_score  = 0
        bp_home_score  = 0
        bp_adj         = 1.0
        if fatigue:
            af = fatigue.get(away, {})
            hf = fatigue.get(home, {})
            bp_away_rating = af.get('rating', 'normal')
            bp_home_rating = hf.get('rating', 'normal')
            bp_away_score  = af.get('fatigue_score', 30)
            bp_home_score  = hf.get('fatigue_score', 30)
            bp_adj = ((af.get('run_adj', 1.0) - 1) +
                      (hf.get('run_adj', 1.0) - 1)) / 2 + 1

        # adj_total computed for display only — NOT used for qualifying or line selection
        adj_total = round(exp_total * ump_adj * bp_adj, 2)

        # ── Qualifying threshold uses raw exp_total only ──────────────────────
        if exp_total > EXP_THRESHOLD:
            continue

        candidates = best_under_line(exp_total)

        for line in candidates:
            hist_p = p_under(exp_total, line)
            buffer = line - exp_total
            if buffer < MIN_BUFFER or buffer > MAX_BUFFER:
                continue

            # Get live odds if available, otherwise use table
            if use_live_odds and event_id_map:
                odds = None
            else:
                # Market-observed pricing (calibrated from FanDuel/DK):
                # Buffer 2.5-3.5 above expected maps to -200 to -260 at most books
                buf = line - exp_total
                if buf <= 2.0:   odds = -900
                elif buf <= 2.5: odds = -400
                elif buf <= 3.0: odds = -260
                elif buf <= 3.5: odds = -215
                elif buf <= 4.0: odds = -200
                else:            odds = -185

            if odds is None or odds < MIN_ODDS or odds > MAX_ODDS:
                continue

            implied = american_to_implied(odds)
            edge    = (hist_p - implied) * 100

            if edge < EDGE_MIN:
                continue

            bets.append({
                'game':           f'{away}@{home}',
                'away':           away,
                'home':           home,
                'game_pk':        g.get('game_pk'),
                'away_sp_era':    away_era,
                'home_sp_era':    home_era,
                'away_pit':       g.get('away_pit', 'TBD'),
                'home_pit':       g.get('home_pit', 'TBD'),
                'exp_total':      round(exp_total, 2),
                'adj_total':      round(adj_total, 2),
                'ump_name':       ump_info.get('name', 'TBD'),
                'ump_impact':     ump_info.get('impact', 'neutral'),
                'ump_run_adj':    round(ump_adj, 3),
                'bp_away_rating': bp_away_rating,
                'bp_home_rating': bp_home_rating,
                'bp_away_score':  bp_away_score,
                'bp_home_score':  bp_home_score,
                'bp_adj':         round(bp_adj, 3),
                'line':           line,
                'bet':            f'Under {line}',
                'odds':           odds,
                'implied':        implied,
                'hist_p':         hist_p,
                'edge':           edge,
            })

    # De-duplicate: one bet per game (best edge)
    by_game = {}
    for b in bets:
        k = b['game']
        if k not in by_game or b['edge'] > by_game[k]['edge']:
            by_game[k] = b
    return sorted(by_game.values(), key=lambda x: -x['edge'])


# ── TODAY'S PICKS ────────────────────────────────────────────────────────────
def run_today():
    today      = date.today().isoformat()
    today_year = int(today[:4])
    print(f"\n{'='*72}")
    print(f"  ALT UNDER STRATEGY — TODAY'S QUALIFYING PLAYS  ({today})")
    print(f"  Rules: exp total <= {EXP_THRESHOLD} | Under (exp + {MIN_BUFFER}-{MAX_BUFFER}) | "
          f"-{abs(MAX_ODDS)} to -{abs(MIN_ODDS)} | edge >= {EDGE_MIN}pp")
    print(f"{'='*72}")

    # ── 1. xERA lookup (Baseball Savant) ─────────────────────────────────────
    print("  Loading xERA data (Baseball Savant)...", end=' ', flush=True)
    xera_lookup = {}
    try:
        from data.fangraphs import build_xera_lookup
        xera_lookup = build_xera_lookup(today_year)
        print(f"{len(xera_lookup)} pitchers")
    except Exception as e:
        print(f"FAILED ({e}) — using raw ERA")

    # ── 2. Today's umpires + career stats ────────────────────────────────────
    print("  Loading HP umpire data...", end=' ', flush=True)
    ump_data = {}
    try:
        from data.umpires import get_today_umpires, get_umpire_stats
        ump_raw = get_today_umpires(today)
        for gid, info in ump_raw.items():
            stats = get_umpire_stats(info['name'], year=today_year)
            ump_data[gid] = {**info, **stats}
        print(f"{len(ump_data)} games assigned")
    except Exception as e:
        print(f"FAILED ({e}) — no umpire adjustment")

    # ── 3. Bullpen fatigue (last 3 days) ─────────────────────────────────────
    print("  Computing bullpen fatigue (last 3 days)...", end=' ', flush=True)
    fatigue = {}
    try:
        from data.bullpen import compute_bullpen_fatigue
        fatigue = compute_bullpen_fatigue(today=today)
        n_tired = sum(1 for v in fatigue.values() if v['rating'] in ('tired', 'exhausted'))
        print(f"{n_tired} teams tired/exhausted")
    except Exception as e:
        print(f"FAILED ({e}) — no bullpen adjustment")

    # ── 4. Fetch today's schedule + pitchers ─────────────────────────────────
    url = (f'https://statsapi.mlb.com/api/v1/schedule'
           f'?sportId=1&date={today}&hydrate=probablePitcher,team')
    sched = mlb_fetch(url)
    games_today = []
    for day in sched.get('dates', []):
        for g in day.get('games', []):
            status = g.get('status', {}).get('abstractGameState', '')
            if status == 'Final':
                continue
            away = g.get('teams', {}).get('away', {})
            home = g.get('teams', {}).get('home', {})
            rec = {
                'game_pk':     g.get('gamePk'),          # needed for ump lookup
                'away':        away.get('team', {}).get('abbreviation', '?'),
                'home':        home.get('team', {}).get('abbreviation', '?'),
                'away_pit':    away.get('probablePitcher', {}).get('fullName', 'TBD'),
                'home_pit':    home.get('probablePitcher', {}).get('fullName', 'TBD'),
                'away_pit_id': away.get('probablePitcher', {}).get('id'),
                'home_pit_id': home.get('probablePitcher', {}).get('id'),
            }
            games_today.append(rec)

    print(f"  Games found: {len(games_today)}")

    # Get pitcher quality (xERA preferred, raw ERA fallback)
    pit_cache: dict = {}
    pit_ids = set()
    for g in games_today:
        if g['away_pit_id']: pit_ids.add(g['away_pit_id'])
        if g['home_pit_id']: pit_ids.add(g['home_pit_id'])
    for pid in pit_ids:
        get_pitcher_quality(pid, xera_lookup=xera_lookup, cache=pit_cache)
        time.sleep(0.05)

    bets = qualify_and_build_bets(
        games_today,
        pit_stats=pit_cache,
        xera_lookup=xera_lookup,
        ump_data=ump_data,
        fatigue=fatigue,
    )

    if not bets:
        print("  No games qualify today under these strategy rules.")
        return

    # Fetch live alternate total odds from Odds API
    events_url = f'{BASE}/sports/{SPORT_KEY}/events?apiKey={ODDS_KEY}'
    try:
        events, _ = _fetch(events_url)
        event_map = {}
        for e in events:
            at = FULL_TO_ABBR.get(e['away_team'], e['away_team'][:3])
            ht = FULL_TO_ABBR.get(e['home_team'], e['home_team'][:3])
            event_map[(at, ht)] = e['id']
    except:
        event_map = {}

    _UMP_ICON = {'under': 'U', 'over': 'O', 'neutral': '-'}
    _BP_ICON  = {'fresh': 'F', 'normal': 'N', 'tired': 'T', 'exhausted': 'X'}

    print()
    print(f"  {'Game':<12} {'Exp':>5} {'Adj':>5} {'Bet':<12} {'Odds':>6} {'Model%':>7} {'Edge':>6}  "
          f"{'Ump':<16} BP(A/H)  Pitchers")
    print(f"  {'-'*100}")

    for b in bets:
        away, home = b['away'], b['home']
        # Try to get live odds
        eid = event_map.get((away, home))
        live_odds = None
        if eid:
            try:
                params = urllib.parse.urlencode({
                    'apiKey':   ODDS_KEY, 'regions': 'us',
                    'markets':  'alternate_totals',
                    'bookmakers': 'draftkings,fanduel,betmgm',
                    'oddsFormat': 'american',
                })
                data, _ = _fetch(f'{BASE}/sports/{SPORT_KEY}/events/{eid}/odds?{params}')
                target_line = b['line']
                best_price  = None
                for bm in data.get('bookmakers', []):
                    for mkt in bm.get('markets', []):
                        for out in mkt.get('outcomes', []):
                            nm    = (out.get('name') or '').strip().lower()
                            pt    = out.get('point')
                            price = out.get('price')
                            if nm == 'under' and pt == target_line and price is not None:
                                price_i = int(price)
                                if best_price is None or price_i > best_price:
                                    best_price = price_i
                if best_price and MIN_ODDS <= best_price <= MAX_ODDS:
                    live_odds = best_price
                time.sleep(0.15)
            except Exception:
                pass

        display_odds    = live_odds if live_odds else b['odds']
        display_implied = american_to_implied(display_odds) * 100
        live_tag        = '(L)' if live_odds else '(E)'
        sp_str          = f"{b['away_pit'].split()[-1]} vs {b['home_pit'].split()[-1]}"
        xera_str        = f"xERA:{b['away_sp_era']:.2f}/{b['home_sp_era']:.2f}"

        ump_icon  = _UMP_ICON.get(b['ump_impact'], '-')
        ump_label = f"{b['ump_name'].split()[-1]}[{ump_icon}]" if b['ump_name'] != 'TBD' else 'TBD'
        bp_label  = f"{_BP_ICON.get(b['bp_away_rating'],'?')}/{_BP_ICON.get(b['bp_home_rating'],'?')}"

        print(
            f"  {b['game']:<12} {b['exp_total']:>5.1f} {b['adj_total']:>5.1f} "
            f"{b['bet']:<12} {display_odds:>+5d}  {b['hist_p']*100:>6.1f}%  {b['edge']:>+5.1f}pp  "
            f"{ump_label:<16} {bp_label}  {sp_str} ({xera_str}){live_tag}"
        )

    print()
    print(f"  Qualifying games: {len(bets)}")
    print(f"  BP legend: F=fresh N=normal T=tired X=exhausted | Ump: U=under-lean O=over-lean -=neutral")


# ── 14-DAY BACKTEST ──────────────────────────────────────────────────────────
def run_backtest():
    print(f"\n{'='*72}")
    print(f"  ALT UNDER STRATEGY — 14-DAY BACKTEST  (Apr 28 – May 11, 2026)")
    print(f"  Strategy: exp total <= {EXP_THRESHOLD} | Under (exp+{MIN_BUFFER} to +{MAX_BUFFER}) | best edge per game")
    print(f"{'='*72}")

    # Load saved game data
    profile = os.environ.get('USERPROFILE','C:/Users/17146')
    games_path = os.path.join(profile,'bt_games.json')
    pits_path  = os.path.join(profile,'bt_pit_stats.json')
    if not os.path.exists(games_path):
        print("  Run fetch scripts first (bt_games.json not found)")
        return

    with open(games_path) as f:
        all_games = json.load(f)
    with open(pits_path) as f:
        pit_raw = json.load(f)
    pit_stats = {int(k):v for k,v in pit_raw.items()}

    # Group by date
    by_date = defaultdict(list)
    for g in all_games:
        by_date[g['date']].append(g)

    daily_log = []
    all_bets_log = []

    for d in sorted(by_date):
        day_games = by_date[d]
        bets = qualify_and_build_bets(day_games, pit_stats=pit_stats)

        for b in bets:
            # Find the actual game result
            actual = next(
                (g for g in day_games if g['away']==b['away'] and g['home']==b['home']), None
            )
            if not actual:
                continue
            actual_total = actual.get('total')
            if actual_total is None:
                continue

            hit   = actual_total <= b['line']
            dec   = 1.0 + 100.0 / abs(b['odds'])
            pnl   = FLAT_BET * dec - FLAT_BET if hit else -FLAT_BET

            b['date']         = d
            b['actual_total'] = actual_total
            b['hit']          = hit
            b['pnl']          = pnl
            all_bets_log.append(b)

        wins  = sum(1 for b in [x for x in all_bets_log if x['date']==d] if b['hit'])
        total_day = len([x for x in all_bets_log if x['date']==d])
        daily_log.append({'date':d,'bets':total_day,'wins':wins})

    if not all_bets_log:
        print("  No qualifying bets found in 14-day window.")
        return

    # ── Day-by-day log ───────────────────────────────────────────────────────
    print()
    print(f"  DAY-BY-DAY RESULTS")
    print(f"  {'Date':<12} {'Bets':>5} {'Wins':>5} {'PnL':>8}  Bets placed")
    print(f"  {'-'*70}")

    cum_pnl = 0
    for d in sorted(by_date):
        day_bets = [b for b in all_bets_log if b['date']==d]
        if not day_bets:
            print(f"  {d:<12} {'—':>5}  no qualifying games")
            continue
        day_pnl = sum(b['pnl'] for b in day_bets)
        cum_pnl += day_pnl
        wins = sum(1 for b in day_bets if b['hit'])
        bet_str = '  '.join(
            f"{b['bet']}@{b['game']}({'W' if b['hit'] else 'L'},{b['actual_total']})"
            for b in day_bets
        )
        pnl_str = f'+${day_pnl:.0f}' if day_pnl >= 0 else f'-${abs(day_pnl):.0f}'
        print(f"  {d:<12} {len(day_bets):>5} {wins:>5} {pnl_str:>8}  {bet_str}")

    # ── Summary stats ────────────────────────────────────────────────────────
    total_bets    = len(all_bets_log)
    total_wins    = sum(1 for b in all_bets_log if b['hit'])
    total_wagered = total_bets * FLAT_BET
    total_pnl     = sum(b['pnl'] for b in all_bets_log)
    total_return  = total_wagered + total_pnl
    hit_rate      = total_wins / total_bets * 100 if total_bets else 0
    roi           = total_pnl / total_wagered * 100 if total_wagered else 0
    avg_hist_p    = sum(b['hist_p'] for b in all_bets_log) / total_bets * 100
    avg_implied   = sum(b['implied'] for b in all_bets_log) / total_bets * 100
    avg_edge      = avg_hist_p - avg_implied
    avg_exp       = sum(b['exp_total'] for b in all_bets_log) / total_bets

    days_with_bet = len(set(b['date'] for b in all_bets_log))
    days_total    = len(by_date)

    print()
    print(f"  {'='*60}")
    print(f"  SUMMARY — {total_bets} bets over {days_total} days ({days_with_bet} with qualifying games)")
    print(f"  {'='*60}")
    print(f"  Win/Loss           : {total_wins}W - {total_bets-total_wins}L")
    print(f"  Actual hit rate    : {hit_rate:.1f}%")
    print(f"  Model projected    : {avg_hist_p:.1f}%  (2025 baseline Poisson)")
    print(f"  Book implied avg   : {avg_implied:.1f}%")
    print(f"  Average edge       : {avg_edge:+.1f}pp")
    print(f"  Avg expected total : {avg_exp:.1f} runs/game")
    print()
    print(f"  Total wagered      : ${total_wagered:,.0f}  (${FLAT_BET}/bet)")
    print(f"  Total returned     : ${total_return:,.0f}")
    print(f"  Net P&L            : ${total_pnl:+,.0f}")
    print(f"  ROI                : {roi:+.1f}%")
    print()

    # ── By line breakdown ─────────────────────────────────────────────────────
    by_line = defaultdict(lambda:{'bets':0,'wins':0,'pnl':0,'wag':0})
    for b in all_bets_log:
        r = by_line[b['bet']]
        r['bets']+=1; r['wins']+=int(b['hit']); r['pnl']+=b['pnl']; r['wag']+=FLAT_BET

    print(f"  BY BET TYPE:")
    print(f"  {'Bet':<14} {'Bets':>5} {'Wins':>5} {'Hit%':>7} {'Wagered':>9} {'P&L':>8} {'ROI':>7}")
    print(f"  {'-'*58}")
    for line_label, r in sorted(by_line.items()):
        hr  = r['wins']/r['bets']*100 if r['bets'] else 0
        roi_l = r['pnl']/r['wag']*100 if r['wag'] else 0
        print(f"  {line_label:<14} {r['bets']:>5} {r['wins']:>5} {hr:>6.1f}%  ${r['wag']:>7,.0f}  ${r['pnl']:>+6,.0f}  {roi_l:>+6.1f}%")

    # ── Worst/best days ───────────────────────────────────────────────────────
    print()
    print(f"  BIGGEST SINGLE-DAY SWINGS:")
    day_pnls = defaultdict(float)
    for b in all_bets_log:
        day_pnls[b['date']] += b['pnl']
    sorted_days = sorted(day_pnls.items(), key=lambda x: x[1])
    worst = sorted_days[:3]
    best  = sorted_days[-3:]
    for d,p in worst:
        print(f"    Worst  {d}: ${p:+.0f}")
    for d,p in reversed(best):
        print(f"    Best   {d}: ${p:+.0f}")

    # ── Calibration check ─────────────────────────────────────────────────────
    print()
    print(f"  CALIBRATION (model projected vs actual):")
    buckets = [(0.60,0.70),(0.70,0.75),(0.75,0.80),(0.80,0.85),(0.85,0.95)]
    print(f"  {'Prob bucket':<20} {'N':>4} {'Model%':>8} {'Actual%':>8} {'Diff':>7}")
    for lo,hi in buckets:
        group = [b for b in all_bets_log if lo<=b['hist_p']<hi]
        if not group: continue
        mdl = sum(b['hist_p'] for b in group)/len(group)*100
        act = sum(int(b['hit']) for b in group)/len(group)*100
        diff = act-mdl
        print(f"  {lo:.0%}–{hi:.0%}              {len(group):>4} {mdl:>7.1f}%  {act:>7.1f}%  {diff:>+6.1f}pp")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'both'
    if mode in ('both', '--today', 'today'):
        run_today()
    if mode in ('both', '--bt', 'bt'):
        run_backtest()
