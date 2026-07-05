"""
💰 Polymarket Sharp Money tracker.

For every open MLB game-level market on Polymarket, shows where the
limit-order book has heaviest bid-side depth. Strong skew on one side
means real money is queueing up to bet that side at better prices —
pinpointing where sharps are positioned.
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.polymarket_sharp import scan  # noqa: E402
from scripts.sportsbook_matcher import match_signals  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

st.set_page_config(page_title="Sharp Money", page_icon="💰", layout="wide")
st.title("💰 Polymarket Sharp Money")
st.caption(
    "For each open MLB game market on Polymarket, we measure depth on each "
    "side of the inside spread (within ±5¢ of mid). When 70%+ of resting "
    "bids sit on one side, that's where the smart money wants to bet at "
    "better prices than the current mid — a sharp positioning signal.  \n"
    "**Free data — no Odds API quota used.**"
)


# ---------- Cached scan ----------

@st.cache_data(ttl=900, show_spinner="Pulling Polymarket order books (~30s)...")
def cached_scan(min_volume, min_liquidity, top_n):
    return scan(min_volume=min_volume, min_liquidity=min_liquidity, top_n=top_n)


# ---------- Controls ----------

ctrl_cols = st.columns([1.5, 1, 1, 1, 1, 1])
with ctrl_cols[0]:
    refresh = st.button("🔄 Refresh now", type="primary", use_container_width=True,
                        help="Clears the 15-min cache and pulls fresh order books")
with ctrl_cols[1]:
    top_n = st.selectbox("Markets to scan", [30, 50, 75, 100], index=1,
                         help="Top N MLB markets by volume to fetch order books for")
with ctrl_cols[2]:
    min_liquidity = st.number_input("Min liquidity $", value=10000, step=1000,
                                     help="Polymarket listed liquidity floor — "
                                          "$10K+ guarantees real money is behind the market")
with ctrl_cols[3]:
    min_skew = st.slider("Min skew %", 50, 95, 60, 5,
                         help="Only show markets with this much imbalance")
with ctrl_cols[4]:
    min_depth = st.number_input("Min depth $", value=500, step=500,
                                help="Bid-side depth within 5¢ of mid (filter out tiny books)")
with ctrl_cols[5]:
    st.caption(
        "💡 70%+ skew = strong  \n"
        "85%+ skew = extreme  \n"
        "$10K+ liq = real money"
    )

if refresh:
    cached_scan.clear()
    st.toast("Cache cleared — pulling fresh data...", icon="🔄")

# Scan with $10K min so we include all the $10K+ markets the user wants
rows, debug = cached_scan(min_volume=500, min_liquidity=min_liquidity, top_n=top_n)

st.caption(
    f"Scanned **{debug['total_events']}** MLB events → "
    f"**{debug['daily_markets']}** daily markets → "
    f"**{debug['candidates']}** with volume → "
    f"**{debug['with_book']}** with live order books."
)


# ---------- Filter applied ----------

filtered = [
    r for r in rows
    if r["skew_strength"] >= min_skew
    and (r["yes_bid_depth"] + r["no_bid_depth"]) >= min_depth
    and (r.get("liquidity") or 0) >= min_liquidity
]

st.warning(
    "⚠️ **Big sharp money ≠ good bet.** Always check the **Verdict** column "
    "in the Cross-venue plays table below. A market can have $2M of sharp money "
    "on one side and STILL be bad value if the sportsbook is already pricing it "
    "correctly. Only ✅ BET verdicts (≥5pp edge) are real arbs.",
    icon="⚠️",
)
st.markdown(f"### 🎯 {len(filtered)} markets passing filter")
if not filtered:
    st.warning(
        f"No markets with ≥{min_skew}% skew and ≥${min_depth} depth right now. "
        "Lower the threshold or wait — Polymarket books update constantly."
    )
    st.stop()


# ---------- Sharp-money split ----------

tab_all, tab_yes, tab_no = st.tabs([
    f"📋 All ({len(filtered)})",
    f"🟢 YES heavy ({sum(1 for r in filtered if r['skew_side']=='YES')})",
    f"🔴 NO heavy ({sum(1 for r in filtered if r['skew_side']=='NO')})",
])


def _opponent_team(team, event_title):
    """Given one team's name and the event title 'A vs B', return the other team."""
    if not team or not event_title:
        return None
    for sep in (" vs. ", " vs ", " @ "):
        if sep in event_title:
            a, b = event_title.split(sep, 1)
            a, b = a.strip(), b.strip()
            t = team.strip().lower()
            if t in a.lower() or a.lower() in t:
                return b
            if t in b.lower() or b.lower() in t:
                return a
    return None


def _other_side_label(r):
    """The label for the OPPOSITE side of the sharp pick (in human terms)."""
    mt = r.get("match_type")
    sharp_side = r.get("skew_side")
    if mt == "h2h":
        # Sharp on AWAY team if YES, HOME team if NO; other is the other team
        return r.get("home_team", "?") if sharp_side == "YES" else r.get("away_team", "?")
    if mt == "totals":
        # Sharp on OVER if YES, UNDER if NO — include matchup for clarity
        pt = r.get("point")
        away = (r.get("away_team","") or "").split()[-1] or ""
        home = (r.get("home_team","") or "").split()[-1] or ""
        match_str = f" ({away} @ {home})" if away and home else ""
        if sharp_side == "YES":
            base = f"UNDER {pt}" if pt is not None else "UNDER"
        else:
            base = f"OVER {pt}" if pt is not None else "OVER"
        return f"{base}{match_str}"
    if mt == "spreads":
        team = r.get("team")
        pt = r.get("point")
        # The opposite of "Team A -1.5 covers" is "Team B +1.5 covers"
        # (i.e., Team B wins OR Team A wins by exactly 1)
        opponent = _opponent_team(team, r.get("event", "")) or "Opponent"
        if sharp_side == "YES":
            # Sharp says Team A covers -> Other side = Team B gets the +N points
            opp_pt = -(pt or 0)
            sign = "+" if opp_pt > 0 else ""
            return f"{opponent} {sign}{opp_pt}"
        # Sharp says Team A doesn't cover -> Other side = Team A covers the original spread
        sign = "+" if (pt or 0) > 0 else ""
        return f"{team} {sign}{pt}"
    return "?"


def _depth_ratio(sharp_d, other_d):
    """Sharp-side depth ratio. Returns a float, capped at 99 for display."""
    if not other_d or other_d <= 0:
        return 99.0 if (sharp_d or 0) > 0 else 0.0
    return min(99.0, sharp_d / other_d)


@st.cache_data(ttl=120, show_spinner=False)
def _load_persistence_index():
    """Load today's saved sharp_money_history and index by (game, sharp_pick)
    so we can annotate live scan rows with n_appearances / first_seen_at.
    Both fields are populated identically on live rows and saved records,
    so no field-name gymnastics needed.
    Cached 2 min to avoid re-reading the file on every rerun."""
    import json as _j
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    path = os.path.join(ROOT, "sharp_money_history", f"{today}.json")
    idx = {}
    if not os.path.exists(path):
        return idx
    try:
        with open(path, encoding="utf-8") as f:
            for p in _j.load(f).get("picks", []) or []:
                key = (p.get("game","").strip(), p.get("sharp_pick","").strip())
                idx[key] = {
                    "n":     p.get("n_appearances") or 1,
                    "first": p.get("first_seen_at"),
                    "last":  p.get("last_seen_at"),
                    "hist":  p.get("skew_history") or [],
                }
    except Exception:
        pass
    return idx


_persist_idx = _load_persistence_index()


def _lookup_persistence(row):
    """For a live scan row, look up n_appearances from today's saved snapshot."""
    key = (str(row.get("event","")).strip(),
           str(row.get("sharp_pick","")).strip())
    return _persist_idx.get(key)


def render_table(rows_subset, sort_by="depth"):
    if not rows_subset:
        st.info("No markets in this bucket.")
        return
    table = []
    for r in rows_subset:
        marker = "💰💰" if r["skew_strength"] >= 85 else ("💰" if r["skew_strength"] >= 70 else "")
        # Depth on the sharp side vs the other side (no more "YES bid $" jargon)
        sharp_depth = (r["yes_bid_depth"] if r["skew_side"] == "YES"
                       else r["no_bid_depth"])
        other_depth = (r["no_bid_depth"] if r["skew_side"] == "YES"
                       else r["yes_bid_depth"])
        ratio = _depth_ratio(sharp_depth, other_depth)
        persist = _lookup_persistence(r)
        n_seen = persist["n"] if persist else None
        table.append({
            "Game":             r["event"][:32],
            "Mkt":              r["category"],
            "Sharp pick":       f"{marker} {r.get('sharp_pick', '')}",
            "$ on sharp pick":  sharp_depth,
            "Other side":       _other_side_label(r),
            "$ on other side":  other_depth,
            "Depth ratio":      ratio,
            "Skew %":           r["skew_strength"],
            "Seen":             n_seen,
            "Mid (YES)":        r["mid"],
            "Spread":           r["spread"],
            "Volume $":         r["volume"],
        })
    df = pd.DataFrame(table)
    if sort_by == "depth":
        df["_sort"] = df["$ on sharp pick"] + df["$ on other side"]
        df = df.sort_values("_sort", ascending=False).drop(columns="_sort")
    elif sort_by == "skew":
        df = df.sort_values("Skew %", ascending=False)
    elif sort_by == "volume":
        df = df.sort_values("Volume $", ascending=False)
    elif sort_by == "ratio":
        df = df.sort_values("Depth ratio", ascending=False)
    elif sort_by == "persistence":
        df = df.sort_values("Seen", ascending=False, na_position="last")

    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={
            "Mid (YES)":         st.column_config.NumberColumn(format="$%.3f"),
            "Spread":            st.column_config.NumberColumn(format="$%.3f"),
            "$ on sharp pick":   st.column_config.NumberColumn(format="$%,d"),
            "$ on other side":   st.column_config.NumberColumn(format="$%,d"),
            "Depth ratio":       st.column_config.NumberColumn(
                format="%.1fx",
                help="Sharp-side $ divided by other-side $. Backtest: 10x+ "
                     "hit 70% / +21% ROI, 5-10x hit 67% / +31% ROI, "
                     "2-5x hit 52% / +35% ROI. <2x almost never appears."),
            "Skew %":            st.column_config.NumberColumn(format="%.0f%%"),
            "Seen":              st.column_config.NumberColumn(
                format="%d×",
                help="How many scans this signal has appeared in today. "
                     "Higher = more persistent conviction. '—' means the "
                     "pick hasn't yet passed the strict save filter "
                     "(edge≥3pp AND liquidity≥$10K AND SB match)."),
            "Volume $":          st.column_config.NumberColumn(format="$%,.0f"),
        },
    )


with tab_all:
    sort_choice = st.radio(
        "Sort by", ["depth", "skew", "volume", "ratio", "persistence"],
        horizontal=True, key="sort_all",
        help="'persistence' surfaces signals seen in 2+ scans first — "
             "confirmed conviction beats one-shot flashes.")
    render_table(filtered, sort_by=sort_choice)

with tab_yes:
    yes_only = [r for r in filtered if r["skew_side"] == "YES"]
    render_table(sorted(yes_only, key=lambda r: -r["yes_bid_depth"]))

with tab_no:
    no_only = [r for r in filtered if r["skew_side"] == "NO"]
    render_table(sorted(no_only, key=lambda r: -r["no_bid_depth"]))


# =============================================================================
# 🎯 CONFLUENCE PLAYS — Sharp Money agrees with True Prob and/or Soft Scanner
# =============================================================================

st.markdown("---")
st.markdown("### 🎯 Confluence Plays — multiple systems agree")
st.caption(
    "When the same team / over-under shows up in **Sharp Money** AND in "
    "today's **True Probability** (75%+ consensus) AND/OR **Soft Scanner** "
    "picks, that's a multi-source signal — much stronger than any one system alone."
)


def _normalize_team(s):
    """Normalize a team name to a stable comparison key.
    Handles 'Detroit Tigers', 'Tigers', 'D. Tigers' all → 'tigers'.
    We use the LAST word so different representations collapse together."""
    if not s: return ""
    s2 = s.lower().replace(",","").replace(".","").replace("'","").strip()
    # Take last word so "Detroit Tigers" / "Tigers" / "Detroit" all → "tigers"
    # (Note: "Red Sox" / "Sox" / "Boston Red Sox" all → "sox", "White Sox" too —
    # acceptable collision since they don't play each other on same slate.)
    parts = s2.split()
    return parts[-1] if parts else ""


def _make_game_key(away, home):
    """Build a sorted game key from two team names. Sorted so order doesn't matter."""
    a = _normalize_team(away)
    h = _normalize_team(home)
    if not a or not h: return ""
    return "|".join(sorted([a, h]))


def _split_event(event_str):
    """Parse a Sharp Money event string like 'Detroit Tigers vs. Cleveland Guardians'.
    Returns (away_part, home_part) or (None, None) if can't parse."""
    if not event_str: return (None, None)
    for sep in (" vs. ", " vs ", " @ ", " at "):
        if sep in event_str:
            a, b = event_str.split(sep, 1)
            return (a.strip(), b.strip())
    return (None, None)


def _load_today_snapshot(dirname):
    """Load today's snapshot from a tracker dir; returns picks list or [].
    Tolerates git merge-conflict markers in JSON (cron sometimes races and
    leaves <<<<<<<, =======, >>>>>>> in files) — strips them and retries."""
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    today = _dt.now(tz=_Z("America/New_York")).strftime("%Y-%m-%d")
    path = os.path.join(ROOT, dirname, f"{today}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        # First try as-is
        try:
            return _json.loads(raw).get("picks", [])
        except Exception:
            pass
        # Strip merge-conflict markers (keep INCOMING side after =======)
        if "<<<<<<<" in raw or "=======" in raw or ">>>>>>>" in raw:
            out, skip = [], False
            for line in raw.split("\n"):
                if line.startswith("<<<<<<<"): skip = True; continue
                if line.startswith("======="): skip = False; continue
                if line.startswith(">>>>>>>"): continue
                if skip: continue
                out.append(line)
            cleaned = "\n".join(out)
            return _json.loads(cleaned).get("picks", [])
        return []
    except Exception:
        return []


def _build_play_index(picks, source_name):
    """Build a quick lookup index for confluence matching.
    Returns a dict keyed by (game_key, market_type, side_id) → pick.

    Game key uses sorted(away, home) so the order of teams doesn't matter.
    Each side_id format documented per market type below."""
    idx = {}
    for p in picks:
        # Build canonical game key from away/home team fields.
        # Falls back to parsing 'game' or 'event' if those aren't present.
        away = p.get("away_team","")
        home = p.get("home_team","")
        if not away or not home:
            a2, h2 = _split_event(p.get("game") or p.get("event") or "")
            if a2: away = a2
            if h2: home = h2
        gkey = _make_game_key(away, home)
        if not gkey: continue

        stat = (p.get("stat_key") or "").lower()
        market = (p.get("market") or "").lower()
        side = (p.get("side") or "").lower()  # 'over'/'under'/'away'/'home'/'yes'
        point = p.get("point")

        # --- MONEYLINE (h2h) ---
        if "h2h" in stat or "moneyline" in market or stat == "ml":
            # Team picked = the side. For Run Lines side='Away'/'Home'; for true ML
            # the selection field has the team name.
            sel = _normalize_team(p.get("selection") or p.get("player") or "")
            idx[(gkey, "ml", sel)] = p
            continue

        # --- TOTALS (over/under game runs) ---
        # Match by side+point. We normalize point to a string with trailing
        # .5 / .0 collapsed so 12.5 == "12.5" == 12.5.
        if "total" in stat or market.startswith("total"):
            try: pt_str = f"{float(point):g}"
            except Exception: pt_str = str(point)
            if side in ("over", "under"):
                idx[(gkey, "tot", f"{side}_{pt_str}")] = p
            continue

        # --- SPREADS / RUN LINE ---
        # True Prob stores spread picks with side='Away'/'Home' + point=±N.
        # Sharp Money stores spreads with team + point.
        if "spread" in stat or "spread" in market or "run line" in market:
            # Side: prefer team name (player field) — collapses representation
            team_norm = _normalize_team(p.get("player") or "")
            try: pt_str = f"{float(point):g}"
            except Exception: pt_str = str(point)
            if team_norm:
                idx[(gkey, "spread", f"{team_norm}_{pt_str}")] = p
            # Also index by Away/Home so we can match without name resolution
            if side in ("away", "home"):
                idx[(gkey, "spread_ah", f"{side}_{pt_str}")] = p
            continue

        # --- PLAYER PROPS (kept for completeness; never matches Sharp Money) ---
        if stat in ("hits","tb","hr","hrr","ks","walks","rbi","runs"):
            player = _normalize_team(p.get("player") or p.get("selection") or "")
            try: pt_str = f"{float(point):g}"
            except Exception: pt_str = str(point)
            idx[(gkey, f"prop_{stat}", f"{player}_{side}_{pt_str}")] = p
    return idx


_tp_picks = _load_today_snapshot("true_prob_history")
_soft_picks = _load_today_snapshot("soft_scanner_history")
tp_idx = _build_play_index(_tp_picks, "true_prob")
soft_idx = _build_play_index(_soft_picks, "soft")


def _confluence_check(sharp_row):
    """Returns list of source names that ALSO have this play."""
    # Build the game key — try the away_team/home_team fields first,
    # then parse the 'event' string.
    away = sharp_row.get("away_team","")
    home = sharp_row.get("home_team","")
    if not away or not home:
        a2, h2 = _split_event(sharp_row.get("event") or "")
        if a2: away = a2
        if h2: home = h2
    gkey = _make_game_key(away, home)
    if not gkey: return []

    mt = sharp_row.get("match_type")
    sharp_side = sharp_row.get("skew_side")
    hits = []

    if mt == "h2h":
        # YES=away team, NO=home team
        target = away if sharp_side == "YES" else home
        target_norm = _normalize_team(target)
        for src_name, idx in (("True Prob", tp_idx), ("Soft Scanner", soft_idx)):
            if (gkey, "ml", target_norm) in idx:
                if src_name not in hits: hits.append(src_name)
            # Also match against spread (Run Line) — same team with margin
            # is a weaker confluence signal but still meaningful
            for key in list(idx.keys()):
                if (key[0] == gkey and key[1] == "spread"
                    and key[2].startswith(f"{target_norm}_")):
                    if src_name not in hits: hits.append(src_name)
                    break

    elif mt == "totals":
        side_str = "over" if sharp_side == "YES" else "under"
        pt = sharp_row.get("point")
        try: pt_str = f"{float(pt):g}"
        except Exception: pt_str = str(pt)
        for src_name, idx in (("True Prob", tp_idx), ("Soft Scanner", soft_idx)):
            if (gkey, "tot", f"{side_str}_{pt_str}") in idx:
                if src_name not in hits: hits.append(src_name)
            # Match nearby points too (within 1.0) — if Sharp says O8.5 and
            # True Prob says O9.5, that's still aligned confluence
            if src_name in hits: continue
            for key in list(idx.keys()):
                if key[0] == gkey and key[1] == "tot":
                    try:
                        s, p2 = key[2].rsplit("_", 1)
                        if s == side_str and abs(float(p2) - float(pt)) <= 1.0:
                            if src_name not in hits: hits.append(src_name)
                            break
                    except Exception:
                        continue

    elif mt == "spreads":
        team_norm = _normalize_team(sharp_row.get("team") or "")
        pt = sharp_row.get("point")
        try: pt_str = f"{float(pt):g}"
        except Exception: pt_str = str(pt)
        for src_name, idx in (("True Prob", tp_idx), ("Soft Scanner", soft_idx)):
            if (gkey, "spread", f"{team_norm}_{pt_str}") in idx:
                if src_name not in hits: hits.append(src_name)
            # Also match the team-side ML pick (covers spread = wins)
            if (gkey, "ml", team_norm) in idx:
                if src_name not in hits: hits.append(src_name)
    return hits


# Annotate filtered picks with confluence hits
for r in filtered:
    r["_confluence"] = _confluence_check(r)

confluent = sorted(
    [r for r in filtered if len(r["_confluence"]) >= 1],
    key=lambda r: (-len(r["_confluence"]), -r["skew_strength"], -r["yes_bid_depth"] - r["no_bid_depth"])
)

if not confluent:
    # Diagnostic: show what we DID try to match against so the user can see
    # whether the issue is "no snapshots yet" vs "snapshots exist but no overlap"
    sharp_games = sorted({_make_game_key(r.get("away_team",""), r.get("home_team",""))
                          for r in filtered if r.get("away_team")})
    tp_games = sorted({k[0] for k in tp_idx.keys()})
    soft_games = sorted({k[0] for k in soft_idx.keys()})
    overlap_tp = [g for g in sharp_games if g in tp_games]
    overlap_soft = [g for g in sharp_games if g in soft_games]

    st.info(
        "No confluence plays right now — no Sharp Money signals overlap with "
        "True Prob or Soft Scanner picks for today."
    )
    with st.expander("🔍 Why? (diagnostic)", expanded=False):
        st.markdown(f"""
- **Sharp Money picks today:** {len(filtered)} markets across {len(sharp_games)} games
- **True Prob picks loaded:** {len(_tp_picks)} picks across {len(tp_games)} games
- **Soft Scanner picks loaded:** {len(_soft_picks)} picks across {len(soft_games)} games
- **Sharp ↔ True Prob game overlap:** {len(overlap_tp)} games
- **Sharp ↔ Soft Scanner game overlap:** {len(overlap_soft)} games
""")
        if not _tp_picks and not _soft_picks:
            st.warning(
                "⚠️ **No tracker snapshots have been saved for today yet.** "
                "The daily cron normally writes True Prob + Soft Scanner snapshots "
                "around noon ET. Until those run, Confluence has nothing to compare "
                "against. Check back later, or trigger a manual run."
            )
        elif not overlap_tp and not overlap_soft:
            st.warning(
                "Snapshots exist but no game-key overlap with today's Sharp Money "
                "scan. This usually means trackers ran for a different set of games "
                "(e.g. tomorrow's slate captured early). Possibly stale snapshot."
            )
        else:
            st.info(
                "Same games appear in both — but different markets/sides. "
                "Sharp Money's pick (e.g. NO on -125 favorite) didn't match "
                "True Prob's same-game pick (e.g. Under 12.5 total). "
                "This is normal — different systems target different angles."
            )
else:
    crows = []
    for r in confluent[:15]:
        sources = r["_confluence"]
        # Tier: 1 source = strong, 2 sources = TRIPLE-CONFLUENT
        if len(sources) >= 2:
            tier = "🟢🟢🟢 TRIPLE"
        elif "True Prob" in sources:
            tier = "🟢🟢 + True Prob"
        else:
            tier = "🟢 + Soft Scanner"
        crows.append({
            "Tier":        tier,
            "Sharp pick":  r.get("sharp_pick", ""),
            "Game":        r["event"][:32],
            "Skew %":      r["skew_strength"],
            "Liquidity $": r.get("liquidity", 0),
            "SB price":    r.get("sb_best_price"),
            "Edge pp":     r.get("edge_pp"),
            "Confirmed by": " + ".join(sources),
        })
    cdf = pd.DataFrame(crows)
    st.dataframe(
        cdf, use_container_width=True, hide_index=True,
        column_config={
            "Skew %":      st.column_config.NumberColumn(format="%.0f%%"),
            "Liquidity $": st.column_config.NumberColumn(format="$%,.0f"),
            "SB price":    st.column_config.NumberColumn(format="%+d"),
            "Edge pp":     st.column_config.NumberColumn(format="%+.1f"),
        },
    )
    triples = [r for r in confluent if len(r["_confluence"]) >= 2]
    if triples:
        st.success(
            f"🔥 **{len(triples)} TRIPLE-CONFLUENT play{'s' if len(triples)!=1 else ''} "
            f"found.** Sharp money + True Prob + Soft Scanner all agree — "
            f"highest-conviction signals on the dashboard."
        )


# ---------- Top 3 strongest signals — detail expanders ----------

st.markdown("---")
st.markdown("### 🔍 Top 3 strongest signals — full detail")

strongest = sorted(filtered, key=lambda r: (
    -r["skew_strength"], -(r["yes_bid_depth"] + r["no_bid_depth"])
))[:3]

for r in strongest:
    fav_side = r["skew_side"]
    fav_pct = r["skew_strength"]
    sharp_depth = r["no_bid_depth"] if fav_side == "NO" else r["yes_bid_depth"]
    other_depth = r["yes_bid_depth"] if fav_side == "NO" else r["no_bid_depth"]
    interpretation = (
        f"**The book is loaded on the {fav_side} side** "
        f"(${sharp_depth:,} vs ${other_depth:,} on the other). "
        f"That means real money is queued to buy {fav_side} at better prices "
        f"than the current mid (${r['mid']:.3f}). "
        f"Sharp interpretation: market expects {fav_side}."
    )

    ratio = _depth_ratio(sharp_depth, other_depth)
    # Ratio tier from 39-pick backtest (see /code)
    if ratio >= 10:      ratio_tier = "🟢 extreme (10x+)"
    elif ratio >= 5:     ratio_tier = "🟢 strong (5-10x)"
    elif ratio >= 2:     ratio_tier = "🟡 moderate (2-5x)"
    else:                ratio_tier = "🔴 weak (<2x)"

    # Persistence: how many scans has this signal appeared in today?
    persist = _lookup_persistence(r)
    n_seen = persist["n"] if persist else 0
    if n_seen >= 3:      persist_tier = f"🟢🟢 confirmed ({n_seen}× seen)"
    elif n_seen == 2:    persist_tier = f"🟢 emerging (2× seen)"
    elif n_seen == 1:    persist_tier = f"🟡 first sighting"
    else:                persist_tier = f"⚪ not yet saved"

    header_suffix = f" · seen {n_seen}×" if n_seen else " · new"
    with st.expander(
        f"**{r['question'][:80]}**  ·  {r['skew_side']} {fav_pct:.0f}% skew  ·  "
        f"{ratio:.1f}x depth{header_suffix}",
        expanded=True,
    ):
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Mid (YES)", f"${r['mid']:.3f}")
        c2.metric("YES bid depth", f"${r['yes_bid_depth']:,}")
        c3.metric("NO bid depth", f"${r['no_bid_depth']:,}")
        c4.metric("Spread", f"${r['spread']:.3f}")
        c5.metric("Depth ratio", f"{ratio:.1f}x",
                  help="Sharp-side $ ÷ other-side $. Backtest: 10x+ hit 70%, "
                       "5-10x hit 67%, 2-5x hit 52%.")
        st.markdown(interpretation)
        st.caption(
            f"Depth-ratio: **{ratio_tier}**  ·  Persistence: **{persist_tier}**  ·  "
            f"Volume traded: ${r['volume']:,.0f}  ·  "
            f"Listed liquidity: ${r['liquidity']:,.0f}  ·  "
            f"Category: {r['category']}"
        )
        # If we have persistence history, show skew trajectory
        if persist and len(persist.get("hist") or []) >= 2:
            hist = persist["hist"]
            first_skew = hist[0].get("skew") or 0
            last_skew = hist[-1].get("skew") or 0
            trend = "strengthening ↑" if last_skew > first_skew + 2 else (
                    "weakening ↓" if last_skew < first_skew - 2 else "stable →")
            st.caption(
                f"**Skew trajectory:** {first_skew:.0f}% → {last_skew:.0f}% "
                f"across {len(hist)} scans ({trend}). A strengthening skew "
                f"means sharps are pressing the position; weakening = doubt "
                f"creeping in."
            )



# =============================================================================
# CROSS-VENUE ARB — Polymarket sharp signal vs sportsbook line
# =============================================================================

st.markdown("---")
st.markdown("### 🎯 Cross-venue plays — sharp signal + sportsbook line")
st.caption(
    "For each strong Polymarket signal, we look up the matching sportsbook "
    "line. **Edge pp** = how many percentage points sharper than the "
    "sportsbook's implied probability the Polymarket sharps believe the play "
    "is. Positive edge + strong skew = actionable mispricing."
)


def resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


ODDS_KEY = resolve_secret("THE_ODDS_API_KEY")

if not ODDS_KEY:
    st.warning("No `THE_ODDS_API_KEY` configured — can't cross-reference sportsbook prices.")
else:
    @st.cache_data(ttl=300, show_spinner="Matching sportsbook lines...")
    def cached_match(rows_tuple, _odds_key):
        # Convert back to list[dict] for the matcher
        return match_signals([dict(r) for r in rows_tuple], _odds_key)

    # Use only strongly-skewed rows (less noise)
    strong = [r for r in filtered if r["skew_strength"] >= 65]
    if not strong:
        st.info("No strong signals (≥65% skew) to cross-reference.")
    else:
        # cache key needs hashability — pass tuples of items
        rows_key = tuple(tuple(sorted(r.items())) for r in strong)
        try:
            matched = cached_match(rows_key, ODDS_KEY)
        except Exception as e:
            st.error(f"Sportsbook match failed: {e}")
            matched = []

        # Display table
        if matched:
            tbl = []
            for r in matched:
                marker = "💰💰" if r['skew_strength']>=85 else "💰"
                sharp_d = (r["yes_bid_depth"] if r["skew_side"]=="YES"
                           else r["no_bid_depth"])
                other_d = (r["no_bid_depth"] if r["skew_side"]=="YES"
                           else r["yes_bid_depth"])
                edge = r.get("edge_pp")
                # Verdict that makes it obvious whether to bet
                if edge is None:
                    verdict = "❓ no SB match"
                elif edge >= 5:
                    verdict = "✅ BET — real edge"
                elif edge >= 3:
                    verdict = "🟡 marginal edge"
                elif edge >= 0:
                    verdict = "⚠️ confirms SB — no arb"
                else:
                    verdict = "🚫 SB favors other side"
                tbl.append({
                    "Verdict":     verdict,
                    "Game":        r.get("event", "")[:32],
                    "Mkt":         r.get("category", ""),
                    "Sharp pick":  f"{marker} {r.get('sharp_pick', '')}",
                    "$ on pick":   sharp_d,
                    "Other side":  _other_side_label(r),
                    "$ on other":  other_d,
                    "Skew %":      r["skew_strength"],
                    "SB price":    r.get("sb_best_price"),
                    "SB book":     r.get("sb_book", ""),
                    "PM %":        round((r["mid"] if r["skew_side"]=="YES"
                                         else (1-r["mid"]))*100, 1),
                    "SB %":        r.get("sb_implied_pct"),
                    "Edge pp":     edge,
                    "Play":        r.get("play", "")[:50],
                })
            df = pd.DataFrame(tbl)
            # Sort by edge (highest first)
            df_sorted = df.copy()
            df_sorted["_edge_for_sort"] = pd.to_numeric(df_sorted["Edge pp"], errors="coerce").fillna(-999)
            df_sorted = df_sorted.sort_values("_edge_for_sort", ascending=False).drop(columns="_edge_for_sort")

            st.dataframe(
                df_sorted, use_container_width=True, hide_index=True,
                column_config={
                    "$ on pick":   st.column_config.NumberColumn(format="$%,d"),
                    "$ on other":  st.column_config.NumberColumn(format="$%,d"),
                    "Skew %":      st.column_config.NumberColumn(format="%.0f%%"),
                    "SB price":    st.column_config.NumberColumn(format="%+d"),
                    "PM %":        st.column_config.NumberColumn(format="%.1f%%",
                                    help="Polymarket-implied probability for the sharp side"),
                    "SB %":        st.column_config.NumberColumn(format="%.1f%%",
                                    help="Sportsbook-implied probability for the sharp side"),
                    "Edge pp":     st.column_config.NumberColumn(format="%+.1f",
                                    help="PM % − SB %. Positive = sportsbook underpricing, "
                                         "actionable at 3pp+, strong at 5pp+, very strong at 10pp+"),
                },
            )

            st.caption(
                "**Verdict column legend:** "
                "✅ BET = ≥5pp edge (real arb)  ·  "
                "🟡 marginal = 3-5pp edge (small bet or skip)  ·  "
                "⚠️ confirms SB = sharp money agrees with sportsbook, no edge to exploit  ·  "
                "🚫 SB favors other side = sportsbook prices the OPPOSITE of sharps — skip the sharp pick  \n"
                "_Always verify the live sportsbook price before betting — odds move._"
            )

            # Top actionable plays — REAL edge (>=3pp), strong skew, real liquidity.
            # Sub-3pp edges get eaten by sportsbook vig (~4-5pp typical).
            MIN_EDGE_FOR_ACTION = 3.0
            actionable = [r for r in matched
                          if r.get("edge_pp") is not None and r["edge_pp"] >= MIN_EDGE_FOR_ACTION
                          and r["skew_strength"] >= 70
                          and (r.get("liquidity") or 0) >= 10000
                          and r.get("sb_best_price") is not None]
            actionable.sort(key=lambda r: -r["edge_pp"])

            if actionable:
                st.markdown(f"#### 🏆 Top {min(5, len(actionable))} actionable plays")
                st.caption(
                    f"≥{MIN_EDGE_FOR_ACTION}pp edge AND ≥70% skew AND ≥$10K liquidity AND sportsbook match found. "
                    "Sub-3pp edges get eaten by sportsbook vig (~4-5pp) — those aren't shown."
                )
                for r in actionable[:5]:
                    edge = r["edge_pp"]
                    color = "🟢🟢" if edge >= 10 else ("🟢" if edge >= 5 else "🟡")
                    game = r.get("event", "?")
                    with st.expander(
                        f"{color} **{game}**  ·  {r['play']}  ·  edge {edge:+.1f}pp",
                        expanded=True,
                    ):
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Polymarket implied (sharp side)",
                                  f"{r['mid']*100 if r['skew_side']=='YES' else (1-r['mid'])*100:.1f}%")
                        c2.metric("Sportsbook implied", f"{r['sb_implied_pct']:.1f}%")
                        c3.metric("Edge", f"{edge:+.1f}pp", help="PM sharp implied − SB implied")
                        st.write(
                            f"**Game:** {game}  \n"
                            f"**The play:** {r['play']}  \n"
                            f"**Sharp pick:** {r.get('sharp_pick', '')}  \n"
                            f"**Why:** Polymarket sharps have loaded {r['skew_side']} side "
                            f"with {r['skew_strength']:.0f}% bid depth on ${r['volume']:,.0f} volume. "
                            f"That implies they think the true probability is "
                            f"~{r['mid']*100 if r['skew_side']=='YES' else (1-r['mid'])*100:.0f}% "
                            f"while {r['sb_book']} is offering {r['sb_implied_pct']:.0f}% implied. "
                            f"Difference: **{edge:+.1f}pp edge**."
                        )
            else:
                st.info(
                    f"**No actionable plays right now.** "
                    f"Need ≥{MIN_EDGE_FOR_ACTION}pp edge + ≥70% skew + ≥$10K liquidity + sportsbook match.  \n"
                    f"This is normal — strong cross-venue mispricing only shows up "
                    f"a few times per slate, usually closer to first pitch when "
                    f"sportsbook lines firm up. Marginal-edge plays (sub-3pp) "
                    f"appear in the table above but aren't worth betting after vig."
                )


st.markdown("---")
st.caption(
    f"Last scan: {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"Cache TTL: 15 min  ·  "
    f"Polymarket: Gamma + CLOB (free)  ·  "
    f"Sportsbook match: Odds API (small quota)"
)
