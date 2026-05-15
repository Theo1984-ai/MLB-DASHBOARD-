"""
Alternate spreads and totals edge finder.
Uses 2025 season data as historical hit-rate baseline.
Shows only -200 to -1000 odds with positive edge.
"""
import sys, json, time, math
from collections import defaultdict
sys.path.insert(0, '.')
from data.odds import _fetch, BASE, SPORT_KEY, american_to_implied
import urllib.parse, urllib.request

KEY = '328b6a86d5bd586ca057559e63a2d006'

# Full team name → abbreviation (for matching Odds API names)
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

# ── 2026 team offense / defense (R/G and RA/G) ──────────────────────────────
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

# ── 2025 team offense / defense (full-season baseline) ──────────────────────
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

# ── Today's starting pitcher ERA ────────────────────────────────────────────
LEAGUE_ERA = 4.30
sp_era = {
    'LAA_away':('Walbert Urena',   3.22),
    'CLE_home':('Slade Cecconi',   6.15),
    'NYY_away':('Will Warren',     3.46),
    'BAL_home':('TBD',             4.50),
    'WSH_away':('Miles Mikolas',   7.44),
    'CIN_home':('Brady Singer',    5.63),
    'COL_away':('Michael Lorenzen',6.92),
    'PIT_home':('Paul Skenes',     2.36),
    'PHI_away':('Zack Wheeler',    3.12),
    'BOS_home':('TBD',             4.50),
    'TB_away': ('Shane McClanahan',2.60),
    'TOR_home':('Patrick Corbin',  3.60),
    'DET_away':('Jack Flaherty',   5.56),
    'NYM_home':('Freddy Peralta',  3.12),
    'CHC_away':('Colin Rea',       4.03),
    'ATL_home':('Grant Holmes',    4.34),
    'KC_away': ('Stephen Kolek',   4.50),
    'CWS_home':('Erick Fedde',     3.79),
    'MIA_away':('Eury Perez',      5.01),
    'MIN_home':('Bailey Ober',     4.19),
    'SD_away': ('Matt Waldron',    7.71),
    'MIL_home':('Brandon Sproat',  5.87),
    'AZ_away': ('Zac Gallen',      4.70),
    'TEX_home':('MacKenzie Gore',  5.18),
    'SEA_away':('Bryan Woo',       4.02),
    'HOU_home':('Tatsuya Imai',    7.27),
    'STL_away':('Andre Pallante',  4.34),
    'ATH_home':('Jeffrey Springs', 3.89),
    'SF_away': ('Adrian Houser',   6.19),
    'LAD_home':('Yoshinobu Yamamoto',3.09),
}

MATCHUPS = [
    ('LAA','CLE','LAA_away','CLE_home'),
    ('NYY','BAL','NYY_away','BAL_home'),
    ('WSH','CIN','WSH_away','CIN_home'),
    ('COL','PIT','COL_away','PIT_home'),
    ('PHI','BOS','PHI_away','BOS_home'),
    ('TB', 'TOR','TB_away', 'TOR_home'),
    ('DET','NYM','DET_away','NYM_home'),
    ('CHC','ATL','CHC_away','ATL_home'),
    ('KC', 'CWS','KC_away', 'CWS_home'),
    ('MIA','MIN','MIA_away','MIN_home'),
    ('SD', 'MIL','SD_away', 'MIL_home'),
    ('AZ', 'TEX','AZ_away', 'TEX_home'),
    ('SEA','HOU','SEA_away','HOU_home'),
    ('STL','ATH','STL_away','ATH_home'),
    ('SF', 'LAD','SF_away', 'LAD_home'),
]

EVENT_IDS = {
    ('LAA','CLE'): '9936f843966ef49b29f8f61cd41491f1',
    ('NYY','BAL'): '6d160243347bd35cdb3abf1b2690fda8',
    ('WSH','CIN'): '6c8098ab353355d002d66e825b4217ab',
    ('COL','PIT'): '79fa6c856d2a95ff10cb955ab2d424e2',
    ('PHI','BOS'): '07b1faa56850a94bfd3921725fe520a2',
    ('TB', 'TOR'): 'b2c44ca94237edf5b7270d7dde706835',
    ('DET','NYM'): 'fe40950314ca87c69985e8a1a37c8822',
    ('CHC','ATL'): '744c4597586797d5f7e9be094f031f11',
    ('KC', 'CWS'): 'c125a2382938301b1e4462c3600de32b',
    ('MIA','MIN'): 'ad3552c4909328a0be4de1ec98e97bcf',
    ('SD', 'MIL'): 'a0a43f882e4a4ab92fa1024e42f33be0',
    ('AZ', 'TEX'): '34393ecc2b2d109779095b1d4935225b',
    ('SEA','HOU'): 'a1fa0d550bd68cf44d71f0657fa93d6a',
    ('STL','ATH'): '265d87925f501ac50a2a12c6b5a91354',
    ('SF', 'LAD'): 'a0929cf66d445eec19f118010165e95c',
}

# ── Poisson helpers ──────────────────────────────────────────────────────────
def poisson_pmf(k, lam):
    if k < 0 or lam <= 0:
        return 0.0
    return (lam**k * math.exp(-lam)) / math.factorial(k)

def p_over_total(lam, point):
    n = int(point)
    return 1.0 - sum(poisson_pmf(k, lam) for k in range(n + 1))

def p_under_total(lam, point):
    n = int(point)
    return sum(poisson_pmf(k, lam) for k in range(n + 1))

def p_wins_by(lam_win, lam_lose, margin):
    p = 0.0
    for w in range(25):
        for l in range(max(0, w - margin + 1)):
            if w - l >= margin:
                p += poisson_pmf(w, lam_win) * poisson_pmf(l, lam_lose)
    return p

def p_covers_plus(lam_team, lam_opp, point):
    # Team covers +point (wins or doesn't lose by more than |point|)
    margin = int(abs(point))
    return 1.0 - p_wins_by(lam_opp, lam_team, margin + 1)

# ── Run expectation ──────────────────────────────────────────────────────────
def exp_runs(off_rpg, def_rapg, sp_era_val):
    base = (off_rpg + def_rapg) / 2.0
    adj  = (sp_era_val - LEAGUE_ERA) * 0.22
    return max(1.5, base + adj)

# ── Main ─────────────────────────────────────────────────────────────────────
all_bets = []
last_hdrs = {}

for away, home, away_key, home_key in MATCHUPS:
    away_sp_name, away_sp_e = sp_era[away_key]
    home_sp_name, home_sp_e = sp_era[home_key]

    # 2026 expected runs
    lam_a = exp_runs(rpg26[away], rapg26[home], home_sp_e)
    lam_b = exp_runs(rpg26[home], rapg26[away], away_sp_e)
    total_26 = lam_a + lam_b

    # 2025 expected runs (historical baseline)
    lam_a25 = exp_runs(rpg25[away], rapg25[home], home_sp_e)
    lam_b25 = exp_runs(rpg25[home], rapg25[away], away_sp_e)
    total_25 = lam_a25 + lam_b25

    eid = EVENT_IDS[(away, home)]
    params = urllib.parse.urlencode({
        'apiKey':     KEY,
        'regions':    'us',
        'markets':    'alternate_spreads,alternate_totals',
        'bookmakers': 'draftkings,fanduel,betmgm',
        'oddsFormat': 'american',
    })
    url = f'{BASE}/sports/{SPORT_KEY}/events/{eid}/odds?{params}'
    try:
        data, last_hdrs = _fetch(url)
    except Exception as e:
        print(f'  {away}@{home}: {e}')
        time.sleep(0.2)
        continue

    # Best odds per outcome across books
    best = {}
    for bm in data.get('bookmakers', []):
        for mkt in bm.get('markets', []):
            mk = mkt.get('key')
            for out in mkt.get('outcomes', []):
                price = out.get('price')
                if price is None:
                    continue
                price = int(price)
                if price > -200 or price < -1000:
                    continue
                name  = out.get('name', '?')
                point = out.get('point')
                key   = (mk, name, point)
                if key not in best or price > best[key]['price']:
                    best[key] = {'price': price, 'name': name, 'point': point, 'market': mk}

    for (mk, name, point), info in best.items():
        price   = info['price']
        implied = american_to_implied(price)

        if mk == 'alternate_totals':
            pt = float(point)
            if name == 'Over':
                hist_p  = p_over_total(total_25, pt)
                model_p = p_over_total(total_26, pt)
                label   = f'Over {pt}'
            else:
                hist_p  = p_under_total(total_25, pt)
                model_p = p_under_total(total_26, pt)
                label   = f'Under {pt}'

        elif mk == 'alternate_spreads':
            pt = float(point)
            name_abbr = FULL_TO_ABBR.get(name, name)
            is_away = (name_abbr == away)
            if is_away:
                if pt < 0:
                    hist_p  = p_wins_by(lam_a25, lam_b25, int(abs(pt)))
                    model_p = p_wins_by(lam_a,   lam_b,   int(abs(pt)))
                else:
                    hist_p  = p_covers_plus(lam_a25, lam_b25, pt)
                    model_p = p_covers_plus(lam_a,   lam_b,   pt)
            else:  # home team
                if pt < 0:
                    hist_p  = p_wins_by(lam_b25, lam_a25, int(abs(pt)))
                    model_p = p_wins_by(lam_b,   lam_a,   int(abs(pt)))
                else:
                    hist_p  = p_covers_plus(lam_b25, lam_a25, pt)
                    model_p = p_covers_plus(lam_b,   lam_a,   pt)
            label = f'{name} {pt:+.1f}'
        else:
            continue

        edge_hist  = (hist_p  - implied) * 100
        edge_model = (model_p - implied) * 100

        if edge_hist <= 0:
            continue

        all_bets.append({
            'game':      f'{away}@{home}',
            'away_sp':   away_sp_name,
            'home_sp':   home_sp_name,
            'market':    'Alt Total' if mk == 'alternate_totals' else 'Alt Spread',
            'bet':       label,
            'full_label': f'{away}@{home}  {label}',
            'odds':      price,
            'implied':   implied,
            'hist_p':    hist_p,
            'model_p':   model_p,
            'edge_hist': edge_hist,
            'edge_model':edge_model,
            'exp_total': total_26,
            'exp_total_25': total_25,
        })
    time.sleep(0.15)

print(f"Quota remaining: {last_hdrs.get('x-requests-remaining','?')}")
print()

# Deduplicate: keep best edge per (game, bet)
deduped = {}
for b in all_bets:
    key = (b['game'], b['bet'])
    if key not in deduped or b['edge_hist'] > deduped[key]['edge_hist']:
        deduped[key] = b

results = sorted(deduped.values(), key=lambda x: -x['edge_hist'])

print(f"Positive-edge alternate bets today: {len(results)}")
print()

# ── Display ──────────────────────────────────────────────────────────────────
mkt_groups = {}
for b in results:
    mkt_groups.setdefault(b['market'], []).append(b)

for mkt, bets in mkt_groups.items():
    print(f"{'='*80}")
    print(f"  {mkt.upper()}  — -200 to -1000 odds, positive 2025 edge only")
    print(f"{'='*80}")
    print(f"  {'Game':<12} {'Bet':<20} {'Odds':>6} {'Implied':>8} {'2025 Hit%':>10} {'Edge':>7} {'SPs'}")
    print(f"  {'-'*85}")
    for b in bets:
        away, home = b['game'].split('@')
        sps = f"{b['away_sp'].split()[-1]} vs {b['home_sp'].split()[-1]}"
        print(f"  {b['game']:<12} {b['bet']:<20} {b['odds']:>+5d}  {b['implied']*100:>6.1f}%  {b['hist_p']*100:>8.1f}%  {b['edge_hist']:>+6.1f}pp  {sps}")
    print()
