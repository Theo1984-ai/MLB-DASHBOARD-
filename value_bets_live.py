"""
Live Alt Under Value Bets — Uses real DraftKings alt line prices
================================================================
- Fetches DK alternate_totals prices via Odds API (live)
- Computes real edge: Poisson model% minus actual DK implied%
- Caches to JSON so re-runs don't burn credits
- Filters: -200 to -1000 range, positive edge only
"""
import json, math, time, urllib.request, os
from datetime import datetime

DATE       = datetime.now().strftime('%Y-%m-%d')
CACHE_FILE = f'C:\\Users\\17146\\MLB homerun\\cache\\odds_{DATE}.json'
ODDS_KEY   = '328b6a86d5bd586ca057559e63a2d006'
BASE       = 'https://api.the-odds-api.com/v4'
SPORT      = 'baseball_mlb'
LEAGUE_ERA = 4.30
MIN_ODDS   = -1000
MAX_ODDS   = -200
MIN_EDGE   = 1.0   # pp minimum

# ── 2025 last-season baseline ─────────────────────────────────────────────────
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
ABBR = {
    'New York Yankees':'NYY','Baltimore Orioles':'BAL','Boston Red Sox':'BOS',
    'Philadelphia Phillies':'PHI','Chicago Cubs':'CHC','Atlanta Braves':'ATL',
    'Kansas City Royals':'KC','Chicago White Sox':'CWS','Arizona Diamondbacks':'AZ',
    'Texas Rangers':'TEX','San Francisco Giants':'SF','Los Angeles Dodgers':'LAD',
    'St. Louis Cardinals':'STL','Oakland Athletics':'ATH','Seattle Mariners':'SEA',
    'Houston Astros':'HOU','Los Angeles Angels':'LAA','Cleveland Guardians':'CLE',
    'San Diego Padres':'SD','Milwaukee Brewers':'MIL','Detroit Tigers':'DET',
    'New York Mets':'NYM','Tampa Bay Rays':'TB','Toronto Blue Jays':'TOR',
    'Colorado Rockies':'COL','Pittsburgh Pirates':'PIT','Washington Nationals':'WSH',
    'Cincinnati Reds':'CIN','Minnesota Twins':'MIN','Miami Marlins':'MIA',
}

os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent':'mlb/1.0'})
    with urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
        remaining = r.headers.get('x-requests-remaining', '?')
        data = json.loads(r.read())
    return data, remaining

def exp_runs(off, def_r, sp_era):
    return max(1.5, (off + def_r) / 2.0 + (sp_era - LEAGUE_ERA) * 0.22)

def poisson_p(lam, k):
    return (lam**k * math.exp(-lam)) / math.factorial(k)

def p_under(lam, line):
    """P(total < line) — uses int(line) as ceiling since baseball is integer."""
    return sum(poisson_p(lam, k) for k in range(int(line) + 1))

def implied(odds):
    if odds == 0: return 0.5
    return abs(odds) / (abs(odds) + 100) if odds < 0 else 100 / (odds + 100)

def fair_am(p):
    p = max(0.001, min(p, 0.999))
    return int(round(-p / (1 - p) * 100)) if p >= 0.5 else int(round((1 - p) / p * 100))

def get_era(pid, cache={}):
    if not pid: return LEAGUE_ERA
    if pid in cache: return cache[pid]
    try:
        url = (f'https://statsapi.mlb.com/api/v1/people/{pid}/stats'
               f'?stats=season&season=2026&group=pitching&gameType=R')
        req = urllib.request.Request(url, headers={'User-Agent':'mlb/1.0'})
        with urllib.request.urlopen(req, timeout=8, context=_UNVERIFIED_SSL) as r:
            d = json.loads(r.read())
        splits = d.get('stats',[{}])[0].get('splits',[])
        if splits:
            s   = splits[0]['stat']
            ip  = float(s.get('inningsPitched',0) or 0)
            era = float(s.get('era',0) or 0)
            result = era if ip >= 5 else LEAGUE_ERA
            cache[pid] = result
            return result
    except: pass
    cache[pid] = LEAGUE_ERA
    return LEAGUE_ERA

# ── Load from cache or fetch fresh ───────────────────────────────────────────
if os.path.exists(CACHE_FILE):
    print(f'Loading cached odds from {CACHE_FILE}')
    with open(CACHE_FILE) as f:
        cache_data = json.load(f)
    events_today = cache_data['events']
    alt_odds     = cache_data['alt_odds']
    credits_used = cache_data.get('credits_used', '?')
    print(f'  Cached. Credits used on fetch: {credits_used}')
else:
    print(f'Fetching live odds from DraftKings (date: {DATE})...')

    # Step 1: Get today's event IDs (1 credit)
    ev_url = f'{BASE}/sports/{SPORT}/events?apiKey={ODDS_KEY}&dateFormat=iso'
    all_events, rem = fetch(ev_url)
    events_today = [e for e in all_events if DATE in e['commence_time']]
    print(f'  {len(events_today)} games today  |  Credits remaining: {rem}')

    # Step 2: Get schedule + pitcher IDs (free, MLB API)
    sched_url = (f'https://statsapi.mlb.com/api/v1/schedule'
                 f'?sportId=1&date={DATE}&hydrate=probablePitcher,team')
    req = urllib.request.Request(sched_url, headers={'User-Agent':'mlb/1.0'})
    with urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
        sched = json.loads(r.read())

    pit_map = {}
    for day in sched.get('dates', []):
        for g in day.get('games', []):
            if g.get('status',{}).get('abstractGameState') == 'Final': continue
            aw, hm = g['teams']['away'], g['teams']['home']
            at = ABBR.get(aw['team']['name'])
            ht = ABBR.get(hm['team']['name'])
            if at and ht:
                pit_map[(at,ht)] = (
                    aw.get('probablePitcher',{}).get('id'),
                    hm.get('probablePitcher',{}).get('id'),
                    aw.get('probablePitcher',{}).get('fullName','TBD'),
                    hm.get('probablePitcher',{}).get('fullName','TBD'),
                )

    # Step 3: Fetch pitcher ERAs (free, MLB Stats API)
    print('  Fetching pitcher ERAs...')
    for apid, hpid, _, _ in pit_map.values():
        for pid in [apid, hpid]:
            if pid: get_era(pid); time.sleep(0.05)

    # Step 4: Compute exp_total for each game, fetch alt odds for all
    print('  Fetching DK alternate_totals for all games...')
    alt_odds = {}
    credits_start = int(rem) if rem != '?' else 0

    for ev in events_today:
        at = ABBR.get(ev['away_team'])
        ht = ABBR.get(ev['home_team'])
        if not at or not ht: continue
        if at not in rpg25 or ht not in rpg25: continue

        eid = ev['id']
        url = (f'{BASE}/sports/{SPORT}/events/{eid}/odds'
               f'?apiKey={ODDS_KEY}&regions=us'
               f'&markets=alternate_totals,h2h,spreads'
               f'&oddsFormat=american&bookmakers=draftkings')
        try:
            d, rem = fetch(url)
            alt_odds[eid] = {'ev': ev, 'data': d}
            time.sleep(0.15)
        except Exception as ex:
            print(f'    Warning: {at}@{ht} failed — {ex}')

    print(f'  Done. Credits remaining: {rem}')

    # Save cache
    cache_data = {
        'date': DATE,
        'events': events_today,
        'alt_odds': alt_odds,
        'pit_map': {f'{k[0]}@{k[1]}': list(v) for k,v in pit_map.items()},
        'credits_used': str(credits_start - int(rem)) if rem != '?' else '?'
    }
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache_data, f, indent=2)
    print(f'  Cached to {CACHE_FILE}')
    credits_used = cache_data['credits_used']

# Reload pit_map from cache if needed
if 'pit_map' not in dir():
    raw_pm = cache_data.get('pit_map', {})
    pit_map = {}
    for k, v in raw_pm.items():
        at, ht = k.split('@')
        pit_map[(at,ht)] = tuple(v)

# Re-fetch ERAs if coming from cache
era_cache = {}
for (at,ht), (apid,hpid,_,_) in pit_map.items():
    for pid in [apid,hpid]:
        if pid and pid not in era_cache:
            era_cache[pid] = get_era(pid)

# ── Compute real edge for every alt under line ────────────────────────────────
bets = []

for eid, obj in alt_odds.items():
    ev   = obj['ev']
    data = obj['data']
    at   = ABBR.get(ev['away_team'])
    ht   = ABBR.get(ev['home_team'])
    if not at or not ht: continue

    pits    = pit_map.get((at,ht), (None,None,'TBD','TBD'))
    apid, hpid, anam, hnam = pits

    away_era = era_cache.get(apid, LEAGUE_ERA)
    home_era = era_cache.get(hpid, LEAGUE_ERA)
    lam_a    = exp_runs(rpg25[at], rapg25[ht], home_era)
    lam_b    = exp_runs(rpg25[ht], rapg25[at], away_era)
    exp_tot  = lam_a + lam_b

    # Find DK alt totals
    dk_unders = {}   # point -> price
    for bk in data.get('bookmakers', []):
        if bk['key'] != 'draftkings': continue
        for mkt in bk.get('markets', []):
            if mkt['key'] == 'alternate_totals':
                for o in mkt['outcomes']:
                    if o['name'] == 'Under':
                        dk_unders[o['point']] = o['price']

    if not dk_unders: continue

    for line, dk_price in sorted(dk_unders.items()):
        if dk_price > MAX_ODDS or dk_price < MIN_ODDS: continue  # must be -200 to -1000

        model_p  = p_under(exp_tot, line)
        dk_imp   = implied(dk_price)
        edge     = (model_p - dk_imp) * 100
        if edge < MIN_EDGE: continue

        fa = fair_am(model_p)

        bets.append({
            'game':     f'{at}@{ht}',
            'bet':      f'Under {line}',
            'away_sp':  anam.split()[-1] if anam != 'TBD' else 'TBD',
            'home_sp':  hnam.split()[-1] if hnam != 'TBD' else 'TBD',
            'away_era': away_era,
            'home_era': home_era,
            'exp_tot':  round(exp_tot, 2),
            'model_p':  round(model_p, 4),
            'fair_am':  fa,
            'dk_price': dk_price,
            'dk_imp':   round(dk_imp * 100, 1),
            'edge_pp':  round(edge, 1),
        })

# Dedup: best edge per (game, bet)
seen = {}
for b in bets:
    k = (b['game'], b['bet'])
    if k not in seen or b['edge_pp'] > seen[k]['edge_pp']:
        seen[k] = b

final = sorted(seen.values(), key=lambda x: -x['edge_pp'])

# ── Print ─────────────────────────────────────────────────────────────────────
print()
print('=' * 115)
print(f"  LIVE ALT UNDER VALUE BETS — {DATE}  (DraftKings real prices)")
print(f"  Model: 2025 RPG/RAPG  |  ERA: live 2026  |  Edge = Model% minus DK implied%")
print(f"  Odds filter: {MAX_ODDS} to {MIN_ODDS}  |  Min edge: {MIN_EDGE}pp  |  Credits used: {credits_used}")
print('=' * 115)
print()
print(f"  {'#':<3} {'Game':<13} {'Bet':<14} {'Starters':<36} "
      f"{'ExpTot':>7} {'Model%':>7} {'Fair':>8} {'DK Price':>9} {'DK Imp%':>8} {'Edge':>7}")
print(f"  {'-'*113}")

for i, b in enumerate(final, 1):
    sp = f"{b['away_sp']}({b['away_era']:.2f}) v {b['home_sp']}({b['home_era']:.2f})"
    print(f"  {i:<3} {b['game']:<13} {b['bet']:<14} {sp:<36} "
          f"{b['exp_tot']:>7.2f} {b['model_p']*100:>6.1f}% "
          f"{b['fair_am']:>+8d} {b['dk_price']:>+9d} "
          f"{b['dk_imp']:>7.1f}% {b['edge_pp']:>+6.1f}pp")

print()
print('=' * 115)
print(f"  {len(final)} positive-edge bets found")
print()

# ── Best parlay (1 leg per game, max 5, by Kelly growth) ──────────────────────
def kelly_growth(p, dk_odds):
    dec = 1 + 100/abs(dk_odds) if dk_odds < 0 else 1 + dk_odds/100
    b = dec - 1
    if b <= 0: return -999, 0
    k = max(0, min((b*p-(1-p))/b, 1.0))
    gr = p*math.log(1+k*b) + (1-p)*math.log(max(1e-12, 1-k))
    return gr, dec

# Best line per game
best_per_game = {}
for b in final:
    gr, _ = kelly_growth(b['model_p'], b['dk_price'])
    if b['game'] not in best_per_game or gr > best_per_game[b['game']][1]:
        best_per_game[b['game']] = (b, gr)

pool = sorted([v[0] for v in best_per_game.values()],
              key=lambda x: -kelly_growth(x['model_p'], x['dk_price'])[0])

from itertools import combinations as combs

best_parlays = []
for n in range(2, min(6, len(pool)+1)):
    for combo in combs(pool, n):
        p = 1.0; dec = 1.0
        for leg in combo:
            p   *= leg['model_p']
            _, d = kelly_growth(leg['model_p'], leg['dk_price'])
            dec *= d
        b_val = dec - 1
        k = max(0, min((b_val*p-(1-p))/b_val, 1.0))
        ev = p*b_val*100 - (1-p)*100
        gr = p*math.log(1+k*b_val) + (1-p)*math.log(max(1e-12,1-k))
        payout_am = int((dec-1)*100) if dec >= 2 else int(-100/(dec-1))
        best_parlays.append((gr, list(combo), p, dec, payout_am, ev, k))

best_parlays.sort(key=lambda x: -x[0])

if best_parlays:
    print('=' * 115)
    print('  OPTIMAL PARLAY  (best Kelly growth, real DK prices, 1 leg per game)')
    print('=' * 115)

    gr, legs, p, dec, payout, ev, k = best_parlays[0]
    print()
    print(f"  {len(legs)}-Leg Alt Under Parlay")
    print()
    for i, leg in enumerate(legs, 1):
        print(f"  {i}. {leg['game']:<13} {leg['bet']:<14}  "
              f"model {leg['model_p']*100:.1f}%  DK {leg['dk_price']:+d}  "
              f"edge +{leg['edge_pp']:.1f}pp")
    print()
    print(f"  Combined win probability : {p*100:.1f}%")
    print(f"  Actual DK payout         : {payout:+d}")
    print(f"  EV per $100              : ${ev:+.0f}")
    print(f"  Kelly fraction           : {k*100:.1f}%")
    print()
    for stake in [25, 50, 100]:
        print(f"  ${stake} bet  -->  wins ${stake*(dec-1):.0f}  (total ${stake*dec:.0f})")
    print()
    print('  Top 5 combos by Kelly growth:')
    for rank, (gr2, legs2, p2, dec2, pay2, ev2, k2) in enumerate(best_parlays[:5], 1):
        names = ' + '.join(f"{l['game']} {l['bet']}" for l in legs2)
        print(f"  #{rank} {len(legs2)}-leg  Win:{p2*100:.1f}%  Pay:{pay2:+d}  "
              f"EV:${ev2:+.0f}  Kelly:{k2*100:.1f}%  Growth:{gr2*100:.2f}%")
        print(f"       {names}")

print()
print('=' * 115)
