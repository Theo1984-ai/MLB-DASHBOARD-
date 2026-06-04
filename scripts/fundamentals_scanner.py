"""
Fundamentals-only prop scanner.

Applies the framework:
  1. xwOBA / xSLG percentile (luck-stripped quality of contact)
  2. Hard-Hit % + Barrel % percentile (regression signal)
  3. Macro pitch-type / L-R splits (NOT individual BvP)
  4. Cross-book line dispersion (sharp money / line movement proxy)

Returns ranked prop plays per game.
"""
from __future__ import annotations

import json
import ssl as _ssl
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd

from data import savant, mlb_api

_SSL = _ssl._create_unverified_context()
SHARP_BOOKS = "draftkings,fanduel,betmgm,williamhill_us,bovada"


def _amer_to_imp(am):
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def _ev_per_100(am, p):
    if am > 0:
        return p * am - (1 - p) * 100
    return p * 100 - (1 - p) * abs(am)


def _percentile(df, col):
    if col not in df.columns or df[col].isna().all():
        return {}
    s = df[col].dropna()
    rank = s.rank(pct=True) * 100
    return dict(zip(df.loc[s.index, "player_id"], rank))


def _name_match(prop_name, target):
    """Loose match: 'Bobby Witt Jr.' should match 'Witt Jr., Bobby'."""
    pn = (prop_name or "").lower().replace(",", "").replace(".", "").strip()
    tn = (target or "").lower().replace(",", "").replace(".", "").strip()
    pn_parts = set(pn.split())
    tn_parts = set(tn.split())
    return len(pn_parts & tn_parts) >= 2


# ---------- Statcast composite ----------

def load_statcast_composites(season):
    """Returns dict {player_id: composite_percentile}.

    composite = avg(xwOBA pct, xSLG pct, Hard-Hit pct, Barrel pct)

    Also returns the individual percentile dicts + raw values for display.
    """
    bs = savant.batter_statcast(season)
    bx = savant.batter_xstats(season)

    xwoba_pct = _percentile(bx, "xwoba") if "xwoba" in bx.columns else {}
    xslg_pct = _percentile(bx, "xslg") if "xslg" in bx.columns else {}

    hh_col = next((c for c in ("hard_hit_percent", "hard_hit_pct")
                   if c in bs.columns), None)
    br_col = next((c for c in ("barrel_batted_rate", "barrels_per_pa_percent",
                                "brl_percent", "barrel_rate")
                   if c in bs.columns), None)
    hh_pct = _percentile(bs, hh_col) if hh_col else {}
    br_pct = _percentile(bs, br_col) if br_col else {}

    # Raw values for display
    raw = {}
    for _, row in bx.iterrows():
        pid = row.get("player_id")
        if pid is None: continue
        raw[pid] = {"xwoba": row.get("xwoba"), "xslg": row.get("xslg"),
                    "name": row.get("player_name", "?")}
    if hh_col:
        for _, row in bs.iterrows():
            pid = row.get("player_id")
            if pid is None: continue
            raw.setdefault(pid, {})["hard_hit"] = row.get(hh_col)
    if br_col:
        for _, row in bs.iterrows():
            pid = row.get("player_id")
            if pid is None: continue
            raw.setdefault(pid, {})["barrel"] = row.get(br_col)

    composites = {}
    for pid in set(list(xwoba_pct.keys()) + list(xslg_pct.keys()) +
                   list(hh_pct.keys()) + list(br_pct.keys())):
        parts = []
        for d in (xwoba_pct, xslg_pct, hh_pct, br_pct):
            v = d.get(pid)
            if v is not None: parts.append(v)
        if parts:
            composites[pid] = sum(parts) / len(parts)

    return {
        "composites": composites,
        "raw": raw,
        "xwoba_pct": xwoba_pct, "xslg_pct": xslg_pct,
        "hh_pct": hh_pct, "br_pct": br_pct,
        "has_hh": hh_col is not None, "has_br": br_col is not None,
    }


# ---------- Today's slate + elite batters ----------

def find_elite_batters_today(composites_data, season, min_composite=70):
    """Returns list of {batter info, opp_pitcher info} for today's games."""
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    today = datetime.now(tz=ET).strftime("%Y-%m-%d")

    games = mlb_api.get_schedule(today)
    now_utc = datetime.now(timezone.utc)
    upcoming = []
    for g in games:
        try:
            fp_dt, _ = mlb_api.parse_first_pitch(g.get("gameDate", ""))
            if fp_dt and fp_dt.astimezone(timezone.utc) > now_utc:
                upcoming.append((g, fp_dt))
        except Exception:
            continue

    elite = []
    composites = composites_data["composites"]
    raw = composites_data["raw"]
    for g, fp_dt in upcoming:
        away_name = g["teams"]["away"]["team"]["name"]
        home_name = g["teams"]["home"]["team"]["name"]
        away_id = g["teams"]["away"]["team"]["id"]
        home_id = g["teams"]["home"]["team"]["id"]
        away_p = g["teams"]["away"].get("probablePitcher") or {}
        home_p = g["teams"]["home"].get("probablePitcher") or {}
        il = mlb_api.get_team_il(home_id, season) | mlb_api.get_team_il(away_id, season)
        for tid in (home_id, away_id):
            team_name = home_name if tid == home_id else away_name
            opp_pname = (away_p.get("fullName", "TBD") if tid == home_id
                         else home_p.get("fullName", "TBD"))
            for r in mlb_api.get_team_roster(tid, season):
                if r.get("position", {}).get("type") == "Pitcher":
                    continue
                pid = r.get("person", {}).get("id")
                if not pid or pid in il:
                    continue
                score = composites.get(pid)
                if score is None or score < min_composite:
                    continue
                rdata = raw.get(pid, {})
                elite.append({
                    "score": round(score, 1),
                    "name": r.get("person", {}).get("fullName", "?"),
                    "pid": pid,
                    "team": team_name,
                    "opp_pitcher": opp_pname,
                    "matchup": f"{away_name.split()[-1]} @ {home_name.split()[-1]}",
                    "matchup_full_away": away_name,
                    "matchup_full_home": home_name,
                    "first_pitch": fp_dt.isoformat() if fp_dt else None,
                    "xwoba": rdata.get("xwoba"),
                    "xslg": rdata.get("xslg"),
                    "hard_hit": rdata.get("hard_hit"),
                    "barrel": rdata.get("barrel"),
                })
    elite.sort(key=lambda x: -x["score"])
    return elite


# ---------- Pull props for elite batters ----------

def _fetch_events(api_key):
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events?apiKey={api_key}"
    return json.loads(urllib.request.urlopen(url, timeout=15, context=_SSL).read())


def _fetch_event_props(api_key, event_id):
    url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds"
           f"?apiKey={api_key}&regions=us"
           f"&markets=batter_hits,batter_hits_alternate,"
           f"batter_total_bases,batter_total_bases_alternate,"
           f"batter_home_runs,batter_home_runs_alternate,"
           f"batter_rbis,batter_rbis_alternate,"
           f"batter_runs_scored,batter_runs_scored_alternate,"
           f"pitcher_strikeouts,pitcher_strikeouts_alternate"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception:
        return {}


# ---------- Pitcher composite (K%/xwOBA-against) ----------

def load_pitcher_composites(season):
    """Returns {pitcher_id: composite_pct} based on:
       - K/9 percentile (high = good K pitcher)
       - xwOBA-against percentile (low = good pitcher — INVERTED)
    """
    from data import mlb_api as _ma
    px = savant.pitcher_xstats(season)

    # xwOBA-against percentile, INVERTED (lower xwOBA = higher percentile)
    xwoba_against_pct = {}
    if "xwoba_against" in px.columns:
        s = px["xwoba_against"].dropna()
        # Invert: lower is better → higher percentile
        ranks = (1 - s.rank(pct=True)) * 100
        xwoba_against_pct = dict(zip(px.loc[s.index, "player_id"], ranks))

    # Build raw stats dict
    pitcher_raw = {}
    for _, row in px.iterrows():
        pid = row.get("player_id")
        if pid is None: continue
        pitcher_raw[int(pid)] = {
            "name": row.get("player_name", "?"),
            "xwoba_against": row.get("xwoba_against"),
            "xera": row.get("xera"),
            "xba_against": row.get("xba_against"),
        }
    return {
        "xwoba_against_pct": xwoba_against_pct,
        "raw": pitcher_raw,
    }


def find_elite_pitchers_today(season, min_xwoba_against_pct=70):
    """Returns today's starting pitchers ranked by Statcast composite.
       Pulls K/9 from mlb_api per-pitcher to combine with xwOBA-against."""
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    today = datetime.now(tz=ET).strftime("%Y-%m-%d")

    pdata = load_pitcher_composites(season)
    xwoba_pct = pdata["xwoba_against_pct"]
    raw = pdata["raw"]

    games = mlb_api.get_schedule(today)
    now_utc = datetime.now(timezone.utc)
    upcoming = []
    for g in games:
        try:
            fp_dt, _ = mlb_api.parse_first_pitch(g.get("gameDate", ""))
            if fp_dt and fp_dt.astimezone(timezone.utc) > now_utc:
                upcoming.append((g, fp_dt))
        except Exception:
            continue

    pitchers = []
    for g, fp_dt in upcoming:
        away_name = g["teams"]["away"]["team"]["name"]
        home_name = g["teams"]["home"]["team"]["name"]
        away_p = g["teams"]["away"].get("probablePitcher") or {}
        home_p = g["teams"]["home"].get("probablePitcher") or {}
        for side, p_meta in (("away", away_p), ("home", home_p)):
            pid = p_meta.get("id")
            if not pid: continue
            pid = int(pid)
            pname = p_meta.get("fullName", "TBD")
            xwoba_p = xwoba_pct.get(pid)
            # Fetch K/9 for composite (mlb_api returns strings sometimes)
            k_per_9 = None
            try:
                season_stats = mlb_api.get_pitcher_season(pid, season)
                raw_k9 = season_stats.get("k_per9")
                if raw_k9 is not None and raw_k9 != "":
                    k_per_9 = float(raw_k9)
            except (ValueError, TypeError, Exception):
                k_per_9 = None

            # Composite = average of available percentiles
            parts = []
            if xwoba_p is not None: parts.append(xwoba_p)
            if k_per_9 is not None and k_per_9 > 0:
                # K/9 league avg ~9; elite ~12+. Map to rough percentile.
                k9_pct = min(100, max(0, (k_per_9 - 6) / 7 * 100))
                parts.append(k9_pct)
            if not parts: continue
            composite = sum(parts) / len(parts)
            if composite < min_xwoba_against_pct: continue
            r = raw.get(pid, {})
            pitchers.append({
                "score": round(composite, 1),
                "name": pname,
                "pid": pid,
                "team": home_name if side == "home" else away_name,
                "opp_team": away_name if side == "home" else home_name,
                "matchup": f"{away_name.split()[-1]} @ {home_name.split()[-1]}",
                "matchup_full_away": away_name,
                "matchup_full_home": home_name,
                "first_pitch": fp_dt.isoformat() if fp_dt else None,
                "xwoba_against": r.get("xwoba_against"),
                "xera": r.get("xera"),
                "k_per_9": k_per_9,
            })
    pitchers.sort(key=lambda x: -x["score"])
    return pitchers


def find_pitcher_k_plays(elite_pitchers, api_key, max_pitchers=20):
    """For elite pitchers, pull K props and rank by composite × consensus × X-book."""
    events = _fetch_events(api_key)
    event_map = {(e["away_team"] + "|" + e["home_team"]).lower(): e["id"]
                 for e in events}

    plays = []
    cache = {}
    for e in elite_pitchers[:max_pitchers]:
        eid = event_map.get((e["matchup_full_away"] + "|" + e["matchup_full_home"]).lower())
        if not eid: continue
        if eid not in cache:
            cache[eid] = _fetch_event_props(api_key, eid)
        pp = cache[eid]

        by_offer = defaultdict(dict)
        for b in pp.get("bookmakers", []):
            for m in b.get("markets", []):
                mkt = m["key"]
                if mkt not in ("pitcher_strikeouts", "pitcher_strikeouts_alternate"):
                    continue
                for o in m.get("outcomes", []):
                    player = o.get("description") or o.get("name", "")
                    if not _name_match(player, e["name"]):
                        continue
                    side = o.get("name", "")
                    pt = o.get("point")
                    price = o.get("price")
                    if price is None: continue
                    by_offer[(mkt, side, pt)][b["key"]] = int(price)

        for (mkt, side, pt), books in by_offer.items():
            if len(books) < 3: continue
            best_book, best_price = max(books.items(), key=lambda x: x[1])
            worst_price = min(books.values())
            consensus = sum(_amer_to_imp(p) for p in books.values()) / len(books)
            best_imp = _amer_to_imp(best_price)
            worst_imp = _amer_to_imp(worst_price)
            cross_book_pp = (worst_imp - best_imp) * 100
            ev_pp = (consensus - best_imp) * 100
            ev_dollars = _ev_per_100(best_price, consensus)
            short = mkt.replace("pitcher_", "").replace("_alternate", "*").replace("strikeouts", "K's")
            plays.append({
                "score": e["score"],
                "name": e["name"],
                "pid": e["pid"],
                "matchup": e["matchup"],
                "opp_team": e["opp_team"],
                "market": short,
                "market_raw": mkt,
                "side": side,
                "point": pt,
                "best_price": best_price,
                "best_book": best_book,
                "consensus_pct": round(consensus * 100, 1),
                "cross_book_pp": round(cross_book_pp, 1),
                "edge_pp": round(ev_pp, 1),
                "ev_per_100": round(ev_dollars, 2),
                "n_books": len(books),
                "xwoba_against": e.get("xwoba_against"),
                "k_per_9": e.get("k_per_9"),
                "first_pitch": e["first_pitch"],
                # Settler-required
                "stat_key":  "ks",
                "player":    e["name"],
                "batter_id": None,
                "pitcher_id": e["pid"],
                "away_team": e["matchup_full_away"],
                "home_team": e["matchup_full_home"],
            })

    plays.sort(key=lambda x: -(x["score"]/100 * x["consensus_pct"]
                               * (1 + max(x["cross_book_pp"], 0)/10)))
    return plays


def find_prop_plays(elite_batters, api_key, max_games=12):
    """For top elite batters, pull prop lines and rank by:
       composite × consensus × cross-book dispersion."""
    events = _fetch_events(api_key)
    event_map = {(e["away_team"] + "|" + e["home_team"]).lower(): e["id"]
                 for e in events}

    plays = []
    games_seen = set()
    for e in elite_batters:
        eid = event_map.get((e["matchup_full_away"] + "|" + e["matchup_full_home"]).lower())
        if not eid:
            continue
        if e["matchup"] in games_seen and len(games_seen) >= max_games:
            continue
        games_seen.add(e["matchup"])

        # Cache props per game to avoid re-fetching
        if not hasattr(find_prop_plays, "_cache"):
            find_prop_plays._cache = {}
        if eid not in find_prop_plays._cache:
            find_prop_plays._cache[eid] = _fetch_event_props(api_key, eid)
        pp = find_prop_plays._cache[eid]

        by_offer = defaultdict(dict)
        for b in pp.get("bookmakers", []):
            for m in b.get("markets", []):
                mkt = m["key"]
                if mkt not in ("batter_hits", "batter_hits_alternate",
                               "batter_total_bases", "batter_total_bases_alternate",
                               "batter_home_runs", "batter_home_runs_alternate"):
                    continue
                for o in m.get("outcomes", []):
                    player = o.get("description") or o.get("name", "")
                    if not _name_match(player, e["name"]):
                        continue
                    side = o.get("name", "")
                    pt = o.get("point")
                    price = o.get("price")
                    if price is None:
                        continue
                    by_offer[(mkt, side, pt)][b["key"]] = int(price)

        for (mkt, side, pt), books in by_offer.items():
            if len(books) < 3:
                continue
            best_book, best_price = max(books.items(), key=lambda x: x[1])
            worst_price = min(books.values())
            consensus = sum(_amer_to_imp(p) for p in books.values()) / len(books)
            best_imp = _amer_to_imp(best_price)
            worst_imp = _amer_to_imp(worst_price)
            cross_book_pp = (worst_imp - best_imp) * 100
            ev_pp = (consensus - best_imp) * 100
            ev_dollars = _ev_per_100(best_price, consensus)
            short_mkt = (mkt.replace("batter_", "").replace("_alternate", "*")
                            .replace("home_runs", "HR")
                            .replace("total_bases", "TB")
                            .replace("hits", "Hits"))
            # stat_key for settler: map market_raw -> settler stat name
            stat_key = ("hr" if "home_runs" in mkt
                        else "tb" if "total_bases" in mkt
                        else "hits")
            plays.append({
                "score": e["score"],
                "name": e["name"],
                "pid": e["pid"],
                "matchup": e["matchup"],
                "opp_pitcher": e["opp_pitcher"],
                "market": short_mkt,
                "market_raw": mkt,
                "side": side,
                "point": pt,
                "best_price": best_price,
                "best_book": best_book,
                "consensus_pct": round(consensus * 100, 1),
                "cross_book_pp": round(cross_book_pp, 1),
                "edge_pp": round(ev_pp, 1),
                "ev_per_100": round(ev_dollars, 2),
                "n_books": len(books),
                "xwoba": e["xwoba"],
                "hard_hit": e["hard_hit"],
                "barrel": e["barrel"],
                "first_pitch": e["first_pitch"],
                # Settler-required fields
                "stat_key":  stat_key,
                "player":    e["name"],
                "batter_id": e["pid"],
                "away_team": e["matchup_full_away"],
                "home_team": e["matchup_full_home"],
            })

    # Sort by composite × consensus × cross-book gap weighting
    plays.sort(key=lambda x: -(x["score"] / 100 * x["consensus_pct"]
                               * (1 + max(x["cross_book_pp"], 0) / 10)))
    return plays


# ---------- Main entry ----------

def scan(api_key, season=None, min_composite=70):
    """Single entry point — returns dict with hitter + pitcher + HR sections."""
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    if season is None:
        season = int(datetime.now(tz=ET).strftime("%Y"))

    # Reset per-call cache
    if hasattr(find_prop_plays, "_cache"):
        find_prop_plays._cache = {}

    composites_data = load_statcast_composites(season)
    elite_batters = find_elite_batters_today(composites_data, season, min_composite=min_composite)
    plays = find_prop_plays(elite_batters, api_key, max_games=15)

    # Pitcher K props (Statcast fundamentals)
    elite_pitchers = find_elite_pitchers_today(season, min_xwoba_against_pct=60)
    pitcher_plays = find_pitcher_k_plays(elite_pitchers, api_key, max_pitchers=20)

    # Split plays into hitter (Hits/TB/RBIs/Runs) vs HR longshots
    hitter_plays = [p for p in plays
                    if "home_runs" not in p.get("market_raw", "")]
    hr_plays = [p for p in plays
                if "home_runs" in p.get("market_raw", "")]

    return {
        "elite_batters":   elite_batters,
        "elite_pitchers":  elite_pitchers,
        "hitter_plays":    hitter_plays,
        "pitcher_plays":   pitcher_plays,
        "hr_plays":        hr_plays,
        # Back-compat
        "plays":           plays,
        "elite":           elite_batters,
        "n_games":         len(set(e["matchup"] for e in elite_batters)),
        "n_elite_batters": len(elite_batters),
        "n_elite_pitchers":len(elite_pitchers),
        "n_plays":         len(plays),
        "n_pitcher_plays": len(pitcher_plays),
        "n_hr_plays":      len(hr_plays),
        "scanned_at":      datetime.now(tz=ET).isoformat(),
    }
