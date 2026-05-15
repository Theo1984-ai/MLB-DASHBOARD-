"""
Backtest for alternate spreads + totals strategy.
Uses Apr 28 - May 11 game results vs Poisson model.
Compares actual hit rate to model-projected and book-implied rates.
"""
import json, math, os

# ── Load data ──────────────────────────────────────────────────────────────
PROFILE = os.environ.get('USERPROFILE', 'C:/Users/17146')
with open(os.path.join(PROFILE, 'bt_games.json')) as f:
    games = json.load(f)
with open(os.path.join(PROFILE, 'bt_pit_stats.json')) as f:
    pit_stats_raw = json.load(f)
pit_stats = {int(k): v for k, v in pit_stats_raw.items()}

# ── 2025 and 2026 team stats ─────────────────────────────────────────────
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

LEAGUE_ERA = 4.30
FALLBACK_TEAM_RPG  = 4.30
FALLBACK_TEAM_RAPG = 4.10

def exp_runs(off_rpg, def_rapg, sp_era):
    base = (off_rpg + def_rapg) / 2.0
    adj  = (sp_era - LEAGUE_ERA) * 0.22
    return max(1.5, base + adj)

def poisson_pmf(k, lam):
    if k < 0 or lam <= 0:
        return 0.0
    return (lam ** k * math.exp(-lam)) / math.factorial(k)

def p_over_val(lam, line):
    n = int(line)
    return 1.0 - sum(poisson_pmf(k, lam) for k in range(n + 1))

def p_under_val(lam, line):
    n = int(line)
    return sum(poisson_pmf(k, lam) for k in range(n + 1))

def p_wins_by(lam_w, lam_l, margin):
    p = 0.0
    for w in range(25):
        for l in range(max(0, w - margin + 1)):
            if w - l >= margin:
                p += poisson_pmf(w, lam_w) * poisson_pmf(l, lam_l)
    return p

def american_to_implied(odds):
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)

# ── Typical market prices for each alternate line type ────────────────────
# Used for P&L simulation since we don't have historical book odds.
# Based on today's observed pricing patterns from FanDuel/DK.
ALT_TOTAL_PRICES = {
    # line_offset = line - expected_total → typical American odds
    # Over lines (line < expected total → cheaper)
    ('over', -3.0): -900,  # Over (expected-3): very heavy fav
    ('over', -2.5): -600,
    ('over', -2.0): -450,
    ('over', -1.5): -340,
    ('over', -1.0): -250,
    ('over', -0.5): -200,
    ('over',  0.0): -170,
    ('over',  0.5): -140,
    ('over',  1.0): -115,
    # Under lines (line > expected total → cheaper)
    ('under',  0.5): -200,
    ('under',  1.0): -230,
    ('under',  1.5): -270,
    ('under',  2.0): -320,
    ('under',  2.5): -380,
    ('under',  3.0): -450,
    ('under',  3.5): -530,
    ('under',  4.0): -650,
    ('under',  4.5): -800,
    ('under',  5.0): -950,
}

ALT_SPREAD_PRICES = {
    # spread_offset = spread_line (e.g. +2.0 → typical odds)
    2.0: -270,
    2.5: -300,
    3.0: -330,
    3.5: -380,
    4.0: -430,
    4.5: -500,
    5.0: -600,
    5.5: -700,
    6.0: -850,
    6.5: -1000,
}

# ── Define the bet types to test ──────────────────────────────────────────
# Alt totals: test Under (exp+1.5), Under (exp+2.5), Under (exp+3.5)
#             and Over (exp-2.5), Over (exp-1.5)
# Alt spreads: underdog team +2, +3, +4

TOTAL_UNDER_OFFSETS = [1.5, 2.5, 3.5]  # Under (expected + offset)
TOTAL_OVER_OFFSETS  = [-2.5, -1.5]     # Over  (expected + offset)  [negative = below expected]
SPREAD_LINES        = [2.0, 3.0, 4.0]  # underdog team gets +X

results_by_type = {}

def add_result(label, hit, model_p, implied_p, odds):
    if label not in results_by_type:
        results_by_type[label] = {'bets':0,'wins':0,'wagered':0,'returned':0,'model_p_sum':0,'implied_p_sum':0}
    r = results_by_type[label]
    r['bets']      += 1
    r['wins']      += int(hit)
    r['wagered']   += 100
    dec = 1.0 + 100.0/abs(odds)  # negative odds decimal
    r['returned']  += 100 * dec if hit else 0
    r['model_p_sum'] += model_p
    r['implied_p_sum'] += implied_p

# ── Run backtest ─────────────────────────────────────────────────────────
skipped = 0
for g in games:
    away = g['away']
    home = g['home']
    actual_total  = g['total']
    away_runs     = g['away_runs']
    home_runs     = g['home_runs']
    away_pit_id   = g.get('away_pit_id')
    home_pit_id   = g.get('home_pit_id')

    # Get team stats — use 2025 as historical baseline (matches strategy design)
    if away not in rpg25 or home not in rpg25:
        skipped += 1
        continue

    away_sp_era = pit_stats.get(away_pit_id, LEAGUE_ERA) if away_pit_id else LEAGUE_ERA
    home_sp_era = pit_stats.get(home_pit_id, LEAGUE_ERA) if home_pit_id else LEAGUE_ERA

    # Expected runs using 2025 baseline (same as model)
    lam_away_25 = exp_runs(rpg25[away], rapg25[home], home_sp_era)
    lam_home_25 = exp_runs(rpg25[home], rapg25[away], away_sp_era)
    exp_total_25 = lam_away_25 + lam_home_25

    # Also compute 2026 model expectation for comparison
    lam_away_26 = exp_runs(rpg26.get(away, FALLBACK_TEAM_RPG), rapg26.get(home, FALLBACK_TEAM_RAPG), home_sp_era)
    lam_home_26 = exp_runs(rpg26.get(home, FALLBACK_TEAM_RPG), rapg26.get(away, FALLBACK_TEAM_RAPG), away_sp_era)
    exp_total_26 = lam_away_26 + lam_home_26

    # Identify underdog (team with higher expected run allowance = home if home_sp is better)
    if lam_away_26 < lam_home_26:
        dog_runs = away_runs
        fav_runs = home_runs
        dog_lam25 = lam_away_25
        fav_lam25 = lam_home_25
    else:
        dog_runs = home_runs
        fav_runs = away_runs
        dog_lam25 = lam_home_25
        fav_lam25 = lam_away_25

    # ── ALT TOTALS ─────────────────────────────────────────────────────────
    for offset in TOTAL_UNDER_OFFSETS:
        line = round(exp_total_25 + offset - 0.5) + 0.5  # nearest .5 above expected+offset
        hit  = actual_total <= line
        mp   = p_under_val(exp_total_25, line)
        # Find closest price from table
        closest_offset = min(ALT_TOTAL_PRICES.keys(), key=lambda x: abs(x[1]-offset) if x[0]=='under' else 99)
        odds = ALT_TOTAL_PRICES.get(('under', offset), -350)
        imp  = american_to_implied(odds)
        label = f'Alt Under (exp+{offset})'
        add_result(label, hit, mp, imp, odds)

    for offset in TOTAL_OVER_OFFSETS:
        line = round(exp_total_25 + offset - 0.5) + 0.5
        hit  = actual_total > line
        mp   = p_over_val(exp_total_25, line)
        odds = ALT_TOTAL_PRICES.get(('over', offset), -350)
        imp  = american_to_implied(odds)
        label = f'Alt Over (exp{offset:+.1f})'
        add_result(label, hit, mp, imp, odds)

    # ── ALT SPREADS (underdog side) ────────────────────────────────────────
    for spread in SPREAD_LINES:
        # Dog covers if: dog_runs > fav_runs - spread (i.e. dog doesn't lose by more than spread-1)
        actual_margin = fav_runs - dog_runs
        hit = actual_margin < spread  # dog loses by LESS than spread (covers +spread)
        mp  = p_wins_by(fav_lam25, dog_lam25, int(spread))  # P(fav wins by >= spread) = P(dog fails)
        mp  = 1.0 - mp  # P(dog covers)
        odds = ALT_SPREAD_PRICES.get(spread, -400)
        imp  = american_to_implied(odds)
        label = f'Alt Spread (dog +{int(spread)}.0)'
        add_result(label, hit, mp, imp, odds)

# ── Report ────────────────────────────────────────────────────────────────
print(f'Backtest: Apr 28 - May 11, 2026')
print(f'Games processed: {len(games) - skipped}  |  Skipped (unknown team): {skipped}')
print()

total_wagered = 0
total_returned = 0

print(f'{"Bet Type":<28} {"Bets":>5} {"Wins":>5} {"Act%":>7} {"Mdl%":>7} {"Imp%":>7} {"Act-Imp":>8} {"Wagered":>9} {"Return":>9} {"ROI":>7}')
print('=' * 102)

order = [
    'Alt Over (exp-2.5)', 'Alt Over (exp-1.5)',
    'Alt Under (exp+1.5)', 'Alt Under (exp+2.5)', 'Alt Under (exp+3.5)',
    'Alt Spread (dog +2.0)', 'Alt Spread (dog +3.0)', 'Alt Spread (dog +4.0)',
]

for label in order:
    r = results_by_type.get(label)
    if not r or r['bets'] == 0:
        continue
    act_pct = r['wins'] / r['bets'] * 100
    mdl_pct = r['model_p_sum'] / r['bets'] * 100
    imp_pct = r['implied_p_sum'] / r['bets'] * 100
    edge    = act_pct - imp_pct
    roi     = (r['returned'] - r['wagered']) / r['wagered'] * 100
    total_wagered  += r['wagered']
    total_returned += r['returned']
    edge_flag = ' ***' if edge > 5 else (' **' if edge > 2 else (' *' if edge > 0 else ''))
    print(f'{label:<28} {r["bets"]:>5} {r["wins"]:>5} {act_pct:>6.1f}%  {mdl_pct:>6.1f}%  {imp_pct:>6.1f}%  {edge:>+7.1f}pp  ${r["wagered"]:>7,.0f}  ${r["returned"]:>7,.0f}  {roi:>+6.1f}%{edge_flag}')

print('=' * 102)
overall_roi = (total_returned - total_wagered) / total_wagered * 100 if total_wagered else 0
print(f'{"TOTAL":<28} {"":>5} {"":>5} {"":>7} {"":>7} {"":>7} {"":>8}  ${total_wagered:>7,.0f}  ${total_returned:>7,.0f}  {overall_roi:>+6.1f}%')

print()
print('Legend:  Act% = actual hit rate  |  Mdl% = model projected  |  Imp% = book implied')
print('         Act-Imp = edge vs book  |  *** = strong positive edge  ** = moderate  * = slight')
print()

# ── Distribution analysis ────────────────────────────────────────────────
print('TOTAL RUNS DISTRIBUTION (actual games):')
totals = [g['total'] for g in games]
for thresh in [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]:
    under_pct = sum(1 for t in totals if t < thresh) / len(totals) * 100
    over_pct  = sum(1 for t in totals if t > thresh) / len(totals) * 100
    print(f'  Under {thresh}: {under_pct:5.1f}%  |  Over {thresh}: {over_pct:5.1f}%')
print()

# Spread coverage
margins = [abs(g['home_runs'] - g['away_runs']) for g in games]
print('ACTUAL RUN MARGINS (absolute):')
for m in [1, 2, 3, 4, 5, 6, 7]:
    within_pct = sum(1 for x in margins if x < m) / len(margins) * 100
    print(f'  Win by less than {m}: {within_pct:5.1f}%  (dog covers +{m-1}.5: {within_pct:5.1f}%)')
