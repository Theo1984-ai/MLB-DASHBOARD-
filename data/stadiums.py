"""
Static metadata for all 30 MLB ballparks.

Keys = MLB Stats API team_id (one team per stadium except LAA/LAD/NYY/NYM/CWS/CHC).
- lat, lon: ballpark coordinates (for weather lookup)
- orientation_deg: compass bearing from home plate to dead center field
                   (used to translate wind direction into LF/CF/RF terminology)
- park: ballpark name
- hr_factor: park HR factor (100 = neutral; >100 favors HR; <100 suppresses)
             Source: Baseball Savant 2024 published park factors (HR index)
"""

STADIUMS = {
    # hr_factor_lhb / hr_factor_rhb from Baseball Savant 2023-2024 published park factors (HR index, batter side)
    108: {"park": "Angel Stadium",         "lat": 33.8003, "lon": -117.8827, "orientation_deg":  56, "hr_factor": 100, "hr_factor_lhb": 102, "hr_factor_rhb":  98},
    109: {"park": "Chase Field",           "lat": 33.4453, "lon": -112.0667, "orientation_deg":   0, "hr_factor": 102, "hr_factor_lhb": 100, "hr_factor_rhb": 104},
    110: {"park": "Oriole Park at Camden", "lat": 39.2839, "lon":  -76.6217, "orientation_deg":  62, "hr_factor": 105, "hr_factor_lhb": 100, "hr_factor_rhb": 110},
    111: {"park": "Fenway Park",           "lat": 42.3467, "lon":  -71.0972, "orientation_deg":  45, "hr_factor":  97, "hr_factor_lhb":  85, "hr_factor_rhb": 110},
    112: {"park": "Wrigley Field",         "lat": 41.9484, "lon":  -87.6553, "orientation_deg":  31, "hr_factor":  99, "hr_factor_lhb": 100, "hr_factor_rhb":  98},
    113: {"park": "Great American Ball Pk","lat": 39.0975, "lon":  -84.5070, "orientation_deg":  40, "hr_factor": 113, "hr_factor_lhb": 116, "hr_factor_rhb": 110},
    114: {"park": "Progressive Field",     "lat": 41.4962, "lon":  -81.6852, "orientation_deg":   2, "hr_factor":  98, "hr_factor_lhb": 100, "hr_factor_rhb":  96},
    115: {"park": "Coors Field",           "lat": 39.7559, "lon": -104.9942, "orientation_deg":   0, "hr_factor": 112, "hr_factor_lhb": 110, "hr_factor_rhb": 114},
    116: {"park": "Comerica Park",         "lat": 42.3390, "lon":  -83.0485, "orientation_deg": 150, "hr_factor":  93, "hr_factor_lhb":  92, "hr_factor_rhb":  94},
    117: {"park": "Daikin Park",           "lat": 29.7572, "lon":  -95.3552, "orientation_deg": 345, "hr_factor": 102, "hr_factor_lhb": 100, "hr_factor_rhb": 104},
    118: {"park": "Kauffman Stadium",      "lat": 39.0517, "lon":  -94.4803, "orientation_deg":  45, "hr_factor":  97, "hr_factor_lhb":  98, "hr_factor_rhb":  96},
    119: {"park": "Dodger Stadium",        "lat": 34.0739, "lon": -118.2400, "orientation_deg":  25, "hr_factor": 102, "hr_factor_lhb": 105, "hr_factor_rhb":  99},
    120: {"park": "Nationals Park",        "lat": 38.8730, "lon":  -77.0074, "orientation_deg":  30, "hr_factor": 100, "hr_factor_lhb": 102, "hr_factor_rhb":  98},
    121: {"park": "Citi Field",            "lat": 40.7571, "lon":  -73.8458, "orientation_deg":  20, "hr_factor":  95, "hr_factor_lhb":  93, "hr_factor_rhb":  97},
    133: {"park": "Sutter Health Park",    "lat": 38.5800, "lon": -121.5130, "orientation_deg":  53, "hr_factor": 105, "hr_factor_lhb": 105, "hr_factor_rhb": 105},
    134: {"park": "PNC Park",              "lat": 40.4469, "lon":  -80.0058, "orientation_deg":  90, "hr_factor":  92, "hr_factor_lhb":  88, "hr_factor_rhb":  96},
    135: {"park": "Petco Park",            "lat": 32.7076, "lon": -117.1570, "orientation_deg":   1, "hr_factor":  91, "hr_factor_lhb":  90, "hr_factor_rhb":  92},
    136: {"park": "T-Mobile Park",         "lat": 47.5914, "lon": -122.3325, "orientation_deg":  45, "hr_factor":  93, "hr_factor_lhb":  92, "hr_factor_rhb":  94},
    137: {"park": "Oracle Park",           "lat": 37.7786, "lon": -122.3893, "orientation_deg":  90, "hr_factor":  88, "hr_factor_lhb":  82, "hr_factor_rhb":  92},
    138: {"park": "Busch Stadium",         "lat": 38.6226, "lon":  -90.1928, "orientation_deg":  56, "hr_factor":  96, "hr_factor_lhb":  96, "hr_factor_rhb":  96},
    139: {"park": "Tropicana Field",       "lat": 27.7682, "lon":  -82.6534, "orientation_deg":  41, "hr_factor":  95, "hr_factor_lhb":  93, "hr_factor_rhb":  97},
    140: {"park": "Globe Life Field",      "lat": 32.7473, "lon":  -97.0817, "orientation_deg":  76, "hr_factor": 102, "hr_factor_lhb": 102, "hr_factor_rhb": 102},
    141: {"park": "Rogers Centre",         "lat": 43.6414, "lon":  -79.3894, "orientation_deg":   0, "hr_factor": 102, "hr_factor_lhb": 100, "hr_factor_rhb": 104},
    142: {"park": "Target Field",          "lat": 44.9817, "lon":  -93.2778, "orientation_deg":  90, "hr_factor":  98, "hr_factor_lhb":  98, "hr_factor_rhb":  98},
    143: {"park": "Citizens Bank Park",    "lat": 39.9061, "lon":  -75.1665, "orientation_deg":  18, "hr_factor": 105, "hr_factor_lhb": 108, "hr_factor_rhb": 102},
    144: {"park": "Truist Park",           "lat": 33.8908, "lon":  -84.4678, "orientation_deg":  60, "hr_factor": 100, "hr_factor_lhb": 100, "hr_factor_rhb": 100},
    145: {"park": "Rate Field",            "lat": 41.8300, "lon":  -87.6339, "orientation_deg":  43, "hr_factor": 102, "hr_factor_lhb": 102, "hr_factor_rhb": 102},
    146: {"park": "loanDepot park",        "lat": 25.7781, "lon":  -80.2197, "orientation_deg":  40, "hr_factor":  90, "hr_factor_lhb":  88, "hr_factor_rhb":  92},
    147: {"park": "Yankee Stadium",        "lat": 40.8296, "lon":  -73.9262, "orientation_deg":  75, "hr_factor": 110, "hr_factor_lhb": 125, "hr_factor_rhb":  98},
    158: {"park": "American Family Field", "lat": 43.0280, "lon":  -87.9712, "orientation_deg":   0, "hr_factor": 102, "hr_factor_lhb": 105, "hr_factor_rhb":  99},
}


def get_stadium(team_id: int) -> dict:
    """Returns stadium metadata for a home team_id, or a neutral fallback."""
    return STADIUMS.get(team_id, {
        "park": "Unknown", "lat": 0.0, "lon": 0.0,
        "orientation_deg": 0, "hr_factor": 100,
        "hr_factor_lhb": 100, "hr_factor_rhb": 100,
    })


def park_factor_for_batter(stadium: dict, bat_side: str) -> int:
    """
    Returns the handedness-specific HR factor.
    bat_side: 'L', 'R', or 'S' (switch-hitter — average the two sides).
    """
    if bat_side == "L":
        return stadium.get("hr_factor_lhb", stadium.get("hr_factor", 100))
    if bat_side == "R":
        return stadium.get("hr_factor_rhb", stadium.get("hr_factor", 100))
    # Switch-hitter: average (they bat the opposite hand of the pitcher,
    # but at this point we don't have pitcher hand here; mean is a safe default)
    lhb = stadium.get("hr_factor_lhb", 100)
    rhb = stadium.get("hr_factor_rhb", 100)
    return int((lhb + rhb) / 2)


def wind_label(wind_dir_deg: float, park_orientation_deg: float) -> str:
    """
    Converts a meteorological wind direction (degrees, where the wind is FROM)
    into a baseball-relevant label relative to the park's orientation.

    Wind 'blowing out' = wind moving toward the outfield = wind FROM home plate
    direction (i.e., wind dir = orientation - 180 mod 360).
    """
    if wind_dir_deg is None:
        return "—"
    # Convert "from" direction to "to" direction (where the wind is going)
    wind_to = (wind_dir_deg + 180) % 360
    # Angle relative to CF
    rel = (wind_to - park_orientation_deg) % 360
    if rel > 180:
        rel -= 360  # now in [-180, 180]

    if -22.5 <= rel <= 22.5:
        return "Out to CF"
    if 22.5 < rel <= 67.5:
        return "Out to RF"
    if 67.5 < rel <= 112.5:
        return "Cross L→R"
    if 112.5 < rel <= 157.5:
        return "In from RF"
    if rel > 157.5 or rel < -157.5:
        return "In from CF"
    if -157.5 <= rel < -112.5:
        return "In from LF"
    if -112.5 <= rel < -67.5:
        return "Cross R→L"
    if -67.5 <= rel < -22.5:
        return "Out to LF"
    return "—"


def wind_hr_boost(wind_label_str: str, wind_mph: float) -> float:
    """
    Returns a multiplicative HR boost based on wind direction + speed.

    Coefficients re-derived from a 27-day backtest (5,759 predictions,
    575 actual HRs). Old uniform ±1.5%/mph under-counted "In from CF"
    suppression (true effect ~-4pp HR rate) and over-counted out-to-LF
    (true effect only +0.4pp). New per-direction coefs:

      Out to RF:    +2.0%/mph above 5mph   (empirical: +1.56pp HR rate)
      Out to CF:    +1.0%/mph                (empirical: +0.15pp)
      Out to LF:    +1.0%/mph                (empirical: +0.41pp)
      Cross winds:  +0.5%/mph                (empirical: +0.21-0.75pp)
      In from LF:   -1.5%/mph                (empirical: -0.68pp)
      In from RF:   -2.5%/mph                (empirical: -1.80pp)
      In from CF:   -4.0%/mph                (empirical: -4.10pp HR rate)

    Floor/ceiling: clamp to [0.45, 1.40] to prevent runaway in extreme wind.
    """
    if wind_mph is None or wind_mph < 5:
        return 1.0
    excess = wind_mph - 5

    # Per-direction coefficients (multiplier delta per mph above 5)
    DIR_COEFS = {
        "Out to RF":   +0.020,   # strong out (toward short RF in most parks)
        "Out to CF":   +0.010,   # moderate out
        "Out to LF":   +0.010,   # moderate out (short LF mostly Yankee/Wrigley)
        "Cross L→R":   +0.005,   # marginal positive
        "Cross R→L":   +0.005,   # marginal positive
        "In from LF":  -0.015,
        "In from CF":  -0.040,   # strongest suppression (well-validated)
        "In from RF":  -0.025,
    }
    coef = DIR_COEFS.get(wind_label_str, 0.0)
    boost = 1.0 + coef * excess
    # Clamp to plausible range
    return max(0.45, min(1.40, boost))


if __name__ == "__main__":
    print(f"{len(STADIUMS)} stadiums configured")
    s = get_stadium(147)  # Yankees
    print(f"Yankee Stadium: {s}")
    # 270° = wind from west, blowing east. Yankee orientation 75° → wind to 90° → rel=15 → "Out to CF"
    print(f"Wind from W (270°), 12mph → {wind_label(270, 75)}, boost={wind_hr_boost(wind_label(270, 75), 12):.3f}")
    print(f"Wind from E (90°), 12mph → {wind_label(90, 75)}, boost={wind_hr_boost(wind_label(90, 75), 12):.3f}")
