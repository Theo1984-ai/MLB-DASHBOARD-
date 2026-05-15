"""
True Poisson probability for every alternate run line.
Uses the same run-expectation model as the Alt Under strategy.
Shows fair odds for -3.5 / -2.5 / -1.5 / ML / +1.5 / +2.5 / +3.5
for today's 5 value games.
"""
import json, math, urllib.request, time

LEAGUE_ERA = 4.30

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


def exp_runs(off, def_r, sp_era):
    return max(1.5, (off + def_r) / 2.0 + (sp_era - LEAGUE_ERA) * 0.22)


def poisson_joint(lam_a, lam_b, max_r=25):
    """Build full joint score distribution P[a][b]."""
    pa = [(lam_a**k * math.exp(-lam_a)) / math.factorial(k) for k in range(max_r)]
    pb = [(lam_b**k * math.exp(-lam_b)) / math.factorial(k) for k in range(max_r)]
    return pa, pb


def cover_prob(lam_a, lam_b, spread, side='away'):
    """
    P(side covers spread).
    spread > 0 means side is giving points (e.g. -1.5 → spread=1.5)
    spread < 0 means side is getting points (e.g. +1.5 → spread=-1.5)
    side='away' means we track lam_a; 'home' tracks lam_b.
    """
    pa, pb = poisson_joint(lam_a, lam_b)
    p = 0.0
    max_r = len(pa)
    for a in range(max_r):
        for b in range(max_r):
            if side == 'away':
                diff = a - b        # positive = away winning
            else:
                diff = b - a        # positive = home winning
            # Cover condition: diff > spread (spread is the points given)
            if diff > spread:
                p += pa[a] * pb[b]
    return p


def prob_to_american(p):
    """Convert true probability to fair American odds (no vig)."""
    if p <= 0 or p >= 1:
        return 0
    if p >= 0.5:
        return int(round(-p / (1 - p) * 100))
    else:
        return int(round((1 - p) / p * 100))


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


# ── Get full schedule + pitcher ERAs for today ────────────────────────────────
print('Fetching schedule + pitcher ERAs...')
sched_url = ('https://statsapi.mlb.com/api/v1/schedule'
             '?sportId=1&date=2026-05-13&hydrate=probablePitcher,team')
req = urllib.request.Request(sched_url, headers={'User-Agent': 'mlb/1.0'})
with urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL) as r:
    sched = json.loads(r.read())

pit_map = {}
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

# Build VALUE_GAMES from full schedule (all games, fav = home team by default,
# will be corrected below by model expected runs)
VALUE_GAMES = []
for key, (apid, hpid, anam, hnam) in pit_map.items():
    at, ht = key.split('@')
    if at in rpg25 and ht in rpg25:
        VALUE_GAMES.append((at, ht, None))   # fav determined by model below

era_cache = {}
for at, ht, _ in VALUE_GAMES:
    key  = f'{at}@{ht}'
    pits = pit_map.get(key, (None, None, 'TBD', 'TBD'))
    for pid in [pits[0], pits[1]]:
        if pid and pid not in era_cache:
            era_cache[pid] = get_era(pid)
            time.sleep(0.07)

# ── Compute all games ─────────────────────────────────────────────────────────
SPREADS = [-3.5, -2.5, -1.5, 0.0, 1.5, 2.5, 3.5]

game_results = []
for at, ht, _ in VALUE_GAMES:
    key  = f'{at}@{ht}'
    pits = pit_map.get(key, (None, None, 'TBD', 'TBD'))
    apid, hpid, anam, hnam = pits
    away_era = era_cache.get(apid, LEAGUE_ERA)
    home_era = era_cache.get(hpid, LEAGUE_ERA)

    lam_a = exp_runs(rpg25[at], rapg25[ht], home_era)
    lam_b = exp_runs(rpg25[ht], rapg25[at], away_era)
    exp_tot = lam_a + lam_b

    # Model favorite = team with higher expected runs
    if lam_a >= lam_b:
        fav, dog = at, ht
        fav_side  = 'away'
        fav_exp, dog_exp = lam_a, lam_b
    else:
        fav, dog = ht, at
        fav_side  = 'home'
        fav_exp, dog_exp = lam_b, lam_a

    spread_probs = {}
    for sp in SPREADS:
        p = cover_prob(lam_a, lam_b,
                       -sp,          # negate: label -1.5 means giving 1.5 = spread +1.5
                       side=fav_side)
        spread_probs[sp] = p

    game_results.append({
        'key': key, 'at': at, 'ht': ht,
        'anam': anam, 'hnam': hnam,
        'away_era': away_era, 'home_era': home_era,
        'lam_a': lam_a, 'lam_b': lam_b,
        'exp_tot': exp_tot,
        'fav': fav, 'dog': dog, 'fav_side': fav_side,
        'fav_exp': fav_exp, 'dog_exp': dog_exp,
        'spread_probs': spread_probs,
    })

# Sort by fav_exp - dog_exp descending (biggest mismatches first)
game_results.sort(key=lambda x: -(x['fav_exp'] - x['dog_exp']))

# ── Summary table ─────────────────────────────────────────────────────────────
print()
print('=' * 105)
print(f"  TODAY'S FULL SLATE — ALTERNATE RUN LINE PROBABILITIES  (2026-05-13)")
print(f"  Fav = model-determined favorite  |  All probs from fav's perspective")
print('=' * 105)
print()
print(f"  {'Game':<13} {'Fav':<5} {'Away SP':<16} {'Home SP':<16} "
      f"{'ExpA':>5} {'ExpH':>5} {'Diff':>5}  "
      f"{'ML%':>6} {'-1.5%':>6} {'-2.5%':>6}  "
      f"{'Fair ML':>8} {'Fair-1.5':>9} {'Fair-2.5':>9}")
print('-' * 105)

for g in game_results:
    sp  = g['spread_probs']
    pml  = sp[0.0]
    prl  = sp[-1.5]
    prl2 = sp[-2.5]
    print(f"  {g['key']:<13} {g['fav']:<5} "
          f"{g['anam'].split()[-1]:<16} {g['hnam'].split()[-1]:<16} "
          f"{g['lam_a']:>5.2f} {g['lam_b']:>5.2f} "
          f"{g['fav_exp']-g['dog_exp']:>+5.2f}  "
          f"{pml*100:>5.1f}% {prl*100:>5.1f}% {prl2*100:>5.1f}%  "
          f"{prob_to_american(pml):>+8d} {prob_to_american(prl):>+9d} {prob_to_american(prl2):>+9d}")

# ── Detail per game ───────────────────────────────────────────────────────────
print()
print('=' * 105)
print('  FULL ALTERNATE LINE BREAKDOWN BY GAME')
print('=' * 105)

for g in game_results:
    sp = g['spread_probs']
    print()
    print(f"  {g['key']}  |  Fav: {g['fav']} ({g['fav_exp']:.2f} exp)  "
          f"Dog: {g['dog']} ({g['dog_exp']:.2f} exp)  |  "
          f"Total: {g['exp_tot']:.2f}")
    print(f"  {g['anam'].split()[-1]} (ERA {g['away_era']:.2f}) vs "
          f"{g['hnam'].split()[-1]} (ERA {g['home_era']:.2f})")
    print()
    print(f"  {'Line':<12} {'Prob':>8} {'Fair Odds':>10}  {'Bar'}")
    print(f"  {'-'*55}")
    labels = {
        -3.5: f"{g['fav']} -3.5",
        -2.5: f"{g['fav']} -2.5",
        -1.5: f"{g['fav']} -1.5",
         0.0: f"{g['fav']} ML",
         1.5: f"{g['fav']} +1.5",
         2.5: f"{g['fav']} +2.5",
         3.5: f"{g['fav']} +3.5",
    }
    for s in SPREADS:
        p    = sp[s]
        fair = prob_to_american(p)
        bar  = '#' * int(p * 40)
        print(f"  {labels[s]:<12} {p*100:>7.1f}% {fair:>+10d}  {bar}")

print()
print('=' * 105)
print('  NOTE: Model probabilities only (Poisson). No vig. Compare to book lines for value.')
print('=' * 105)
