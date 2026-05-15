"""
Round-robin parlay combinatorics, expected value, and hit-distribution.

A parlay's payout is the product of decimal odds across all legs.
Probability of winning the parlay is the product of leg probabilities,
assuming independence. EV per $1 staked = prob × (payout − 1) − (1 − prob).

A round robin of N picks "by K" generates C(N,K) parlays. EV / variance /
hit-distribution are computed over that whole bundle.

Cross-game independence is the key assumption — for HR props, batters
in different games are reasonably independent, but two batters in the
same game are not. The round-robin builder enforces "one batter per
game" by default.
"""

from itertools import combinations
from math import comb


# ---------- Odds helpers ----------

def decimal_to_american(decimal: float) -> int:
    if decimal <= 1:
        return 0
    if decimal >= 2.0:
        return int(round((decimal - 1) * 100))
    return int(round(-100 / (decimal - 1)))


def american_to_decimal(american: int) -> float:
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def implied_to_fair_decimal(p: float) -> float:
    """A fair (no-vig) decimal price for a given probability."""
    if p <= 0 or p >= 1:
        return 0
    return 1.0 / p


# ---------- Single parlay ----------

def parlay_metrics(legs, stake: float) -> dict:
    """
    legs: list of dicts each with at least {'name', 'prob', 'decimal_odds'}
    Returns probability, payout multiplier, payout dollars, profit if win,
    EV, and variance for a $stake wager.
    """
    if not legs:
        return {}
    prob = 1.0
    mult = 1.0
    for leg in legs:
        prob *= max(0.0, min(1.0, leg["prob"]))
        mult *= leg["decimal_odds"]

    payout = stake * mult
    profit_win = payout - stake
    loss = stake
    ev = prob * profit_win - (1 - prob) * loss
    # Variance of $X return from a single bet
    var = prob * (profit_win - ev) ** 2 + (1 - prob) * (-loss - ev) ** 2

    return {
        "prob":          prob,
        "decimal_odds":  mult,
        "american_odds": decimal_to_american(mult),
        "payout":        payout,
        "profit_if_win": profit_win,
        "ev":            ev,
        "stdev":         var ** 0.5,
    }


# ---------- Round robin ----------

def enumerate_parlays(picks, sizes, one_per_game: bool = True):
    """
    Yields tuples of legs (indexes into `picks`). `picks` must be a list
    of dicts each with at least 'game_id'. If one_per_game is True,
    skip combinations that would put two legs in the same game.
    """
    n = len(picks)
    for k in sizes:
        if k < 2 or k > n:
            continue
        for combo in combinations(range(n), k):
            if one_per_game:
                games = {picks[i]["game_id"] for i in combo}
                if len(games) != k:
                    continue
            yield combo


def build_round_robin(picks, sizes, stake_per_parlay: float, one_per_game: bool = True):
    """
    picks: list of {name, prob, decimal_odds, game_id, ...}
    sizes: list of ints (e.g. [2, 3] for "by 2s and 3s")
    stake_per_parlay: $ per parlay slip
    Returns:
        parlays: list of dicts with metrics + leg names
        summary: rollup stats
    """
    parlays = []
    for combo in enumerate_parlays(picks, sizes, one_per_game):
        legs = [picks[i] for i in combo]
        m = parlay_metrics(legs, stake_per_parlay)
        m["legs"]      = [leg["name"] for leg in legs]
        m["leg_count"] = len(legs)
        m["leg_idx"]   = combo
        parlays.append(m)

    if not parlays:
        return [], {}

    total_stake = stake_per_parlay * len(parlays)
    total_ev    = sum(p["ev"] for p in parlays)
    total_max_payout = sum(p["payout"] for p in parlays)
    expected_winners = sum(p["prob"] for p in parlays)

    # Hit distribution: probability of exactly k parlays winning, for k=0..N
    # Note: parlays share legs, so they are NOT independent. We compute the
    # exact joint distribution by enumerating over all 2^L outcomes of the
    # underlying L picks and counting how many parlays would win.
    L = len(picks)
    hit_dist = None
    if L <= 14:  # 2^14 = 16384 — fast enough
        hit_dist = _exact_hit_distribution(picks, parlays, stake_per_parlay)

    return parlays, {
        "total_parlays":       len(parlays),
        "total_stake":         total_stake,
        "total_ev":            total_ev,
        "total_max_payout":    total_max_payout,
        "expected_winners":    expected_winners,
        "ev_per_dollar":       total_ev / total_stake if total_stake else 0,
        "hit_distribution":    hit_dist,  # None if too many picks
    }


def _exact_hit_distribution(picks, parlays, stake_per_parlay) -> dict:
    """
    Brute-force enumeration over all 2^L outcomes of the L picks.
    Returns dict with:
      'pmf':           list of (n_winners, prob, payout, profit_or_loss)
      'p_at_least_one_win': probability ≥1 parlay wins
      'p_profit':      probability total round robin returns positive PnL
    """
    L = len(picks)
    probs = [p["prob"] for p in picks]
    total_stake = stake_per_parlay * len(parlays)
    pmf = {}  # n_winners -> total_prob
    payout_dist = {}  # n_winners -> avg payout (weighted)

    for mask in range(1 << L):
        # Probability of this exact outcome
        p_outcome = 1.0
        for i in range(L):
            p_outcome *= probs[i] if (mask >> i) & 1 else (1 - probs[i])

        # Count winning parlays under this outcome
        n_win = 0
        payout = 0.0
        for parlay in parlays:
            # All legs must be hits
            if all((mask >> i) & 1 for i in parlay["leg_idx"]):
                n_win += 1
                payout += parlay["payout"]
        pmf[n_win] = pmf.get(n_win, 0.0) + p_outcome
        payout_dist[n_win] = payout_dist.get(n_win, 0.0) + p_outcome * payout

    # Normalize avg payout by mass
    rows = []
    p_at_least_one = 0.0
    p_profit = 0.0
    for n_win in sorted(pmf):
        prob = pmf[n_win]
        avg_pay = payout_dist[n_win] / prob if prob > 0 else 0
        rows.append({
            "winners": n_win,
            "prob":    prob,
            "payout":  avg_pay,
            "pnl":     avg_pay - total_stake,
        })
        if n_win >= 1:
            p_at_least_one += prob
        if avg_pay - total_stake > 0:
            p_profit += prob

    return {
        "pmf":                rows,
        "p_at_least_one_win": p_at_least_one,
        "p_profit":           p_profit,
    }


if __name__ == "__main__":
    # Demo: 5 picks, round robin by 3
    picks = [
        {"name": "Judge",   "prob": 0.18, "decimal_odds": 4.5, "game_id": 1},
        {"name": "Soto",    "prob": 0.15, "decimal_odds": 5.0, "game_id": 2},
        {"name": "Olson",   "prob": 0.13, "decimal_odds": 5.5, "game_id": 3},
        {"name": "Acuña",   "prob": 0.12, "decimal_odds": 6.0, "game_id": 4},
        {"name": "Schwarber","prob": 0.16, "decimal_odds": 4.8, "game_id": 5},
    ]
    parlays, summ = build_round_robin(picks, [3], stake_per_parlay=10)
    print(f"Round-robin by 3s: {summ['total_parlays']} parlays")
    print(f"Total stake: ${summ['total_stake']:.0f}")
    print(f"Total EV: ${summ['total_ev']:+.2f}  ({summ['ev_per_dollar']*100:+.2f}%)")
    print(f"Expected winning parlays: {summ['expected_winners']:.2f}")
    if summ.get("hit_distribution"):
        hd = summ["hit_distribution"]
        print(f"P(at least 1 winning parlay): {hd['p_at_least_one_win']*100:.1f}%")
        print(f"P(net profitable round robin): {hd['p_profit']*100:.1f}%")
        print(f"Hit distribution (winners → prob, avg payout):")
        for r in hd["pmf"][:8]:
            print(f"  {r['winners']:>2}: {r['prob']*100:>5.2f}%  payout=${r['payout']:>7.2f}  PnL=${r['pnl']:+.2f}")