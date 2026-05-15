"""
Alternate Run Line Value Bets — same rules as value_bets.py
Odds: -200 to -1000 | Edge = model% minus book implied% | Positive only
Alt lines: fav -2.5 and -3.5 (standard -1.5 included when book misprices it)
"""
import json, math, time, urllib.request

DATE       = '2026-05-13'
LEAGUE_ERA = 4.30
MIN_ODDS   = -1000
MAX_ODDS   = -200
MIN_EDGE   = 0.5

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

# This morning's book lines
BOOK_LINES = {
    ('NYY','BAL'): {'ml_a':-178,'ml_h':+150,'rl_a_pt':-1.5,'rl_a_od':-108,'total':8.5,'u_od':-115},
    ('PHI','BOS'): {'ml_a':+118,'ml_h':-132,'rl_a_pt':+1.5,'rl_a_od':-178,'total':9.0,'u_od':-110},
    ('COL','PIT'): {'ml_a':+120,'ml_h':-140,'rl_a_pt':+1.5,'rl_a_od':-140,'total':8.0,'u_od':-115},
    ('LAA','CLE'): {'ml_a':+148,'ml_h':-156,'rl_a_pt':+1.5,'rl_a_od':-166,'total':7.5,'u_od':-110},
    ('DET','NYM'): {'ml_a':+114,'ml_h':-110,'rl_a_pt':-1.5,'rl_a_od':+158,'total':8.5,'u_od':-110},
    ('CHC','ATL'): {'ml_a':-117,'ml_h':+122,'rl_a_pt':-1.5,'rl_a_od':+116,'total':8.5,'u_od':-105},
    ('KC','CWS'):  {'ml_a':-124,'ml_h':-104,'rl_a_pt':-1.5,'rl_a_od':+146,'total':7.5,'u_od':-115},
    ('AZ','TEX'):  {'ml_a':+111,'ml_h':-122,'rl_a_pt':+1.5,'rl_a_od':-200,'total':9.0,'u_od':-110},
    ('SF','LAD'):  {'ml_a':+205,'ml_h':-106,'rl_a_pt':+1.5,'rl_a_od':-106,'total':7.5,'u_od':-115},
    ('TB','TOR'):  {'ml_a':+141,'ml_h':-154,'rl_a_pt':+1.5,'rl_a_od':-154,'total':8.5,'u_od':-110},
    ('SEA','HOU'): {'ml_a':-124,'ml_h':+128,'rl_a_pt':-1.5,'rl_a_od':+128,'total':9.0,'u_od':-105},
    ('MIA','MIN'): {'ml_a':-124,'ml_h':+136,'rl_a_pt':-1.5,'rl_a_od':+136,'total':9.0,'u_od':-105},
    ('STL','ATH'): {'ml_a':+100,'ml_h':-115,'rl_a_pt':+1.5,'rl_a_od':-120,'total':9.0,'u_od':-110},
    ('WSH','CIN'): {'ml_a':+108,'ml_h':-158,'rl_a_pt':+1.5,'rl_a_od':-150,'total':9.5,'u_od':-110},
    ('SD','MIL'):  {'ml_a':+125,'ml_h':-146,'rl_a_pt':+1.5,'rl_a_od':-176,'total':7.5,'u_od':-115},
}

def exp_runs(off, def_r, sp_era):
    return max(1.5, (off + def_r) / 2.0 + (sp_era - LEAGUE_ERA) * 0.22)

def poisson_p(lam, k):
    return (lam**k * math.exp(-lam)) / math.factorial(k)

def p_rl(lam_a, lam_b, spread, side='away', max_r=25):
    p = 0.0
    for a in range(max_r):
        pa = poisson_p(lam_a, a)
        for b in range(max_r):
            diff = (a - b) if side == 'away' else (b - a)
            if diff > spread:
                p += pa * poisson_p(lam_b, b)
    return p

def implied(odds):
    if odds == 0: return 0.5
    if odds < 0:  return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)

def true_p(imp_a, imp_b):
    return imp_a / (imp_a + imp_b)

def fair_am(p):
    p = max(0.001, min(p, 0.999))
    if p >= 0.5: return int(round(-p / (1 - p) * 100))
    return int(round((1 - p) / p * 100))

def get_era(pid):
    if not pid: return LEAGUE_ERA
    try:
        url = (f'https://statsapi.mlb.com/api/v1/people/{pid}/stats'
               f'?stats=season&season=2026&group=pitching&gameType=R')
        req = urllib.request.Request(url, headers={'User-Agent':'mlb/1.0'})
        with urllib.request.urlopen(req, timeout=8, context=_UNVERIFIED_SSL) as r:
            d = json.loads(r.read())
        splits = d.get('stats',[{}])[0].get('splits',[])
        if splits:
            s = splits[0]['stat']
            ip  = float(s.get('inningsPitched', 0) or 0)
            era = float(s.get('era', 0) or 0)
            return era if ip >= 5 else LEAGUE_ERA
    except: pass
    return LEAGUE_ERA

print(f'Alt Spread Value Bets — {DATE}')
print('Fetching pitcher ERAs...')

sched_url = (f'https://statsapi.mlb.com/api/v1/schedule'
             f'?sportId=1&date={DATE}&hydrate=probablePitcher,team')
req = urllib.request.Request(sched_url, headers={'User-Agent':'mlb/1.0'})
with urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
    sched = json.loads(r.read())

pit_map = {}
for day in sched.get('dates', []):
    for g in day.get('games', []):
        if g.get('status', {}).get('abstractGameState') == 'Final': continue
        aw, hm = g['teams']['away'], g['teams']['home']
        at = ABBR.get(aw['team']['name'])
        ht = ABBR.get(hm['team']['name'])
        if at and ht:
            pit_map[(at, ht)] = (
                aw.get('probablePitcher', {}).get('id'),
                hm.get('probablePitcher', {}).get('id'),
                aw.get('probablePitcher', {}).get('fullName', 'TBD'),
                hm.get('probablePitcher', {}).get('fullName', 'TBD'),
            )

era_cache = {}
for apid, hpid, _, _ in pit_map.values():
    for pid in [apid, hpid]:
        if pid and pid not in era_cache:
            era_cache[pid] = get_era(pid)
            time.sleep(0.06)

print(f'  {len(era_cache)} ERAs loaded\n')

bets = []

for (at, ht), bl in BOOK_LINES.items():
    if at not in rpg25 or ht not in rpg25: continue
    pits = pit_map.get((at, ht), (None, None, 'TBD', 'TBD'))
    apid, hpid, anam, hnam = pits
    if 'TBD' in (anam, hnam): continue   # skip TBD starters

    away_era = era_cache.get(apid, LEAGUE_ERA)
    home_era = era_cache.get(hpid, LEAGUE_ERA)
    lam_a = exp_runs(rpg25[at], rapg25[ht], home_era)
    lam_b = exp_runs(rpg25[ht], rapg25[at], away_era)

    # Model favorite
    fav = at if lam_a >= lam_b else ht
    fs  = 'away' if fav == at else 'home'
    fav_lam = lam_a if fs == 'away' else lam_b
    dog_lam = lam_b if fs == 'away' else lam_a
    diff = fav_lam - dog_lam

    # Book's standard RL — determine fav's side
    # rl_a_pt is the away spread. Negative = away is fav giving runs.
    if bl['rl_a_pt'] < 0:
        # Away team is book's RL fav
        book_fav_side = 'away'
        std_rl_od = bl['rl_a_od']   # odds for away -1.5
    else:
        # Home team is book's RL fav
        book_fav_side = 'home'
        # Estimate home -1.5 odds from away +1.5 odds (complement)
        imp_away_plus = implied(bl['rl_a_od'])
        std_rl_od = fair_am(1 - imp_away_plus + 0.04)

    # Standard RL implied probability for MODEL fav
    imp_std_rl = implied(std_rl_od) if book_fav_side == fs else (1 - implied(std_rl_od) + 0.04)
    imp_std_rl = max(0.10, min(imp_std_rl, 0.95))

    # True (no-vig) std RL prob for model fav
    # Use ML to remove vig
    imp_ml_a = implied(bl['ml_a'])
    imp_ml_h = implied(bl['ml_h'])
    true_ml_fav = true_p(imp_ml_a, imp_ml_h) if fs == 'away' else true_p(imp_ml_h, imp_ml_a)

    # Model probability at standard RL for fav (-1.5)
    model_p_std = p_rl(lam_a, lam_b, 1.5, side=fs)   # always 1.5: fav wins by 2+

    # Calibration ratio
    if model_p_std > 0.05:
        calib = imp_std_rl / model_p_std
    else:
        calib = 0.6

    dog = ht if fav == at else at

    # ── Both directions: fav giving, dog getting ─────────────────────────────
    # (margin, label, is_fav_side)
    candidates = [
        (1.5,  f'{fav} -1.5', True),
        (2.5,  f'{fav} -2.5', True),
        (3.5,  f'{fav} -3.5', True),
        (1.5,  f'{dog} +1.5', False),
        (2.5,  f'{dog} +2.5', False),
        (3.5,  f'{dog} +3.5', False),
    ]

    for margin, bet_label, is_fav in candidates:
        if is_fav:
            model_p = p_rl(lam_a, lam_b, margin, side=fs)          # P(fav wins by margin+)
        else:
            model_p = 1.0 - p_rl(lam_a, lam_b, margin, side=fs)   # P(dog covers)

        if model_p < 0.667 or model_p > 0.91: continue
        fa = fair_am(model_p)
        if fa < MIN_ODDS or fa > MAX_ODDS: continue

        # Book implied via calibration
        if is_fav:
            book_imp = min(max(model_p * calib, 0.30), 0.95)
        else:
            fav_p = p_rl(lam_a, lam_b, margin, side=fs)
            book_imp_fav = min(max(fav_p * calib, 0.05), 0.90)
            book_imp = max(1.0 - book_imp_fav, 0.30)
            book_imp = min(book_imp, 0.95)

        edge = (model_p - book_imp) * 100
        if edge < MIN_EDGE: continue

        label = 'Std RL' if margin == 1.5 else 'Alt RL'
        bets.append({
            'type':    label,
            'game':    f'{at}@{ht}',
            'bet':     bet_label,
            'away_sp': anam.split()[-1],
            'home_sp': hnam.split()[-1],
            'away_era':away_era,
            'home_era':home_era,
            'diff':    diff,
            'model_p': model_p,
            'fair_am': fa,
            'book_est':fair_am(book_imp),
            'edge_pp': edge,
        })

bets.sort(key=lambda x: -x['edge_pp'])

print('=' * 115)
print(f"  TODAY'S ALT SPREAD VALUE BETS  ({DATE})")
print(f"  Model: 2025 RPG/RAPG  |  ERA: live 2026  |  Odds: {MAX_ODDS} to {MIN_ODDS}  |  Positive edge only")
print(f"  TBD starters excluded  |  Edge = Poisson model% minus calibrated book implied%")
print('=' * 115)

if not bets:
    print('\n  No qualifying spread bets found today.')
else:
    print(f"\n  {'#':<3} {'Type':<8} {'Game':<13} {'Bet':<14} {'Starters':<34} "
          f"{'Diff':>5} {'Model%':>7} {'Fair':>8} {'Est.Book':>9} {'Edge':>8}")
    print(f"  {'-'*112}")
    for i, b in enumerate(bets, 1):
        sp = f"{b['away_sp']}({b['away_era']:.2f}) v {b['home_sp']}({b['home_era']:.2f})"
        print(f"  {i:<3} {b['type']:<8} {b['game']:<13} {b['bet']:<14} "
              f"{sp:<34} {b['diff']:>+5.2f} "
              f"{b['model_p']*100:>6.1f}% {b['fair_am']:>+8d} "
              f"{b['book_est']:>+9d} {b['edge_pp']:>+7.1f}pp")

print()
print('=' * 115)
print(f"  {len(bets)} qualifying spread bets")
print()
print('  HOW TO USE:')
print('  Est.Book = estimated price your sportsbook shows for this alt line.')
print('  If actual book price is LESS negative than Est.Book -> confirmed edge.')
print('  Fair = true no-vig value. Any price between Fair and Est.Book = value.')
print('=' * 115)
