"""
Baseball Savant CSV leaderboard scrapers.
All endpoints accept ?csv=true and return CSV with player_id (MLBAM ID,
identical to MLB Stats API person.id — used for joins).
"""

import urllib.request
import ssl
# System cert chain is broken on this Windows install — use unverified
# context for these public-data APIs. No auth/PII flows through these endpoints.
_UNVERIFIED_SSL = ssl._create_unverified_context()
import urllib.error
import io
import pandas as pd

USER_AGENT = "mlb-dashboard/1.0"


def _fetch_csv(url: str) -> pd.DataFrame:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20, context=_UNVERIFIED_SSL) as resp:
            raw = resp.read().decode("utf-8-sig")
    except (urllib.error.URLError, TimeoutError):
        return pd.DataFrame()
    if not raw.strip() or raw.lstrip().startswith("<"):
        return pd.DataFrame()
    try:
        df = pd.read_csv(io.StringIO(raw))
    except Exception:
        return pd.DataFrame()
    # Strip whitespace from column names
    df.columns = [c.strip() for c in df.columns]
    return df


def batter_xstats(year: int, min_pa: str = "q") -> pd.DataFrame:
    """
    Batter expected statistics (xBA, xSLG, xwOBA).
    Columns include: player_id, est_ba, est_slg, est_woba, ba, slg, woba.
    """
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=batter&year={year}&min={min_pa}&csv=true"
    )
    df = _fetch_csv(url)
    if df.empty:
        return df
    df = df.rename(columns={
        "last_name, first_name": "player_name",
        "est_ba": "xba", "est_slg": "xslg", "est_woba": "xwoba",
    })
    keep = [c for c in [
        "player_id", "player_name", "year", "pa", "bip",
        "ba", "xba", "slg", "xslg", "woba", "xwoba",
    ] if c in df.columns]
    return df[keep]


def batter_statcast(year: int, min_pa: str = "q") -> pd.DataFrame:
    """
    Batter Statcast batted-ball data:
    barrels, brl_percent, brl_pa, max_hit_speed, avg_hit_speed,
    ev95plus, ev95percent, max_distance, avg_hr_distance,
    anglesweetspotpercent.
    """
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/statcast"
        f"?type=batter&year={year}&min={min_pa}&csv=true"
    )
    df = _fetch_csv(url)
    if df.empty:
        return df
    df = df.rename(columns={"last_name, first_name": "player_name"})
    keep = [c for c in [
        "player_id", "player_name", "attempts",
        "avg_hit_speed", "max_hit_speed", "ev50",
        "anglesweetspotpercent", "max_distance", "avg_hr_distance",
        "ev95plus", "ev95percent", "barrels", "brl_percent", "brl_pa",
    ] if c in df.columns]
    return df[keep]


def pitcher_xstats(year: int, min_pa: str = "q") -> pd.DataFrame:
    """
    Pitcher expected statistics (xERA, est_woba allowed).
    """
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=pitcher&year={year}&min={min_pa}&csv=true"
    )
    df = _fetch_csv(url)
    if df.empty:
        return df
    df = df.rename(columns={
        "last_name, first_name": "player_name",
        "est_ba": "xba_against", "est_slg": "xslg_against",
        "est_woba": "xwoba_against",
    })
    keep = [c for c in [
        "player_id", "player_name", "year", "pa", "bip",
        "ba", "xba_against", "slg", "xslg_against",
        "woba", "xwoba_against", "era", "xera",
    ] if c in df.columns]
    return df[keep]


def pitcher_statcast(year: int, min_pa: str = "q") -> pd.DataFrame:
    """Pitcher Statcast batted-ball data ALLOWED."""
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/statcast"
        f"?type=pitcher&year={year}&min={min_pa}&csv=true"
    )
    df = _fetch_csv(url)
    if df.empty:
        return df
    df = df.rename(columns={"last_name, first_name": "player_name"})
    keep = [c for c in [
        "player_id", "player_name", "attempts",
        "avg_hit_speed", "max_hit_speed",
        "anglesweetspotpercent", "max_distance",
        "ev95percent", "barrels", "brl_percent", "brl_pa",
    ] if c in df.columns]
    return df[keep]


# ---------------------------------------------------------------------------
# Tier-2 sources: pitch arsenal, batter wOBA-by-pitch-type, handedness splits
# ---------------------------------------------------------------------------

def pitcher_arsenal(year: int) -> pd.DataFrame:
    """
    Pitcher pitch-mix usage and average velocities for each pitch type.
    Returns columns: player_id, ff_pct, si_pct, sl_pct, cu_pct, ch_pct,
    fc_pct, ff_avg_speed, plus formatted counts.

    Pitchers must be qualified (≥ ~50 IP). Non-qualified starters return
    no row → caller should default to no arsenal adjustment.
    """
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/custom"
        f"?year={year}&type=pitcher&filter=&min=q"
        f"&selections=ff_avg_speed,ff_pct,si_pct,sl_pct,cu_pct,ch_pct,fc_pct,"
        f"st_pct,sw_pct,kn_pct,fs_pct"
        f"&chart=false&x=ff_pct&y=ff_pct&r=no&chartType=beeswarm&csv=true"
    )
    df = _fetch_csv(url)
    if df.empty:
        return df
    df = df.rename(columns={"last_name, first_name": "player_name"})
    return df


def batter_vs_pitch_types(year: int) -> pd.DataFrame:
    """
    Batter wOBA against each pitch type. Used to match a pitcher's arsenal
    to a batter's strengths/weaknesses.

    Columns: player_id, woba_against_ff, woba_against_si, woba_against_sl,
    woba_against_cu (and possibly _ch / _fc / _st / _sw if available).
    """
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/custom"
        f"?year={year}&type=batter&filter=&min=q"
        f"&selections=woba_against_ff,woba_against_si,woba_against_sl,"
        f"woba_against_cu,woba_against_ch,woba_against_fc,woba_against_st,"
        f"woba_against_sw"
        f"&chart=false&x=woba_against_ff&y=woba_against_ff&r=no&chartType=beeswarm&csv=true"
    )
    df = _fetch_csv(url)
    if df.empty:
        return df
    df = df.rename(columns={"last_name, first_name": "player_name"})
    return df


def pitcher_xstats_split(year: int, bat_side: str) -> pd.DataFrame:
    """
    Pitcher xERA / xwOBA / xSLG / brl_pa restricted to one batter handedness.
    bat_side: "L" or "R".
    """
    if bat_side not in ("L", "R"):
        raise ValueError("bat_side must be 'L' or 'R'")
    filter_key = "batters_faced_split_l" if bat_side == "L" else "batters_faced_split_r"
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/custom"
        f"?year={year}&type=pitcher&filter={filter_key}&min=q"
        f"&selections=xera,xwoba,xslg,xba,brl_pa,brl_percent"
        f"&chart=false&x=xera&y=xera&r=no&chartType=beeswarm&csv=true"
    )
    df = _fetch_csv(url)
    if df.empty:
        return df
    df = df.rename(columns={
        "last_name, first_name": "player_name",
        "xera":   f"xera_vs_{bat_side.lower()}",
        "xwoba":  f"xwoba_vs_{bat_side.lower()}",
        "xslg":   f"xslg_vs_{bat_side.lower()}",
        "xba":    f"xba_vs_{bat_side.lower()}",
        "brl_pa": f"brl_pa_vs_{bat_side.lower()}",
        "brl_percent": f"brl_pct_vs_{bat_side.lower()}",
    })
    return df


if __name__ == "__main__":
    import sys
    yr = 2026
    print("Batter xStats...")
    bx = batter_xstats(yr)
    print(f"  rows={len(bx)} cols={list(bx.columns)}")
    if not bx.empty:
        top = bx.sort_values("xslg", ascending=False).head(3)
        print(top[["player_name", "xba", "xslg", "xwoba"]].to_string(index=False))
    print("\nBatter Statcast...")
    bs = batter_statcast(yr)
    print(f"  rows={len(bs)} cols={list(bs.columns)}")
    if not bs.empty:
        top = bs.sort_values("brl_percent", ascending=False).head(3)
        print(top[["player_name", "brl_percent", "avg_hit_speed", "max_distance"]].to_string(index=False))
    print("\nPitcher xStats...")
    px = pitcher_xstats(yr)
    print(f"  rows={len(px)} cols={list(px.columns)}")
    if not px.empty:
        top = px.sort_values("xera").head(3)
        print(top[["player_name", "era", "xera"]].to_string(index=False))

    print("\nPitcher arsenal...")
    pa = pitcher_arsenal(yr)
    print(f"  rows={len(pa)} cols={list(pa.columns)}")
    if not pa.empty:
        sample = pa.head(3)[["player_name", "ff_pct", "si_pct", "sl_pct", "cu_pct"]]
        print(sample.to_string(index=False))

    print("\nBatter vs pitch types...")
    bvt = batter_vs_pitch_types(yr)
    print(f"  rows={len(bvt)} cols={list(bvt.columns)}")
    if not bvt.empty:
        sample = bvt.head(3)[["player_name", "woba_against_ff", "woba_against_sl"]]
        print(sample.to_string(index=False))

    print("\nPitcher xStats vs LHB...")
    pxL = pitcher_xstats_split(yr, "L")
    print(f"  rows={len(pxL)} cols={list(pxL.columns)}")
    print("\nPitcher xStats vs RHB...")
    pxR = pitcher_xstats_split(yr, "R")
    print(f"  rows={len(pxR)} cols={list(pxR.columns)}")
