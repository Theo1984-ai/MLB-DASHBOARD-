"""Quick spread + moneyline value finder for today's slate."""
import json, urllib.request, time, math

ODDS_KEY = '328b6a86d5bd586ca057559e63a2d006'
BASE     = 'https://api.the-odds-api.com/v4'
SPORT    = 'baseball_mlb'

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
LEAGUE_ERA = 4.30


def exp_runs(off, def_r, sp_era):
    return max(1.5, (off + def_r) / 2.0 + (sp_era - LEAGUE_ERA) * 0.22)


def win_prob(lam_a, lam_b):
    """P(away wins) via Poisson — ignores ties for simplicity."""
    p = 0.0
    for a in range(20):
        pa = (lam_a**a * math.exp(-lam_a)) / math.factorial(a)
        for b in range(a):
            pb = (lam_b**b * math.exp(-lam_b)) / math.factorial(b)
            p += pa * pb
    return p


def implied(odds):
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def get_era(pid, season='2026'):
    if not pid:
        return LEAGUE_ERA
    try:
        url = (f'https://statsapi.mlb.com/api/v1/people/{pid}/stats'
               f'?stats=season&season={season}&group=pitching&gameType=R')
        req = urllib.request.Request(url, headers={'User-Agent': 'mlb/1.0'})
        with urllib.request.urlopen(req, timeout=8, context=_UNVERIFIED_SSL) as r:
            d = json.loads(r.read())
        splits = d.get('stats', [{}])[0].get('splits', [])
        if splits:
            s   = splits[0].get('stat', {})
            ip  = float(s.get('inningsPitched', '0') or '0')
            era = float(s.get('era', '0') or '0')
            return era if ip >= 5 else LEAGUE_ERA
    except Exception:
        pass
    return LEAGUE_ERA


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'mlb/1.0'})
    with urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
        return json.loads(r.read())


# ── Fetch odds ────────────────────────────────────────────────────────────────
print('Fetching odds...')
odds_url = (f'{BASE}/sports/{SPORT}/odds/?apiKey={ODDS_KEY}'
            f'&regions=us&markets=spreads,h2h&oddsFormat=american')
games_odds = fetch(odds_url)

# ── Fetch schedule + probable pitchers ───────────────────────────────────────
print('Fetching schedule...')
sched = fetch('https://statsapi.mlb.com/api/v1/schedule'
              '?sportId=1&date=2026-05-13&hydrate=probablePitcher,team')

pit_map = {}  # 'AT@HT' -> (apid, hpid, aname, hname)
for day in sched.get('dates', []):
    for g in day.get('games', []):
        if g.get('status', {}).get('abstractGameState') == 'Final':
            continue
        aw  = g['teams']['away']
        hm  = g['teams']['home']
        at  = ABBR.get(aw['team']['name'])
        ht  = ABBR.get(hm['team']['name'])
        if at and ht:
            apid = aw.get('probablePitcher', {}).get('id')
            hpid = hm.get('probablePitcher', {}).get('id')
            anam = aw.get('probablePitcher', {}).get('fullName', 'TBD')
            hnam = hm.get('probablePitcher', {}).get('fullName', 'TBD')
            pit_map[f'{at}@{ht}'] = (apid, hpid, anam, hnam)

# ── Fetch ERAs ────────────────────────────────────────────────────────────────
print('Fetching pitcher ERAs...')
era_cache = {}
for key, (apid, hpid, _, _) in pit_map.items():
    for pid in [apid, hpid]:
        if pid and pid not in era_cache:
            era_cache[pid] = get_era(pid)
            time.sleep(0.06)

# ── Build results ─────────────────────────────────────────────────────────────
results = []
for game in games_odds:
    ht = ABBR.get(game['home_team'])
    at = ABBR.get(game['away_team'])
    if not ht or not at:
        continue
    if at not in rpg25 or ht not in rpg25:
        continue

    key  = f'{at}@{ht}'
    pits = pit_map.get(key, (None, None, 'TBD', 'TBD'))
    apid, hpid, aname, hname = pits
    away_era = era_cache.get(apid, LEAGUE_ERA)
    home_era = era_cache.get(hpid, LEAGUE_ERA)

    lam_a = exp_runs(rpg25[at], rapg25[ht], home_era)
    lam_b = exp_runs(rpg25[ht], rapg25[at], away_era)
    diff  = lam_a - lam_b   # positive = away model-favored

    mdl_away_wp = win_prob(lam_a, lam_b)
    mdl_home_wp = 1 - mdl_away_wp

    # Best odds across books
    spread_apt = spread_aod = spread_hpt = spread_hod = None
    ml_away = ml_home = None
    for bk in game.get('bookmakers', []):
        for mkt in bk.get('markets', []):
            if mkt['key'] == 'spreads' and spread_apt is None:
                for o in mkt['outcomes']:
                    ab = ABBR.get(o['name'])
                    if ab == at:
                        spread_apt = o['point']; spread_aod = o['price']
                    elif ab == ht:
                        spread_hpt = o['point']; spread_hod = o['price']
            if mkt['key'] == 'h2h' and ml_away is None:
                for o in mkt['outcomes']:
                    ab = ABBR.get(o['name'])
                    if ab == at:
                        ml_away = o['price']
                    elif ab == ht:
                        ml_home = o['price']
        if spread_apt is not None and ml_away is not None:
            break

    if ml_away is None:
        continue

    bk_away_wp = implied(ml_away)
    bk_home_wp = implied(ml_home) if ml_home else 1 - bk_away_wp
    ml_edge_away = (mdl_away_wp - bk_away_wp) * 100
    ml_edge_home = (mdl_home_wp - bk_home_wp) * 100

    # Best ML edge (whichever side model likes)
    if ml_edge_away >= ml_edge_home:
        best_ml_side = at
        best_ml_odds = ml_away
        best_ml_edge = ml_edge_away
    else:
        best_ml_side = ht
        best_ml_odds = ml_home
        best_ml_edge = ml_edge_home

    results.append({
        'key': key, 'at': at, 'ht': ht,
        'aname': aname, 'hname': hname,
        'away_era': away_era, 'home_era': home_era,
        'lam_a': lam_a, 'lam_b': lam_b, 'diff': diff,
        'spread': f'{spread_apt:+.1f}' if spread_apt is not None else 'N/A',
        'spread_odds': spread_aod,
        'ml_away': ml_away, 'ml_home': ml_home,
        'mdl_away': mdl_away_wp * 100,
        'mdl_home': mdl_home_wp * 100,
        'bk_away':  bk_away_wp  * 100,
        'bk_home':  bk_home_wp  * 100,
        'ml_edge_away': ml_edge_away,
        'ml_edge_home': ml_edge_home,
        'best_ml_side': best_ml_side,
        'best_ml_odds': best_ml_odds,
        'best_ml_edge': best_ml_edge,
    })

results.sort(key=lambda x: -x['best_ml_edge'])

# ── Print ─────────────────────────────────────────────────────────────────────
print()
print('=' * 110)
print(f'  TODAY\'S MLB VALUE PLAYS  (2026-05-13)')
print('=' * 110)
print(f'  {"Game":<13}  {"Away SP":<16} {"Home SP":<16}  '
      f'{"Exp A":>6} {"Exp H":>6} {"Diff":>5}  '
      f'{"Spread":>7} {"ML":>6}  '
      f'{"Model%":>7} {"Book%":>7} {"Edge":>6}  {"Bet"}')
print('-' * 110)

VALUE_EDGE = 5.0   # pp minimum to flag as value

for r in results:
    flag = ' <<< VALUE' if r['best_ml_edge'] >= VALUE_EDGE else ''
    side = r['best_ml_side']
    # show model% and book% for the best side
    if side == r['at']:
        mdl_pct = r['mdl_away']
        bk_pct  = r['bk_away']
    else:
        mdl_pct = r['mdl_home']
        bk_pct  = r['bk_home']

    print(f"  {r['key']:<13}  {r['aname'].split()[-1]:<16} {r['hname'].split()[-1]:<16}"
          f"  {r['lam_a']:>6.2f} {r['lam_b']:>6.2f} {r['diff']:>+5.2f}"
          f"  {r['spread']:>7} {str(r['spread_odds']):>6}"
          f"  {mdl_pct:>6.1f}% {bk_pct:>6.1f}% {r['best_ml_edge']:>+6.1f}pp"
          f"  {side} ML {r['best_ml_odds']:+d}{flag}")

print()
print('Value plays (edge >= 5pp):')
value = [r for r in results if r['best_ml_edge'] >= VALUE_EDGE]
if not value:
    print('  None found today.')
else:
    for r in value:
        side = r['best_ml_side']
        odds = r['best_ml_odds']
        print(f"  {r['key']}  {side} ML {odds:+d}  "
              f"Model {r['mdl_away'] if side==r['at'] else r['mdl_home']:.1f}%  "
              f"Book {r['bk_away'] if side==r['at'] else r['bk_home']:.1f}%  "
              f"Edge {r['best_ml_edge']:+.1f}pp  "
              f"({r['aname'].split()[-1]} vs {r['hname'].split()[-1]})")
