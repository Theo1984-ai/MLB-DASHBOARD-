"""
League-average constants used as priors and normalizers.

Values are calibrated to MLB 2023-2024 league-wide averages. Without
historical training data we use these as Bayesian priors — observed
player rates are regressed toward these for low-sample players.
"""

# Per-PA outcomes
HR_PER_PA      = 0.032       # ~3.2% league HR rate per plate appearance
BB_PER_PA      = 0.085
K_PER_PA       = 0.225
HIT_PER_PA     = 0.245

# Pitcher rate norms
HR_PER_9       = 1.20
K_PER_9        = 8.6
BB_PER_9       = 3.2
ERA            = 4.20
xERA           = 4.20
OPS_AGAINST    = 0.720
xwOBA_AGAINST  = 0.320

# Statcast batted-ball league averages (qualified hitters)
BARREL_PCT     = 7.5         # barrels per BBE × 100
HARDHIT_PCT    = 38.0        # EV ≥ 95mph %
AVG_EXIT_VELO  = 88.5
SWEET_SPOT_PCT = 33.0
xSLG           = 0.410
xwOBA          = 0.320

# Game-level
RUNS_PER_GAME  = 4.50        # team avg, per game
PA_PER_GAME    = 38.5        # team plate appearances per 9-inning game
PA_PER_BATTER  = 4.3         # avg per starter per game
EXPECTED_INNINGS_PER_START = 5.4

# Park-factor neutral baseline
PARK_NEUTRAL   = 100

# Empirical environment effects (from baseball-research literature):
#   - Temperature: HR rate rises ~1% per 1°F above ~70°F (capped)
#   - Humidity: drier air lets ball travel ~0.7% farther per -10% RH
#   - Wind: handled separately in stadiums.wind_hr_boost()
TEMP_BASELINE_F  = 70.0
TEMP_HR_PER_F    = 0.010      # +1% HR rate per °F above 70
TEMP_CAP_DELTA   = 0.20       # cap effect at ±20%

HUMIDITY_BASELINE = 50.0
HUMIDITY_HR_PER_PCT = 0.0007  # tiny but non-zero

# Bayesian regression — prior weight (in PA) for batter rate stats
PRIOR_WEIGHT_BATTER_PA = 100
PRIOR_WEIGHT_PITCHER_BF = 80


def regress(observed_rate, observed_n, prior_rate, prior_weight):
    """
    Bayesian shrinkage toward a prior.
    Returns the posterior rate given an observed rate from `observed_n` trials
    and a prior of strength `prior_weight` (in equivalent trials).
    """
    if observed_rate is None or observed_n is None or observed_n <= 0:
        return prior_rate
    return (observed_rate * observed_n + prior_rate * prior_weight) / (observed_n + prior_weight)


def temp_factor(temp_f):
    """Multiplicative HR adjustment for temperature."""
    if temp_f is None:
        return 1.0
    delta = TEMP_HR_PER_F * (temp_f - TEMP_BASELINE_F)
    delta = max(-TEMP_CAP_DELTA, min(TEMP_CAP_DELTA, delta))
    return 1.0 + delta


def humidity_factor(humidity_pct):
    """Drier air → slightly higher HR rate."""
    if humidity_pct is None:
        return 1.0
    delta = HUMIDITY_HR_PER_PCT * (HUMIDITY_BASELINE - humidity_pct)
    return 1.0 + max(-0.05, min(0.05, delta))


if __name__ == "__main__":
    print(f"League HR/PA: {HR_PER_PA:.4f}")
    print(f"Temp factor at 90°F: {temp_factor(90):.3f}")
    print(f"Temp factor at 50°F: {temp_factor(50):.3f}")
    print(f"Humidity factor at 30%: {humidity_factor(30):.3f}")
    print(f"Humidity factor at 80%: {humidity_factor(80):.3f}")
    # Regress a hot 30-PA streak (.500 BA) toward .250 league avg
    print(f"Regressed BA (.500 in 30 PA → prior .250): {regress(0.500, 30, 0.250, 100):.3f}")
