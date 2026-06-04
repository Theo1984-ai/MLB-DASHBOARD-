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
           f"batter_home_runs,batter_home_runs_alternate"
           f"&bookmakers={SHARP_BOOKS}&oddsFormat=american")
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20, context=_SSL).read())
    except Exception:
        return {}


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
            })

    # Sort by composite × consensus × cross-book gap weighting
    plays.sort(key=lambda x: -(x["score"] / 100 * x["consensus_pct"]
                               * (1 + max(x["cross_book_pp"], 0) / 10)))
    return plays


# ---------- Main entry ----------

def scan(api_key, season=None, min_composite=70):
    """Single entry point — returns dict with all sections."""
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    if season is None:
        season = int(datetime.now(tz=ET).strftime("%Y"))

    # Reset per-call cache
    if hasattr(find_prop_plays, "_cache"):
        find_prop_plays._cache = {}

    composites_data = load_statcast_composites(season)
    elite = find_elite_batters_today(composites_data, season, min_composite=min_composite)
    plays = find_prop_plays(elite, api_key, max_games=15)

    return {
        "elite": elite,
        "plays": plays,
        "n_games": len(set(e["matchup"] for e in elite)),
        "n_elite_batters": len(elite),
        "n_plays": len(plays),
        "scanned_at": datetime.now(tz=ET).isoformat(),
    }
