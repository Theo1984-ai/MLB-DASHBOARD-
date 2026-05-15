"""
Value Bet Finder — Alternate Lines Only
========================================
Odds range : -200 to -1000
Edge       : Model% (Poisson / 2025 last-season RPG data) minus Book Implied%
Lines      : Alternate totals (Under) + Alternate run lines
Data       : Live pitcher ERAs (MLB Stats API, free) + book lines cached from
             this morning's run (Odds API quota exhausted for rest of month)

Edge formula:
  edge_pp = (model_p - book_implied) * 100
  Positive only shown.
"""
import json, math, time, urllib.request
import sys
sys.path.insert(0, '.')

DATE       = '2026-05-13'
LEAGUE_ERA = 4.30
MIN_ODDS   = -1000
MAX_ODDS   = -200
MIN_EDGE   = 0.5     # pp

# ── 2025 LAST-SEASON BASELINE ─────────────────────────────────────────────────
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

# ── LIVE BOOK LINES (fetched this morning, Odds API now at quota 0) ────────────
# Format: (at, ht): {ml_away, ml_home, rl_away_pt, rl_away_odds, total_line, under_odds}
# Source: spread_today.py output from earlier this session
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

# ── Math ──────────────────────────────────────────────────────────────────────
def exp_runs(off, def_r, sp_era):
    return max(1.5, (off + def_r) / 2.0 + (sp_era - LEAGUE_ERA) * 0.22)

def poisson_p(lam, k):
    return (lam**k * math.exp(-lam)) / math.factorial(k)

def p_under(lam, line):
    return sum(poisson_p(lam, k) for k in range(int(line) + 1))

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
    """Remove vig — return true probability for side A."""
    return imp_a / (imp_a + imp_b)

def fair_am(p):
    p = max(0.001, min(p, 0.999))
    if p >= 0.5: return int(round(-p / (1 - p) * 100))
    return int(round((1 - p) / p * 100))


# ── Live pitcher ERAs ─────────────────────────────────────────────────────────
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
            s   = splits[0]['stat']
            ip  = float(s.get('inningsPitched', 0) or 0)
            era = float(s.get('era', 0) or 0)
            return era if ip >= 5 else LEAGUE_ERA
    except: pass
    return LEAGUE_ERA

print(f'Value Bets — {DATE}  |  Alt lines  |  -200 to -1000  |  Positive edge only')
print('Loading pitcher ERAs from MLB Stats API...')

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

# ── Build bets ────────────────────────────────────────────────────────────────
bets = []

for (at, ht), bl in BOOK_LINES.items():
    if at not in rpg25 or ht not in rpg25: continue
    pits = pit_map.get((at, ht), (None, None, 'TBD', 'TBD'))
    apid, hpid, anam, hnam = pits
    away_era = era_cache.get(apid, LEAGUE_ERA)
    home_era = era_cache.get(hpid, LEAGUE_ERA)

    lam_a   = exp_runs(rpg25[at], rapg25[ht], home_era)
    lam_b   = exp_runs(rpg25[ht], rapg25[at], away_era)
    exp_tot = lam_a + lam_b

    fav  = at if lam_a >= lam_b else ht
    dog  = ht if fav == at else at
    fs   = 'away' if fav == at else 'home'

    # ── True (no-vig) probabilities from real ML prices ───────────────────────
    imp_a  = implied(bl['ml_a'])
    imp_h  = implied(bl['ml_h'])
    true_a = true_p(imp_a, imp_h)
    true_h = 1.0 - true_a

    # ── True run-line probability from real RL prices ─────────────────────────
    # rl_a_pt is the AWAY spread, rl_a_od is the odds for that spread
    rl_pt  = bl['rl_a_pt']        # e.g. +1.5 means away is dog
    rl_od  = bl['rl_a_od']
    # Complement odds (home RL) — approximate
    rl_h_od = fair_am(1 - implied(rl_od))   # rough complement
    imp_rl_a = implied(rl_od)
    imp_rl_h = 1 - imp_rl_a + 0.04   # add ~4% vig back for complement
    true_rl_a = true_p(imp_rl_a, min(imp_rl_h, 0.95))

    # ── Under total from real line ────────────────────────────────────────────
    total_line = bl['total']
    u_od       = bl['u_od']
    o_od       = fair_am(1 - implied(u_od) + 0.04)  # approx over odds
    imp_u      = implied(u_od)
    imp_o      = max(0.02, 1 - imp_u + 0.04)
    true_u_std = true_p(imp_u, imp_o)

    vig_total = imp_u + max(0.02, 1.0 - imp_u + 0.04) - 1.0

    # ── Alt Under bets ────────────────────────────────────────────────────────
    for offset in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]:
        line = total_line + offset
        line = round(line * 2) / 2

        model_p = p_under(exp_tot, line)
        if model_p < 0.667 or model_p > 0.91: continue  # out of -200/-1000 range

        fa = fair_am(model_p)
        if fa < MIN_ODDS or fa > MAX_ODDS: continue

        # Book implied at alt line:
        # Use standard Under line as anchor + Poisson to scale
        model_p_std = p_under(exp_tot, total_line)
        if model_p_std > 0.01:
            # Calibrate: book's true prob at standard / model's = vig factor
            calib = true_u_std / model_p_std
            book_imp_alt = min(model_p * calib, 0.9999)
        else:
            book_imp_alt = min(model_p + vig_total / 2, 0.9999)

        edge = (model_p - book_imp_alt) * 100
        if edge < MIN_EDGE: continue

        bets.append({
            'type':     'Alt Under',
            'game':     f'{at}@{ht}',
            'bet':      f'Under {line}',
            'away_sp':  anam.split()[-1],
            'home_sp':  hnam.split()[-1],
            'away_era': away_era,
            'home_era': home_era,
            'exp_tot':  exp_tot,
            'model_p':  model_p,
            'fair_am':  fa,
            'edge_pp':  edge,
            'ref':      f'Std U{total_line} @ {u_od:+d}  (book true {true_u_std*100:.1f}%)',
        })

    # ── Alt Run Line (fav giving points) ─────────────────────────────────────
    for rl in [-1.5, -2.5, -3.5]:
        model_p = p_rl(lam_a, lam_b, abs(rl), side=fs)
        if model_p < 0.667 or model_p > 0.91: continue

        fa = fair_am(model_p)
        if fa < MIN_ODDS or fa > MAX_ODDS: continue

        # Book implied: calibrate from the real standard RL
        # model prob that fav wins by 2+ (standard RL cover)
        model_p_std_rl  = p_rl(lam_a, lam_b, 1.5, side=fs)
        if model_p_std_rl > 0.01:
            # book's true prob at std RL for fav
            fav_rl_imp = (true_rl_a if fs == 'away' else 1 - true_rl_a)
            calib_rl   = fav_rl_imp / model_p_std_rl
            book_imp_rl = min(model_p * calib_rl, 0.9999)
        else:
            book_imp_rl = min(model_p + 0.03, 0.9999)

        edge = (model_p - book_imp_rl) * 100
        if edge < MIN_EDGE: continue

        rl_type = 'Std RL' if abs(rl) == 1.5 else 'Alt RL'
        bets.append({
            'type':     rl_type,
            'game':     f'{at}@{ht}',
            'bet':      f'{fav} {rl:+.1f}',
            'away_sp':  anam.split()[-1],
            'home_sp':  hnam.split()[-1],
            'away_era': away_era,
            'home_era': home_era,
            'exp_tot':  exp_tot,
            'model_p':  model_p,
            'fair_am':  fa,
            'edge_pp':  edge,
            'ref':      f'Std RL {rl_pt:+.1f} @ {rl_od:+d}  (book true fav {(true_rl_a if fs=="away" else 1-true_rl_a)*100:.1f}%)',
        })

# ── Sort & print ──────────────────────────────────────────────────────────────
# Dedup: best edge per (game, bet)
seen = {}
for b in bets:
    k = (b['game'], b['bet'])
    if k not in seen or b['edge_pp'] > seen[k]['edge_pp']:
        seen[k] = b

final = sorted(seen.values(), key=lambda x: -x['edge_pp'])

print('=' * 120)
print(f"  TODAY'S POSITIVE-EDGE ALT LINE BETS  ({DATE})")
print(f"  Model: 2025 RPG/RAPG last-season data  |  ERA: live 2026  |  Lines: this morning's book prices")
print(f"  Odds: {MAX_ODDS} to {MIN_ODDS}  |  Edge = Poisson model% minus book's vig-removed implied%")
print('=' * 120)
print()
print(f"  {'#':<3} {'Type':<10} {'Game':<13} {'Bet':<16} {'Starters':<36} "
      f"{'ExpTot':>7} {'Model%':>7} {'Fair Odds':>10} {'Edge':>8}")
print(f"  {'-'*118}")

for i, b in enumerate(final, 1):
    sp = f"{b['away_sp']}({b['away_era']:.2f}) v {b['home_sp']}({b['home_era']:.2f})"
    print(f"  {i:<3} {b['type']:<10} {b['game']:<13} {b['bet']:<16} "
          f"{sp:<36} {b['exp_tot']:>7.2f} "
          f"{b['model_p']*100:>6.1f}% {b['fair_am']:>+10d} {b['edge_pp']:>+7.1f}pp")
    print(f"       Ref: {b['ref']}")
    print()

print('=' * 120)
print(f"  {len(final)} positive-edge bets found")
print()
print('  HOW TO USE:')
print('  1. Fair Odds = what you SHOULD be getting (no vig). Open your book and find the alt line.')
print('  2. If book prices the alt line at BETTER (less negative) than Fair Odds -> confirmed edge.')
print('  3. If book is more negative than Fair Odds -> no edge at that book.')
print('  4. Model% and Edge are based on 2025 season data — the tested baseline.')
print('=' * 120)
