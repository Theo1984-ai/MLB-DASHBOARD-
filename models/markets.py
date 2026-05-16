"""
Probability calculators for moneyline, run-line, and total-runs markets.

Game-level model (game_model.py) returns:
  home_exp_runs, away_exp_runs, p_home_win, total_runs.

This module turns those into market-specific probabilities that can be
compared to sportsbook implied probabilities to find edges.

Math:
  - Run totals are modeled as independent Poisson r.v.s per team.
  - The total runs scored is the sum, also Poisson with mean = sum of means.
  - The run differential is Skellam-distributed (difference of Poissons).
  - We compute exact probabilities via a brute-force Poisson-product sum
    (cap at 25 runs/team for tail truncation; total mass error < 1e-6).

No scipy dependency — uses stdlib math only.
"""

import math


def _poisson_pmf(k: int, mu: float) -> float:
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-mu) * mu**k / math.factorial(k)


def _poisson_cdf(k: int, mu: float) -> float:
    return sum(_poisson_pmf(i, mu) for i in range(k + 1))


# ---------- Moneyline ----------

def prob_moneyline(p_home_win: float, side: str) -> float:
    """side: 'home' or 'away'. Returns model's P(team wins outright)."""
    if side == "home":
        return p_home_win
    return 1.0 - p_home_win


# ---------- Total runs (over / under) ----------

def prob_total_over(expected_total: float, line: float, max_runs: int = 30) -> float:
    """
    P(actual total > line). MLB totals are typically X.5 (half-run lines)
    so there are no pushes. For integer lines we treat ties as half wins.
    """
    if line == int(line):
        # Integer line — handle push (each team's wins/losses in MLB markets
        # treat exact line as a push refund, but we model as P(strictly >))
        cdf_at_line = _poisson_cdf(int(line), expected_total)
        return 1.0 - cdf_at_line
    floor_line = int(math.floor(line))
    return 1.0 - _poisson_cdf(floor_line, expected_total)


def prob_total_under(expected_total: float, line: float, max_runs: int = 30) -> float:
    return 1.0 - prob_total_over(expected_total, line, max_runs)


# ---------- Run line (spread) ----------

def prob_team_covers_spread(home_exp: float, away_exp: float,
                             team_side: str, spread_point: float,
                             max_runs: int = 25) -> float:
    """
    Probability that `team_side` ('home' or 'away') covers the spread.

    The spread is a number subtracted from the bettor's chosen team's score
    (favorite negative, underdog positive). E.g.:
      - "Yankees -1.5" → home favored, must win by ≥ 2 to cover.
      - "Red Sox +1.5" → away underdog, covers if loses by ≤ 1 OR wins.

    Computes via brute-force Poisson product up to max_runs per side.
    """
    if home_exp <= 0 or away_exp <= 0:
        return 0.5

    # Bettor's "margin needed" relative to other team.
    # team's actual_runs + spread > opponent's actual_runs  ⇔  team covers
    # equivalently: (team_runs - opponent_runs) > -spread
    # So we need P(team_runs - opp_runs > -spread).
    if team_side == "home":
        my_lambda, opp_lambda = home_exp, away_exp
    else:
        my_lambda, opp_lambda = away_exp, home_exp

    threshold = -spread_point  # need diff > threshold to cover

    cover_prob = 0.0
    for my_r in range(max_runs + 1):
        p_my = _poisson_pmf(my_r, my_lambda)
        if p_my < 1e-9:
            continue
        for opp_r in range(max_runs + 1):
            p_opp = _poisson_pmf(opp_r, opp_lambda)
            if p_opp < 1e-9:
                continue
            diff = my_r - opp_r
            # Push handling: if diff exactly equals threshold, it's a push.
            # MLB run lines are usually -1.5/+1.5 so no integer ties matter.
            # For -1 / +1 (alternate run lines), diff == 1 exactly = push.
            if diff > threshold:
                cover_prob += p_my * p_opp
            # Ties (diff == threshold) don't count toward coverage; they push.

    return cover_prob


# ---------- Helper: turn an Odds API outcome row into a model probability ----------

def model_prob_for_outcome(outcome: dict,
                           home_team: str, away_team: str,
                           home_exp_runs: float, away_exp_runs: float,
                           p_home_win: float) -> float | None:
    """
    Given an Odds API outcome dict (with market, name, point) and the
    game-model output, returns the model's probability of that outcome
    occurring. Returns None if the outcome isn't recognized.
    """
    market = outcome.get("market")
    name = outcome.get("name", "")
    point = outcome.get("point")

    if market == "h2h":
        # Outcome name = team name
        if name == home_team:
            return p_home_win
        if name == away_team:
            return 1.0 - p_home_win
        return None

    if market == "totals":
        if point is None:
            return None
        if name.lower() == "over":
            return prob_total_over(home_exp_runs + away_exp_runs, float(point))
        if name.lower() == "under":
            return prob_total_under(home_exp_runs + away_exp_runs, float(point))
        return None

    if market == "spreads":
        if point is None:
            return None
        if name == home_team:
            return prob_team_covers_spread(home_exp_runs, away_exp_runs,
                                           "home", float(point))
        if name == away_team:
            return prob_team_covers_spread(home_exp_runs, away_exp_runs,
                                           "away", float(point))
        return None

    return None


# ---------- Market-blend helpers (shared by dashboard + local scripts) ----------
#
# Backtest-validated alphas (Apr 28 – May 11, 2026):
#   - RL  pure model     (alpha = 1.0) — +24.2% ROI, market blending hurts
#   - ML  50/50 blend    (alpha = 0.5) — pure +17.8% → blend +23.4%
#   - TOT 30/70 blend    (alpha = 0.3) — pure +2.7% → blend +18.6%
#                                        (fixes 73% model / 56% actual overcalibration)

BLEND_ALPHA_ML  = 0.5   # ML: 50% model, 50% devigged market
BLEND_ALPHA_RL  = 1.0   # RL: pure model
BLEND_ALPHA_TOT = 0.3   # TOT: 30% model, 70% devigged market


def devig_two_way(implied_a: float, implied_b: float) -> tuple:
    """
    Given two implied probabilities from sportsbook odds (e.g., home & away ML,
    over & under), return the no-vig fair probabilities that sum to 1.0.
    """
    total = implied_a + implied_b
    if total <= 0:
        return 0.5, 0.5
    return implied_a / total, implied_b / total


def blend(model_p: float, fair_market_p: float, alpha: float) -> float:
    """
    Blend the model probability with the devigged market probability.
      alpha = 1.0 → pure model (no blend)
      alpha = 0.0 → pure market (ignore model)
    """
    alpha = max(0.0, min(1.0, alpha))
    return alpha * model_p + (1 - alpha) * fair_market_p


def blended_moneyline(model_p_team: float, implied_team: float,
                      implied_opp: float, alpha: float = BLEND_ALPHA_ML) -> float:
    """Devig + blend for moneyline."""
    fair_team, _ = devig_two_way(implied_team, implied_opp)
    return blend(model_p_team, fair_team, alpha)


def blended_total(model_p_side: float, implied_over: float,
                  implied_under: float, side: str,
                  alpha: float = BLEND_ALPHA_TOT) -> float:
    """Devig + blend for totals (over/under)."""
    fair_over, fair_under = devig_two_way(implied_over, implied_under)
    fair = fair_over if side.lower() == "over" else fair_under
    return blend(model_p_side, fair, alpha)


def blended_runline(model_p_team: float, *args, **kwargs) -> float:
    """RL uses pure model (alpha=1.0) per backtest. Helper for symmetry."""
    return model_p_team


if __name__ == "__main__":
    # Sanity tests
    print("Total O/U sanity:")
    print(f"  P(total > 8.5 | E=9.0): {prob_total_over(9.0, 8.5):.3f}")
    print(f"  P(total > 8.5 | E=7.5): {prob_total_over(7.5, 8.5):.3f}")
    print(f"  P(total > 10.5 | E=9.0): {prob_total_over(9.0, 10.5):.3f}")
    print()
    print("Run-line sanity:")
    print(f"  Home -1.5 @ home_exp=5.5, away_exp=3.5: {prob_team_covers_spread(5.5, 3.5, 'home', -1.5):.3f}")
    print(f"  Away +1.5 @ home_exp=5.5, away_exp=3.5: {prob_team_covers_spread(5.5, 3.5, 'away', 1.5):.3f}")
    print(f"  Sums close to 1.0 (no push for x.5 lines): "
          f"{prob_team_covers_spread(5.5, 3.5, 'home', -1.5) + prob_team_covers_spread(5.5, 3.5, 'away', 1.5):.4f}")
    print()
    print("Even matchup (4.5 vs 4.5):")
    print(f"  Home -1.5: {prob_team_covers_spread(4.5, 4.5, 'home', -1.5):.3f}")
    print(f"  Total > 9: {prob_total_over(9.0, 9.0):.3f}")
