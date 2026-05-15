"""
Game-level prediction: expected runs per team, total runs (O/U sense),
and win probability.

Approach (no historical training data — purely a-priori construction):
  1. Each team's "true offense" is a weighted blend of season OPS,
     last-15-day OPS (recency boost), and league average (regression).
  2. Each pitcher's run-suppression skill is xERA (preferred) or ERA,
     blended with bullpen via team season pitching ERA.
  3. Combine: matchup_ERA = (team_off_factor × pitcher_runs_against_factor)
     translated to expected runs.
  4. Apply park factor and temperature/humidity environment.
  5. Win probability via a Pythagorean-style log5 on expected runs.
"""

import math
from . import league


def _safe(v, default=None):
    try:
        f = float(v)
        if f != f:
            return default
        return f
    except (ValueError, TypeError):
        return default


def offense_factor(team_season: dict, team_recent: dict | None) -> float:
    """
    Team-level offense multiplier vs league average.
    OPS-based, with recency-weighted blend.
    """
    season_ops = _safe((team_season or {}).get("ops"), league.OPS_AGAINST)
    recent_ops = _safe((team_recent or {}).get("ops"), season_ops)

    games_season = _safe((team_season or {}).get("gamesPlayed"), 0) or 0
    games_recent = _safe((team_recent or {}).get("gamesPlayed"), 0) or 0

    # Regress season toward .720 league avg
    season_reg = league.regress(
        observed_rate=season_ops,
        observed_n=games_season * 38,  # PA per game
        prior_rate=league.OPS_AGAINST,
        prior_weight=400,
    )
    recent_reg = league.regress(
        observed_rate=recent_ops,
        observed_n=games_recent * 38,
        prior_rate=season_reg,  # use season as the prior for recent
        prior_weight=200,
    )

    # 70% season skill / 30% recent form
    blended = 0.70 * season_reg + 0.30 * recent_reg
    # Translate OPS ratio → run-scoring ratio. Empirically, runs scale ~OPS^1.83.
    return (blended / league.OPS_AGAINST) ** 1.83


def pitcher_run_factor(pitcher_stats: dict, pitcher_savant: dict | None) -> float:
    """
    Returns multiplier vs league: 1.0 = league-avg pitcher, <1 suppresses runs.
    Prefer xERA; fall back to ERA. Regress short samples toward 4.20.
    """
    ip = _safe(pitcher_stats.get("ip"), 0) or 0
    bf = ip * 4.3

    era = _safe(pitcher_stats.get("era"), league.ERA)
    xera = None
    if pitcher_savant:
        xera = _safe(pitcher_savant.get("xera"))

    base = xera if xera is not None else era
    regressed = league.regress(
        observed_rate=base,
        observed_n=bf,
        prior_rate=league.ERA,
        prior_weight=league.PRIOR_WEIGHT_PITCHER_BF,
    )
    return regressed / league.ERA


def expected_runs(
    team_offense_factor: float,
    opp_pitcher_factor: float,
    opp_bullpen_era: float | None,
    park_hr: int,
    weather: dict | None,
    opp_bullpen_ip: float | None = None,
) -> float:
    """
    Convert factors into expected runs scored by this team.

    Park HR factor is a proxy for overall offensive park factor (HR-friendly
    parks tend to be runs-friendly, though not identically — we soften it).

    If `opp_bullpen_ip` is supplied alongside opp_bullpen_era, the bullpen
    factor is lightly regressed toward league average for low-IP samples.
    """
    park_runs_factor = 1.0 + 0.5 * ((park_hr or 100) / 100.0 - 1.0)

    temp  = league.temp_factor(_safe((weather or {}).get("temp_f"))) ** 0.5
    humid = league.humidity_factor(_safe((weather or {}).get("humidity"))) ** 0.5

    # Bullpen factor — regress to league when sample is small
    bp_era = _safe(opp_bullpen_era, league.ERA) if opp_bullpen_era is not None else league.ERA
    bp_ip  = opp_bullpen_ip or 0
    if bp_ip > 0:
        # Treat each IP as ~4.3 batters faced; prior weight 80 BF
        bp_era_reg = league.regress(
            observed_rate=bp_era,
            observed_n=bp_ip * 4.3,
            prior_rate=league.ERA,
            prior_weight=80,
        )
    else:
        bp_era_reg = league.ERA
    bullpen_factor = bp_era_reg / league.ERA

    # Blend opposing starter (60% — typical 5.4 IP) with bullpen (40%)
    pitching_factor = 0.60 * opp_pitcher_factor + 0.40 * bullpen_factor

    return (
        league.RUNS_PER_GAME
        * team_offense_factor
        * pitching_factor
        * park_runs_factor
        * temp
        * humid
    )


def win_probability(home_exp_runs: float, away_exp_runs: float) -> float:
    """
    Pythagorean-style win probability.
    Uses Bill James's pythagorean exponent (~1.83) translated to a single game
    via a logistic on the run differential.

    For a single game (high variance), we calibrate slope so that a 1-run
    expected differential ≈ 56% favorite, 2-run ≈ 62%, 3-run ≈ 68%.
    """
    diff = home_exp_runs - away_exp_runs
    # Logistic with slope tuned to approximate single-game variance
    p = 1.0 / (1.0 + math.exp(-0.40 * diff))
    return max(0.05, min(0.95, p))


def predict_game(
    home_team_season, home_team_recent,
    away_team_season, away_team_recent,
    home_pitcher_stats, home_pitcher_savant,
    away_pitcher_stats, away_pitcher_savant,
    home_team_pitching_season,
    away_team_pitching_season,
    park_hr, weather,
    home_bullpen=None,
    away_bullpen=None,
) -> dict:
    """
    Return full prediction breakdown for a single game.

    home_bullpen / away_bullpen: optional dict with {era, ip} from
    mlb_api.get_team_bullpen_stats(). When supplied, takes precedence
    over the team-pitching-season fallback for the bullpen factor.
    """
    home_off = offense_factor(home_team_season, home_team_recent)
    away_off = offense_factor(away_team_season, away_team_recent)
    home_pit = pitcher_run_factor(home_pitcher_stats, home_pitcher_savant)
    away_pit = pitcher_run_factor(away_pitcher_stats, away_pitcher_savant)

    # Prefer isolated-bullpen stats; fall back to team-season pitching ERA.
    if home_bullpen and home_bullpen.get("era"):
        home_bp_era = home_bullpen["era"]
        home_bp_ip  = home_bullpen.get("ip")
    else:
        home_bp_era = _safe((home_team_pitching_season or {}).get("era"))
        home_bp_ip  = None
    if away_bullpen and away_bullpen.get("era"):
        away_bp_era = away_bullpen["era"]
        away_bp_ip  = away_bullpen.get("ip")
    else:
        away_bp_era = _safe((away_team_pitching_season or {}).get("era"))
        away_bp_ip  = None

    # Each team faces the OTHER team's pitching
    home_runs = expected_runs(home_off, away_pit, away_bp_era, park_hr, weather, opp_bullpen_ip=away_bp_ip)
    away_runs = expected_runs(away_off, home_pit, home_bp_era, park_hr, weather, opp_bullpen_ip=home_bp_ip)

    # Slight home-field advantage: ~3% boost in win prob, modeled as +0.15 runs
    home_runs += 0.15

    total = home_runs + away_runs
    p_home = win_probability(home_runs, away_runs)

    return {
        "home_exp_runs":  home_runs,
        "away_exp_runs":  away_runs,
        "total_runs":     total,
        "p_home_win":     p_home,
        "p_away_win":     1.0 - p_home,
        "home_off_factor":  home_off,
        "away_off_factor":  away_off,
        "home_pitch_factor": home_pit,
        "away_pitch_factor": away_pit,
        "home_bullpen_era":  home_bp_era,
        "away_bullpen_era":  away_bp_era,
    }


if __name__ == "__main__":
    # Sanity: even matchup, neutral park, 70°F → ~4.5 R per side, ~50/50
    even = predict_game(
        home_team_season={"ops": 0.720, "gamesPlayed": 30},
        home_team_recent={"ops": 0.720, "gamesPlayed": 15},
        away_team_season={"ops": 0.720, "gamesPlayed": 30},
        away_team_recent={"ops": 0.720, "gamesPlayed": 15},
        home_pitcher_stats={"era": 4.20, "ip": 50},
        home_pitcher_savant={"xera": 4.20},
        away_pitcher_stats={"era": 4.20, "ip": 50},
        away_pitcher_savant={"xera": 4.20},
        home_team_pitching_season={"era": 4.20},
        away_team_pitching_season={"era": 4.20},
        park_hr=100,
        weather={"temp_f": 70, "humidity": 50},
    )
    print(f"Even matchup: home={even['home_exp_runs']:.2f}  away={even['away_exp_runs']:.2f} "
          f"total={even['total_runs']:.2f} p_home={even['p_home_win']*100:.1f}%")

    # Strong home team vs weak away
    strong = predict_game(
        home_team_season={"ops": 0.820, "gamesPlayed": 30},
        home_team_recent={"ops": 0.840, "gamesPlayed": 15},
        away_team_season={"ops": 0.660, "gamesPlayed": 30},
        away_team_recent={"ops": 0.640, "gamesPlayed": 15},
        home_pitcher_stats={"era": 2.80, "ip": 50},
        home_pitcher_savant={"xera": 3.00},
        away_pitcher_stats={"era": 5.50, "ip": 50},
        away_pitcher_savant={"xera": 5.20},
        home_team_pitching_season={"era": 3.50},
        away_team_pitching_season={"era": 5.00},
        park_hr=110,
        weather={"temp_f": 85, "humidity": 40},
    )
    print(f"Mismatch (home favored): home={strong['home_exp_runs']:.2f}  "
          f"away={strong['away_exp_runs']:.2f}  p_home={strong['p_home_win']*100:.1f}%")
