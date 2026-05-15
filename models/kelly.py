"""
Kelly Criterion bet sizing.

Given a probability `p` of winning and decimal odds `d`, the Kelly-optimal
fraction of bankroll to wager is:

    f* = (b·p − q) / b      where b = d − 1, q = 1 − p

If f* ≤ 0 → no edge → don't bet.

Full Kelly maximizes long-run geometric bankroll growth but is volatile.
Most practitioners use a fractional Kelly (half or quarter) to trade
some growth for much lower variance.

We also cap the recommended stake at a sensible max (default 5% of
bankroll per bet) to limit single-bet downside even if Kelly says more.
"""

DEFAULT_MAX_FRACTION = 0.05   # 5% bankroll cap per bet
DEFAULT_KELLY_MULT   = 0.5    # half-Kelly default


def kelly_fraction(p: float, decimal_odds: float) -> float:
    """
    Pure Kelly fraction. Returns 0 (or negative) when there's no edge.
    """
    if decimal_odds <= 1.0:
        return 0.0
    b = decimal_odds - 1.0
    q = 1.0 - p
    return (b * p - q) / b


def fractional_kelly(p: float, decimal_odds: float,
                     multiplier: float = DEFAULT_KELLY_MULT) -> float:
    """Returns multiplier × full Kelly, floored at 0."""
    f = kelly_fraction(p, decimal_odds)
    if f <= 0:
        return 0.0
    return max(0.0, multiplier * f)


def safe_stake(p: float, decimal_odds: float, bankroll: float,
               multiplier: float = DEFAULT_KELLY_MULT,
               max_fraction: float = DEFAULT_MAX_FRACTION) -> dict:
    """
    Returns a dict of:
      {fraction, stake, expected_profit, decimal_odds, profit_if_win}

    fraction is clipped at `max_fraction` to limit single-bet downside.
    """
    f = fractional_kelly(p, decimal_odds, multiplier)
    f_capped = min(f, max_fraction)
    stake = bankroll * f_capped
    profit_if_win = stake * (decimal_odds - 1)
    expected_profit = p * profit_if_win - (1 - p) * stake

    return {
        "fraction":         f_capped,
        "fraction_uncapped": f,
        "stake":            stake,
        "decimal_odds":     decimal_odds,
        "profit_if_win":    profit_if_win,
        "expected_profit":  expected_profit,
        "edge_present":     f > 0,
        "capped":           f > max_fraction,
    }


def breakeven_probability(decimal_odds: float) -> float:
    """The probability at which the offered odds are exactly fair (no edge)."""
    if decimal_odds <= 1.0:
        return 1.0
    return 1.0 / decimal_odds


if __name__ == "__main__":
    print("Kelly examples (bankroll $1,000, half-Kelly, 5% cap):\n")
    cases = [
        ("Judge +400, model 25%",  0.25, 5.00),
        ("Soto +320, model 22%",   0.22, 4.20),
        ("Olson +280, model 21%",  0.21, 3.80),
        ("Mid-tier +500, model 17%", 0.17, 6.00),
        ("No edge +200, model 25%",  0.25, 3.00),
        ("Negative edge +200, model 30%", 0.30, 3.00),
    ]
    for name, p, d in cases:
        info = safe_stake(p, d, bankroll=1000, multiplier=0.5)
        full_k = kelly_fraction(p, d)
        print(f"  {name}")
        print(f"    full Kelly: {full_k*100:+5.2f}%   half-Kelly stake: ${info['stake']:>5.2f}"
              f"   EV: ${info['expected_profit']:+6.2f}"
              f"   {'(capped @5%)' if info['capped'] else ''}"
              f"   {'NO BET' if not info['edge_present'] else ''}")