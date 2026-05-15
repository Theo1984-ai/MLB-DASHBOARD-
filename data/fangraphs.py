"""
FanGraphs + Baseball Savant advanced pitcher metrics.

Uses Baseball Savant xERA (better than raw ERA for run prediction) and
builds a pitcher quality lookup keyed by MLBAM player_id.

xERA explanation:
  - Baseball Savant's expected ERA based on quality of contact allowed
  - Strips out luck (BABIP variance, strand rate) better than raw ERA
  - Correlates more strongly with next-season ERA than current ERA does
"""

import io
import urllib.request
import ssl as _ssl_compat
_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
import urllib.error
import pandas as pd
from typing import Optional

USER_AGENT = "mlb-dashboard/1.0"
LEAGUE_XERA = 4.10   # 2026 approximate league-average xERA


def _fetch_csv(url: str) -> pd.DataFrame:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20, context=_UNVERIFIED_SSL) as resp:
            raw = resp.read().decode("utf-8-sig")
    except Exception:
        return pd.DataFrame()
    if not raw.strip() or raw.lstrip().startswith("<"):
        return pd.DataFrame()
    try:
        return pd.read_csv(io.StringIO(raw))
    except Exception:
        return pd.DataFrame()


def pitcher_advanced(year: int) -> pd.DataFrame:
    """
    Return a DataFrame of advanced pitcher metrics from Baseball Savant.
    Columns: player_id, player_name, era, xera, xwoba_against, pa, bip

    xERA is the primary quality metric used to replace raw ERA in run models.
    Falls back to ERA if xERA is missing (< 5 IP or data gap).
    """
    url = (
        f"https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=pitcher&year={year}&min=1&csv=true"
    )
    df = _fetch_csv(url)
    if df.empty:
        return df

    df.columns = [c.strip() for c in df.columns]
    rename = {
        "last_name, first_name": "player_name",
        "est_woba": "xwoba_against",
        "est_ba":   "xba_against",
        "est_slg":  "xslg_against",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    keep = [c for c in [
        "player_id", "player_name", "year", "pa", "bip",
        "era", "xera", "xwoba_against", "xba_against",
    ] if c in df.columns]
    df = df[keep].copy()

    # Ensure numeric
    for col in ["era", "xera", "pa", "bip"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def build_xera_lookup(year: int) -> dict[int, float]:
    """
    Return {mlbam_player_id: xera} for all qualified pitchers.
    Falls back to raw ERA if xERA is missing.
    If neither available, caller should use LEAGUE_XERA.
    """
    df = pitcher_advanced(year)
    if df.empty:
        return {}

    lookup: dict[int, float] = {}
    for _, row in df.iterrows():
        pid = int(row.get("player_id", 0))
        if not pid:
            continue
        xera = row.get("xera")
        era  = row.get("era")
        # Use xERA if present and reasonable; fall back to ERA
        if pd.notna(xera) and 0.5 < float(xera) < 10.0:
            lookup[pid] = float(xera)
        elif pd.notna(era) and 0.5 < float(era) < 10.0:
            lookup[pid] = float(era)
    return lookup


def get_pitcher_xera(player_id: Optional[int], lookup: dict[int, float]) -> float:
    """
    Return xERA for a pitcher from pre-built lookup.
    Falls back to LEAGUE_XERA if not found.
    """
    if player_id is None:
        return LEAGUE_XERA
    return lookup.get(int(player_id), LEAGUE_XERA)


if __name__ == "__main__":
    print("Testing pitcher advanced metrics...")
    lk = build_xera_lookup(2026)
    print(f"Loaded {len(lk)} pitchers")
    df = pitcher_advanced(2026)
    if not df.empty:
        sample = df.sort_values("xera").head(10)
        print(sample[["player_name", "era", "xera", "pa"]].to_string(index=False))
