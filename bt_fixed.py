"""Fixed-line backtest analysis."""
import json, math, os
from collections import defaultdict

PROFILE = os.environ.get('USERPROFILE','C:/Users/17146')
with open(os.path.join(PROFILE,'bt_games.json')) as f:
    games = json.load(f)
with open(os.path.join(PROFILE,'bt_pit_stats.json')) as f:
    pit_stats_raw = json.load(f)
pit_stats = {int(k):v for k,v in pit_stats_raw.items()}

rpg25={'WSH':4.24,'CIN':4.42,'NYY':5.24,'BAL':4.18,'PHI':4.80,'BOS':4.85,'COL':3.69,'PIT':3.60,'TB':4.41,'TOR':4.93,'DET':4.68,'NYM':4.73,'CHC':4.89,'ATL':4.47,'KC':4.02,'CWS':3.99,'MIA':4.38,'MIN':4.18,'SD':4.33,'MIL':4.97,'AZ':4.88,'TEX':4.22,'SEA':4.73,'HOU':4.24,'STL':4.25,'ATH':4.53,'SF':4.35,'LAD':5.09,'LAA':4.15,'CLE':3.97}
rapg25={'WSH':5.22,'CIN':3.80,'NYY':3.86,'BAL':4.53,'PHI':3.75,'BOS':3.68,'COL':5.76,'PIT':3.69,'TB':3.87,'TOR':4.13,'DET':3.89,'NYM':3.96,'CHC':3.73,'ATL':4.30,'KC':3.68,'CWS':4.14,'MIA':4.55,'MIN':4.45,'SD':3.57,'MIL':3.54,'AZ':4.45,'TEX':3.44,'SEA':3.88,'HOU':3.82,'STL':4.21,'ATH':4.63,'SF':3.76,'LAD':3.91,'LAA':4.80,'CLE':3.66}
LEAGUE_ERA=4.30

def exp_runs(off,def_r,sp):
    return max(1.5,(off+def_r)/2.0+(sp-LEAGUE_ERA)*0.22)

def american_to_implied(o):
    return abs(o)/(abs(o)+100.0) if o<0 else 100.0/(o+100.0)

def poisson_pmf(k,lam):
    if k<0 or lam<=0: return 0.0
    return (lam**k*math.exp(-lam))/math.factorial(k)

def p_under_val(lam,line):
    return sum(poisson_pmf(k,lam) for k in range(int(line)+1))

totals = [g['total'] for g in games]
n = len(totals)
margins = sorted([abs(g['home_runs']-g['away_runs']) for g in games])

# ── Section 1: Fixed line totals ──────────────────────────────────────────
print('='*72)
print('  FIXED LINE TOTALS  —  Actual hit rates vs today recommended odds')
print(f'  Sample: {n} games, Apr 28 - May 11, 2026')
print('='*72)

lines = [
    (5.5,  'over',  -259, 'Over 5.5  (DET@NYM rec)'),
    (5.5,  'over',  -281, 'Over 5.5  (COL@PIT rec)'),
    (6.5,  'over',  -222, 'Over 6.5  (AZ@TEX rec)'),
    (7.5,  'over',  -300, 'Over 7.5  (general)'),
    (9.5,  'under', -200, 'Under 9.5 (floor)'),
    (10.0, 'under', -227, 'Under 10.0 (PHI@BOS rec)'),
    (10.5, 'under', -200, 'Under 10.5 (floor)'),
    (11.0, 'under', -205, 'Under 11.0 (KC@CWS rec)'),
    (11.5, 'under', -201, 'Under 11.5 (STL@ATH rec)'),
    (11.5, 'under', -238, 'Under 11.5 (KC@CWS rec)'),
    (12.0, 'under', -233, 'Under 12.0 (WSH@CIN rec)'),
    (12.0, 'under', -241, 'Under 12.0 (STL@ATH rec)'),
    (12.0, 'under', -295, 'Under 12.0 (KC@CWS rec)'),
]

print(f'  {"Bet":<38} {"Odds":>6} {"Implied":>8} {"Actual%":>8} {"Edge":>8} {"ROI":>8}')
print(f'  {"-"*74}')
for line, side, odds, label in lines:
    if side == 'over':
        hits = sum(1 for t in totals if t > line)
    else:
        hits = sum(1 for t in totals if t <= line)
    act_pct = hits/n*100
    imp_pct = american_to_implied(odds)*100
    edge = act_pct - imp_pct
    dec = 1.0+100.0/abs(odds)
    wag = n*100
    ret = hits*100*dec
    roi = (ret-wag)/wag*100
    flag = ' +++' if edge>5 else ('  ++' if edge>2 else ('   +' if edge>0 else '   -'))
    print(f'  {label:<38} {odds:>+5d}  {imp_pct:>6.1f}%  {act_pct:>6.1f}%  {edge:>+6.1f}pp  {roi:>+7.1f}%{flag}')

# ── Section 2: Qualified game filter ─────────────────────────────────────
print()
print('='*72)
print('  QUALIFIED GAMES ONLY  —  Under X when model exp total <= threshold')
print('  (filter: only take bet when game is projected low-scoring)')
print('='*72)

threshold_tests = [
    (10.0, 7.5,  -227, 'Under 10.0 when model exp < 7.5'),
    (11.0, 8.5,  -205, 'Under 11.0 when model exp < 8.5'),
    (11.0, 9.0,  -205, 'Under 11.0 when model exp < 9.0'),
    (11.5, 9.0,  -201, 'Under 11.5 when model exp < 9.0'),
    (11.5, 9.5,  -201, 'Under 11.5 when model exp < 9.5'),
    (12.0, 9.5,  -241, 'Under 12.0 when model exp < 9.5'),
    (12.0,10.0,  -241, 'Under 12.0 when model exp < 10.0'),
]

print(f'  {"Filter":<40} {"N":>4} {"AvgExp":>7} {"Actual%":>8} {"Implied%":>9} {"Edge":>7} {"ROI":>8}')
print(f'  {"-"*80}')
for under_line, max_exp, odds, label in threshold_tests:
    qualified = []
    for g in games:
        away,home = g['away'],g['home']
        if away not in rpg25 or home not in rpg25: continue
        apid = g.get('away_pit_id')
        hpid = g.get('home_pit_id')
        asp = pit_stats.get(apid,LEAGUE_ERA) if apid else LEAGUE_ERA
        hsp = pit_stats.get(hpid,LEAGUE_ERA) if hpid else LEAGUE_ERA
        la = exp_runs(rpg25[away],rapg25[home],hsp)
        lb = exp_runs(rpg25[home],rapg25[away],asp)
        exp_tot = la+lb
        if exp_tot <= max_exp:
            qualified.append((g,exp_tot))
    if not qualified:
        print(f'  {label:<40}: no qualifying games')
        continue
    hits = sum(1 for g,_ in qualified if g['total'] <= under_line)
    nq = len(qualified)
    act_pct = hits/nq*100
    imp_pct = american_to_implied(odds)*100
    edge = act_pct - imp_pct
    dec = 1.0+100.0/abs(odds)
    wag = nq*100
    ret = hits*100*dec
    roi = (ret-wag)/wag*100
    avg_exp = sum(e for _,e in qualified)/nq
    flag = ' +++' if edge>5 else ('  ++' if edge>2 else ('   +' if edge>0 else '   -'))
    print(f'  {label:<40} {nq:>4} {avg_exp:>7.1f} {act_pct:>7.1f}%  {imp_pct:>8.1f}%  {edge:>+6.1f}pp  {roi:>+7.1f}%{flag}')

# ── Section 3: Alt spreads ────────────────────────────────────────────────
print()
print('='*72)
print('  ALT SPREADS  —  Actual margin coverage vs recommended odds')
print(f'  Based on {n} game margins (all games, regardless of matchup)')
print('='*72)

spread_tests = [
    (2.0, -277, 'Dog +2.0 at -277 (CHC@ATL rec)'),
    (2.0, -240, 'Dog +2.0 at -240 (TB@TOR rec)'),
    (3.0, -295, 'Dog +3.0 at -295 (STL@ATH rec)'),
    (3.0, -297, 'Dog +3.0 at -297 (PHI@BOS rec)'),
    (3.5, -215, 'Dog +3.5 at -215 (SF@LAD rec)'),
    (4.0, -230, 'Dog +4.0 at -230 (SF@LAD rec)'),
    (4.0, -252, 'Dog +4.0 at -252 (COL@PIT rec)'),
]

print(f'  {"Bet":<38} {"Odds":>6} {"Implied":>8} {"Actual%":>8} {"Edge":>8} {"ROI":>8}')
print(f'  {"-"*74}')
for spread, odds, label in spread_tests:
    hits = sum(1 for x in margins if x < spread)/n
    act_pct = hits*100
    imp_pct = american_to_implied(odds)*100
    edge = act_pct - imp_pct
    dec = 1.0+100.0/abs(odds)
    wag = n*100
    ret = hits*n*100*dec
    roi = (ret-wag)/wag*100
    flag = ' +++' if edge>5 else ('  ++' if edge>2 else ('   +' if edge>0 else '   -'))
    print(f'  {label:<38} {odds:>+5d}  {imp_pct:>6.1f}%  {act_pct:>6.1f}%  {edge:>+6.1f}pp  {roi:>+7.1f}%{flag}')

# ── Section 4: Day-by-day totals ──────────────────────────────────────────
print()
print('='*72)
print('  DAILY GAME TOTALS  —  context for variance')
print('='*72)
by_date = defaultdict(list)
for g in games:
    by_date[g['date']].append(g['total'])

print(f'  {"Date":<12} {"Games":>6} {"Avg Total":>10} {"U11 hit%":>9} {"U12 hit%":>9}')
print(f'  {"-"*50}')
for d in sorted(by_date):
    tots = by_date[d]
    avg = sum(tots)/len(tots)
    u11 = sum(1 for t in tots if t<=11)/len(tots)*100
    u12 = sum(1 for t in tots if t<=12)/len(tots)*100
    print(f'  {d:<12} {len(tots):>6} {avg:>10.1f} {u11:>8.0f}% {u12:>8.0f}%')

all_u11 = sum(1 for t in totals if t<=11)/n*100
all_u12 = sum(1 for t in totals if t<=12)/n*100
print(f'  {"AVERAGE":<12} {n:>6} {sum(totals)/n:>10.1f} {all_u11:>8.0f}% {all_u12:>8.0f}%')
print()
print(f'  Overall avg game total (Apr 28-May 11): {sum(totals)/n:.2f} runs')
print(f'  Lowest game total: {min(totals)}  |  Highest: {max(totals)}')
print(f'  Games with total <= 10: {sum(1 for t in totals if t<=10)} ({sum(1 for t in totals if t<=10)/n*100:.1f}%)')
print(f'  Games with total <= 11: {sum(1 for t in totals if t<=11)} ({sum(1 for t in totals if t<=11)/n*100:.1f}%)')
print(f'  Games with total <= 12: {sum(1 for t in totals if t<=12)} ({sum(1 for t in totals if t<=12)/n*100:.1f}%)')
