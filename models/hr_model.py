"""
Home-run probability model.

Without historical training data, we use a multiplicative log-linear
construction grounded in published baseball-research relationships.
Each component is normalized to league average so that an "average batter
in an average matchup at a neutral park in calm 70°F weather" produces
exactly the league baseline HR rate per PA.

Per-PA HR probability:
    p = base × Bskill × Mpit × Fpark × Ftemp × Fhumid × Fwind

Per-game HR probability (≥ 1):
    P(HR≥1) = 1 - (1 - p)^expected_PA
"""

from . import league


def _safe(v, default=None):
    try:
        f = float(v)
        if f != f:
            return default
        return f
    except (ValueError, TypeError):
        return default


def batter_skill_factor(batter_savant: dict | None, recent_stats: dict | None = None) -> float:
    """
    Translates a batter's Statcast profile into an HR-rate multiplier.
    Combines barrel%, hard-hit%, xSLG, sweet-spot%, and avg exit velocity
    (each normalized vs league mean). Sweet-spot% captures launch-angle
    optimization — a ground-ball hitter simply cannot hit HRs at the same
    rate regardless of exit velocity.

    barrel% is still the strongest single HR predictor and carries the
    largest individual weight.
    """
    if not batter_savant:
        skill = 1.0
    else:
        brl  = _safe(batter_savant.get("brl_percent"),          league.BARREL_PCT)
        hh   = _safe(batter_savant.get("ev95percent"),          league.HARDHIT_PCT)
        xslg = _safe(batter_savant.get("xslg"),                 league.xSLG)
        ss   = _safe(batter_savant.get("anglesweetspotpercent"), league.SWEET_SPOT_PCT)
        ev   = _safe(batter_savant.get("avg_hit_speed"),         league.AVG_EXIT_VELO)

        n_brl  = brl  / league.BARREL_PCT
        n_hh   = hh   / league.HARDHIT_PCT
        n_xslg = xslg / league.xSLG          if xslg else 1.0
        n_ss   = ss   / league.SWEET_SPOT_PCT if ss   else 1.0
        n_ev   = ev   / league.AVG_EXIT_VELO  if ev   else 1.0

        # Weights: brl=0.40, xslg=0.25, hh=0.10, sweet_spot=0.20, avg_ev=0.05
        # Sweet-spot% gets a meaningful weight because it gates launch angle —
        # barrel% and EV matter more when the batter is already hitting the
        # ball in the air (8–32° optimal window).
        skill = (n_brl ** 0.40) * (n_xslg ** 0.25) * (n_hh ** 0.10) * (n_ss ** 0.20) * (n_ev ** 0.05)

    # Recency adjustment: HR/PA over last 15 days vs league
    if recent_stats:
        hr = _safe(recent_stats.get("hr"), 0) or 0
        pa = _safe(recent_stats.get("pa"), 0) or 0
        if pa >= 15:  # min sample
            recent_hr_per_pa = hr / pa
            # Regress toward season skill * league baseline
            expected = skill * league.HR_PER_PA
            regressed = league.regress(
                observed_rate=recent_hr_per_pa,
                observed_n=pa,
                prior_rate=expected,
                prior_weight=80,  # ~half a season-equiv prior
            )
            recency_mult = regressed / expected if expected > 0 else 1.0
            # Allow ±20% recency adjustment (was ±15%) — a genuine hot streak
            # over 15+ PA is meaningful signal, not noise.
            recency_mult = max(0.80, min(1.20, recency_mult))
            skill *= recency_mult

    return skill


def handedness_factor(batter_hand: str | None,
                      pitcher_hand: str | None,
                      pitcher_splits: dict | None,
                      pitcher_savant_split: dict | None = None) -> float:
    """
    HR-rate adjustment for batter-vs-pitcher handedness.

    Prefers Savant xwOBA-against split (more stable / predictive) when
    available; falls back to MLB API raw OPS-against splits.

    Switch-hitters bat opposite-hand vs the pitcher (favorable ~95% of
    same-side outcomes on average).
    """
    if not batter_hand or not pitcher_hand:
        return 1.0

    if batter_hand == "S":
        effective_bat_side = "R" if pitcher_hand == "L" else "L"
    else:
        effective_bat_side = batter_hand

    # PREFERRED: Savant xwOBA split (more reliable than raw OPS)
    if pitcher_savant_split:
        side_lower = effective_bat_side.lower()
        xwoba = _safe(pitcher_savant_split.get(f"xwoba_vs_{side_lower}"))
        if xwoba is not None and xwoba > 0:
            factor = (xwoba / league.xwOBA_AGAINST) ** 1.5
            return max(0.6, min(1.7, factor))

    # FALLBACK: raw OPS split
    if not pitcher_splits:
        return 1.0
    if effective_bat_side == "L":
        split = pitcher_splits.get("vsL") or {}
    else:
        split = pitcher_splits.get("vsR") or {}
    if not split:
        return 1.0
    ab = _safe(split.get("ab"), 0) or 0
    if ab < 30:
        return 1.0
    split_ops = _safe(split.get("ops"), league.OPS_AGAINST)
    factor = (split_ops / league.OPS_AGAINST) ** 1.83
    return max(0.6, min(1.7, factor))


def pitcher_matchup_factor(pitcher_stats: dict, pitcher_savant: dict | None = None,
                           bullpen_stats: dict | None = None,
                           lineup_spot: int | None = None) -> float:
    """
    Pitcher's HR-allow tendency vs league. Combines actual HR/9 with xERA-based
    xwOBA-allowed for stability. Optionally blends in opposing bullpen HR/9 since
    most batters face BP for 1-2 of their 4 PAs.

    Args:
      pitcher_stats: starter season stats {hr_per9, ip, ...}
      pitcher_savant: optional Statcast xStats for the starter
      bullpen_stats: optional team bullpen aggregates {hr_per9, ip, n_relievers}
                     If provided, factor becomes weighted blend of SP and BP.
      lineup_spot: optional batting order (1-9). If known, adjusts SP/BP weight
                   (top of order sees more SP, bottom sees more BP).
    """
    hr9 = _safe(pitcher_stats.get("hr_per9"), league.HR_PER_9)
    ip = _safe(pitcher_stats.get("ip"), 0) or 0
    bf_equiv = ip * 4.3

    obs_rate_per_9 = hr9 if bf_equiv > 0 else league.HR_PER_9
    regressed_hr9 = league.regress(
        observed_rate=obs_rate_per_9,
        observed_n=bf_equiv,
        prior_rate=league.HR_PER_9,
        prior_weight=league.PRIOR_WEIGHT_PITCHER_BF,
    )
    sp_factor = regressed_hr9 / league.HR_PER_9

    # Blend in xERA-based factor if available
    if pitcher_savant:
        xwoba_against = _safe(pitcher_savant.get("xwoba_against"), league.xwOBA_AGAINST)
        x_factor = (xwoba_against / league.xwOBA_AGAINST) ** 1.5
        sp_factor = (sp_factor * 0.7) + (x_factor * 0.3)

    # Bullpen blend — most batters face BP for 1-2 of their PAs.
    # Top of order (1-3) faces SP for ~3/4 PAs, bottom (7-9) for ~2/4 PAs.
    if bullpen_stats and bullpen_stats.get("hr_per9") is not None:
        bp_hr9 = _safe(bullpen_stats.get("hr_per9"), league.HR_PER_9)
        bp_ip = _safe(bullpen_stats.get("ip"), 0) or 0
        bp_bf_equiv = bp_ip * 4.3
        bp_regressed = league.regress(
            observed_rate=bp_hr9,
            observed_n=bp_bf_equiv,
            prior_rate=league.HR_PER_9,
            prior_weight=league.PRIOR_WEIGHT_PITCHER_BF,
        )
        bp_factor = bp_regressed / league.HR_PER_9

        # Spot-aware SP/BP weighting (proportion of PAs vs SP)
        if lineup_spot and 1 <= lineup_spot <= 9:
            sp_weight_table = {
                1: 0.75, 2: 0.72, 3: 0.70,
                4: 0.68, 5: 0.65, 6: 0.62,
                7: 0.58, 8: 0.55, 9: 0.52,
            }
            sp_weight = sp_weight_table[lineup_spot]
        else:
            sp_weight = 0.65   # default — modal batter

        factor = sp_weight * sp_factor + (1 - sp_weight) * bp_factor
    else:
        factor = sp_factor

    return max(0.5, min(2.0, factor))


def pitch_arsenal_factor(pitcher_arsenal: dict | None,
                         batter_pitch_perf: dict | None,
                         league_woba: float = 0.320) -> float:
    """
    Returns a multiplicative HR factor based on the matchup between the
    pitcher's arsenal and the batter's wOBA against each pitch type.

    Computes expected wOBA in this matchup as:
       Σ (pitch_pct[type] × batter_woba_against[type])

    Then converts to HR multiplier with the same elasticity exponent (1.5)
    we use for the handedness factor.

    Either input missing → returns 1.0 (no adjustment).
    """
    if not pitcher_arsenal or not batter_pitch_perf:
        return 1.0

    # Pitch type pairs: usage_key -> woba_key
    pairs = {
        "ff_pct": "woba_against_ff",
        "si_pct": "woba_against_si",
        "sl_pct": "woba_against_sl",
        "cu_pct": "woba_against_cu",
        "ch_pct": "woba_against_ch",
        "fc_pct": "woba_against_fc",
        "st_pct": "woba_against_st",
        "sw_pct": "woba_against_sw",
    }

    expected_woba = 0.0
    total_pct = 0.0
    for pct_key, woba_key in pairs.items():
        pct = _safe(pitcher_arsenal.get(pct_key))
        woba = _safe(batter_pitch_perf.get(woba_key))
        if pct is None or woba is None or pct <= 0:
            continue
        # Savant returns pct as decimal already (e.g., 0.55 for 55%)
        expected_woba += pct * woba
        total_pct += pct

    if total_pct < 0.5:  # not enough overlap to trust
        return 1.0

    # Normalize in case usage doesn't sum to 1.0 (rare)
    expected_woba /= total_pct

    # HR rate scales ~wOBA^1.5 (consistent with handedness factor exponent)
    factor = (expected_woba / league_woba) ** 1.5
    return max(0.7, min(1.4, factor))


def bvp_factor(bvp_data: dict | None) -> float:
    """
    Adjusts the per-PA HR probability based on career batter-vs-pitcher history.
    Regresses toward league average — head-to-head history is noisy at small AB.
    Only applies meaningful weight once AB >= 10; ignored below that threshold.

    At 50 AB the regression is roughly 50/50 model vs BvP history.
    """
    if not bvp_data:
        return 1.0
    ab = int(_safe(bvp_data.get("ab"),  0) or 0)
    hr = int(_safe(bvp_data.get("hr"),  0) or 0)
    if ab < 10:
        return 1.0
    bvp_hr_rate = hr / ab
    # Regress toward league HR/PA with a 30-AB prior so sparse samples
    # don't produce extreme adjustments (e.g. 1 HR in 10 AB = 10%)
    regressed = league.regress(bvp_hr_rate, ab, league.HR_PER_PA, 30)
    factor = regressed / league.HR_PER_PA
    return max(0.5, min(2.5, factor))


def recent_form_factor(pitcher_stats: dict, recent_form: dict | None) -> float:
    """
    Adjusts pitcher matchup factor based on recent (last-3-start) HR/9
    vs season HR/9. Catches in-season velocity dips, pitch-mix changes,
    or fatigue.
    """
    if not recent_form or not recent_form.get("recent_hr_per9"):
        return 1.0
    season_hr9 = _safe(pitcher_stats.get("hr_per9"), league.HR_PER_9)
    recent_hr9 = _safe(recent_form.get("recent_hr_per9"))
    if not season_hr9 or not recent_hr9:
        return 1.0
    if season_hr9 < 0.1:
        return 1.0  # avoid divide-by-zero edge case

    ratio = recent_hr9 / season_hr9
    # Soft exponent — recent form is noisy, don't let 3 starts dominate
    factor = ratio ** 0.3
    return max(0.85, min(1.15, factor))


def park_factor(park_hr_factor: int) -> float:
    """Normalize 100=neutral park factor → multiplier."""
    if not park_hr_factor:
        return 1.0
    return park_hr_factor / 100.0


def park_factor_handed(stadium: dict, batter_hand: str | None) -> float:
    """Park HR factor specific to batter handedness."""
    from data.stadiums import park_factor_for_batter
    raw = park_factor_for_batter(stadium, batter_hand or "R")
    return raw / 100.0


def env_factors(weather: dict | None, park_orientation_deg: float | None = None) -> dict:
    """
    Returns dict of {temp, humidity, wind} multiplicative factors.
    Wind requires the ballpark orientation; if not supplied, neutral.
    """
    if not weather:
        return {"temp": 1.0, "humidity": 1.0, "wind": 1.0, "wind_label": "—"}
    temp_f = _safe(weather.get("temp_f"))
    humid = _safe(weather.get("humidity"))
    wind_mph = _safe(weather.get("wind_mph"), 0) or 0
    wind_dir = _safe(weather.get("wind_dir_deg"))

    # Wind factor — re-use the stadiums helper, returned to caller separately
    from data.stadiums import wind_label, wind_hr_boost
    wlbl = wind_label(wind_dir, park_orientation_deg or 0)
    wboost = wind_hr_boost(wlbl, wind_mph)

    return {
        "temp":     league.temp_factor(temp_f),
        "humidity": league.humidity_factor(humid),
        "wind":     wboost,
        "wind_label": wlbl,
    }


def predict_per_pa(
    batter_savant: dict | None,
    pitcher_stats: dict,
    pitcher_savant: dict | None,
    park_hr: int,
    weather: dict | None,
    park_orientation_deg: float | None = None,
    *,
    stadium: dict | None = None,
    batter_hand: str | None = None,
    pitcher_hand: str | None = None,
    pitcher_splits: dict | None = None,
    recent_stats: dict | None = None,
    pitcher_arsenal: dict | None = None,
    batter_pitch_perf: dict | None = None,
    pitcher_savant_split: dict | None = None,
    pitcher_recent_form: dict | None = None,
    bvp_data: dict | None = None,
    bullpen_stats: dict | None = None,
    lineup_spot: int | None = None,
) -> dict:
    """
    Returns full breakdown plus final per-PA HR probability.

    Optional kwargs:
      - pitcher_arsenal: pitcher pitch-mix dict (ff_pct, sl_pct, ...)
      - batter_pitch_perf: batter wOBA-against-each-pitch-type dict
      - pitcher_savant_split: pitcher xStats split by batter handedness
      - pitcher_recent_form: pitcher last-3-start form dict
      - bvp_data: career batter-vs-pitcher record {ab, hr} for head-to-head adjustment
    """
    base      = league.HR_PER_PA
    f_bat     = batter_skill_factor(batter_savant, recent_stats=recent_stats)
    f_pit     = pitcher_matchup_factor(pitcher_stats, pitcher_savant,
                                       bullpen_stats=bullpen_stats,
                                       lineup_spot=lineup_spot)
    f_park    = park_factor_handed(stadium, batter_hand) if stadium else park_factor(park_hr)
    f_hand    = handedness_factor(batter_hand, pitcher_hand, pitcher_splits, pitcher_savant_split)
    f_arsenal = pitch_arsenal_factor(pitcher_arsenal, batter_pitch_perf)
    f_recent  = recent_form_factor(pitcher_stats, pitcher_recent_form)
    f_bvp     = bvp_factor(bvp_data)
    env       = env_factors(weather, park_orientation_deg)

    p = (base * f_bat * f_pit * f_park * f_hand * f_arsenal * f_recent * f_bvp
         * env["temp"] * env["humidity"] * env["wind"])
    p = max(0.001, min(0.25, p))

    return {
        "p_hr_per_pa":  p,
        "f_batter":     f_bat,
        "f_pitcher":    f_pit,
        "f_park":       f_park,
        "f_hand":       f_hand,
        "f_arsenal":    f_arsenal,
        "f_recent":     f_recent,
        "f_bvp":        f_bvp,
        "f_temp":       env["temp"],
        "f_humidity":   env["humidity"],
        "f_wind":       env["wind"],
        "wind_label":   env["wind_label"],
    }


def _calibrate(p: float) -> float:
    """
    Post-hoc calibration based on 16-day backtest (Apr 24 – May 9, 2026).
    The model is systematically over-confident in the 15–40% range by ~7pp.
    Shrink toward actuals: 25–30% bucket was 27.5% model vs 20.9% actual,
    30–40% was 34.0% vs 26.7%. Factor 0.75 corrects both to within 1pp.
    """
    if 0.15 <= p < 0.40:
        return p * 0.75
    return p


def predict_per_game(per_pa_result: dict, expected_pa: float | None = None) -> dict:
    """
    Convert per-PA HR probability → per-game probabilities.
    Models PAs as independent Bernoulli trials (good first approximation).
    """
    p = per_pa_result["p_hr_per_pa"]
    pa = expected_pa or league.PA_PER_BATTER

    p_at_least_one = _calibrate(1.0 - (1.0 - p) ** pa)
    expected_hrs   = p * pa

    return {
        **per_pa_result,
        "expected_pa":      pa,
        "expected_hrs":     expected_hrs,
        "p_at_least_one":   p_at_least_one,
    }


# Average PAs per game by lineup spot (1=leadoff, 9=last). Empirical 2023-2024.
PA_BY_LINEUP_SPOT = {
    1: 4.72, 2: 4.62, 3: 4.51, 4: 4.41, 5: 4.30,
    6: 4.20, 7: 4.10, 8: 4.00, 9: 3.90,
}


def expected_pa_for_spot(lineup_spot: int | None) -> float:
    """
    Returns empirical avg PAs per game for a given lineup spot.
    Falls back to league mean if spot is unknown.
    """
    if lineup_spot is None or lineup_spot < 1 or lineup_spot > 9:
        return league.PA_PER_BATTER
    return PA_BY_LINEUP_SPOT.get(int(lineup_spot), league.PA_PER_BATTER)


def confidence_label(per_pa_result: dict) -> str:
    """
    Legacy: crude qualitative label based on factor extremity (variance proxy).
    Kept for backwards compatibility with older code paths.
    """
    extreme = abs(per_pa_result["f_batter"] - 1.0) + abs(per_pa_result["f_pitcher"] - 1.0)
    if extreme < 0.4:
        return "Medium"
    if extreme < 0.8:
        return "High"
    return "Wide"


def pick_confidence_score(model_p_pct: float | None,
                          edge_pp: float | None = None) -> int:
    """
    Composite 0-100 confidence score for an HR pick.

    Combines two signals derived from the 27-day backtest:

      1. MODEL PROBABILITY MAGNITUDE (0-60 pts)
         Higher model probability = stronger signal. Penalty applied to
         15-20% bin which was empirically +3pp overconfident.

      2. EDGE VS MARKET (0-40 pts, with red flags)
         Positive edge means the soft market hasn't caught up to the model.
         BUT: extreme positive edge (>15pp) is usually model error (Baldwin
         at +25pp went 0-for-4; market knew better). Heavy penalty for
         suspect-large edges.
         Negative edge means the sharp market disagrees with the model —
         model probably overestimating.

    Returns: integer 0-100, where 0 = fade, 100 = max confidence.
    """
    p = model_p_pct or 0

    # Component 1: model probability magnitude
    if p >= 30:        prob_score = 50
    elif p >= 25:      prob_score = 50
    elif p >= 20:      prob_score = 42
    elif p >= 17:      prob_score = 35
    elif p >= 15:      prob_score = 28
    elif p >= 12:      prob_score = 20
    elif p >= 10:      prob_score = 13
    elif p >= 8:       prob_score = 7
    elif p >= 5:       prob_score = max(0, int((p - 4) * 1.5))
    else:              prob_score = 0

    # Empirical penalty: 15-20% bin overconfidence (+3pp historical bias)
    if 15 <= p < 20:
        prob_score -= 5

    # Component 2: market edge
    if edge_pp is None:
        # No odds — can't assess market disagreement
        edge_score = 20    # neutral / partial credit
    elif edge_pp > 15:
        # Extreme positive edge = likely model error, not real soft line
        edge_score = 5
    elif edge_pp >= 8:
        edge_score = 40
    elif edge_pp >= 5:
        edge_score = 32
    elif edge_pp >= 2:
        edge_score = 22
    elif edge_pp >= 0:
        edge_score = 13
    elif edge_pp >= -3:
        edge_score = 5
    elif edge_pp >= -8:
        edge_score = -5
    else:
        edge_score = -15

    total = max(0, min(100, prob_score + edge_score))
    return int(total)


def pick_confidence_tier(score: int) -> str:
    """
    Maps a confidence score to a tier label + emoji.
      75+  HIGH    — bet with conviction
      55+  MED     — moderate edge, normal stake
      35+  LOW     — small stake or pass
      <35  FADE    — skip
    """
    if score >= 75:
        return "🟢 HIGH"
    if score >= 55:
        return "🟡 MED"
    if score >= 35:
        return "🟠 LOW"
    return "🔴 FADE"


def pick_confidence(model_p_pct: float | None, edge_pp: float | None = None) -> dict:
    """Convenience: returns {score, tier} for a pick."""
    score = pick_confidence_score(model_p_pct, edge_pp)
    return {"score": score, "tier": pick_confidence_tier(score)}


if __name__ == "__main__":
    from data.stadiums import get_stadium

    yankee = get_stadium(147)
    batter = {"brl_percent": 18.0, "ev95percent": 55.0, "xslg": 0.560}
    pitcher = {"hr_per9": 1.8, "ip": 30.0}
    pitcher_x = {"xwoba_against": 0.360}
    splits = {"vsL": {"ops": ".880", "ab": 60}, "vsR": {"ops": ".680", "ab": 90}}
    weather = {"temp_f": 85, "humidity": 35, "wind_mph": 14, "wind_dir_deg": 270}

    # LHB Yankee Stadium short-porch demo with full upgrades
    r = predict_per_pa(
        batter, pitcher, pitcher_x, park_hr=110, weather=weather,
        park_orientation_deg=75,
        stadium=yankee, batter_hand="L", pitcher_hand="R",
        pitcher_splits=splits,
        recent_stats={"hr": 4, "pa": 60},  # hot
    )
    g = predict_per_game(r, expected_pa=4.7)
    print(f"LHB at Yankee, HR-prone RHP, hot recent: P(HR≥1)={g['p_at_least_one']*100:.1f}%")
    print(f"  bat×{g['f_batter']:.2f} pit×{g['f_pitcher']:.2f} park×{g['f_park']:.2f} "
          f"hand×{g['f_hand']:.2f} temp×{g['f_temp']:.2f} wind×{g['f_wind']:.2f}")

    # Same batter as RHB at Fenway (LHB-killer park, vsR pitcher)
    fenway = get_stadium(111)
    r2 = predict_per_pa(
        batter, pitcher, pitcher_x, park_hr=97, weather=weather,
        park_orientation_deg=45,
        stadium=fenway, batter_hand="L", pitcher_hand="R",
        pitcher_splits=splits,
    )
    g2 = predict_per_game(r2, expected_pa=4.3)
    print(f"\nSame LHB at Fenway: P(HR≥1)={g2['p_at_least_one']*100:.1f}%  (park×{r2['f_park']:.2f})")

    # League-avg sanity
    r3 = predict_per_pa({}, {"hr_per9": 1.2, "ip": 100}, None, park_hr=100,
                        weather={"temp_f": 70, "humidity": 50, "wind_mph": 0, "wind_dir_deg": 0},
                        park_orientation_deg=0)
    print(f"\nLeague-avg sanity: per-PA={r3['p_hr_per_pa']:.4f} (target ~{league.HR_PER_PA:.4f})")
