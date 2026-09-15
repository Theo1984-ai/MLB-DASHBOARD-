"""
📈 CFB Team Totals — Line Movement (on-demand).

All CFB games this week in one view. Hit 🔄 to take a snapshot.
One file per week — no date-splitting.
"""
import json
import os
import ssl as _ssl_compat
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")
HISTORY_DIR = os.path.join(ROOT, "cfb_team_totals_history")

st.set_page_config(page_title="CFB Line Movement", page_icon="📈", layout="wide")
st.title("📈 CFB Team Totals — Line Movement")
st.caption(
    "DraftKings CFB team-total snapshots vs FanDuel, BetMGM, Bovada, Caesars — "
    "all games this week in one view.  \n"
    "**Consensus** 🟢🟢 = 3+ books agree · 🟢 = 2 books · "
    "⚠️ DK only = single-book move · 🔄 RLM = sharpest signal.  \n"
    "No auto-cron — hit **🔄 Take snapshot now** on demand."
)


def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


ODDS_KEY = _resolve_secret("THE_ODDS_API_KEY")

rc1, rc2 = st.columns([1, 5])
with rc1:
    refresh_btn = st.button("🔄 Take snapshot now", type="primary",
                            use_container_width=True, disabled=not ODDS_KEY)
with rc2:
    if not ODDS_KEY:
        st.error("`THE_ODDS_API_KEY` not configured.")

if refresh_btn and ODDS_KEY:
    with st.spinner("Scanning CFB team totals for this week..."):
        try:
            os.environ["THE_ODDS_API_KEY"] = ODDS_KEY
            from scripts.cfb_team_totals_snapshot import main as run_snapshot
            result = run_snapshot(force=True)
            if result.get("status") == "ok":
                st.success(
                    f"✅ Snapshot #{result['snapshot_n']} saved — "
                    f"**{result['n_games']}** games this week captured."
                )
                st.cache_data.clear()
                st.rerun()
            else:
                st.error(f"Snapshot returned: {result}")
        except Exception as e:
            import traceback
            st.error(f"Snapshot failed: {e}")
            with st.expander("Traceback"):
                st.code(traceback.format_exc()[:2000])


def _week_monday(dt_et):
    days = dt_et.weekday()
    return (dt_et - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def _load_week(week_key):
    path = os.path.join(HISTORY_DIR, f"week_{week_key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


available_weeks = []
if os.path.isdir(HISTORY_DIR):
    for fn in sorted(os.listdir(HISTORY_DIR), reverse=True):
        if fn.startswith("week_") and fn.endswith(".json"):
            available_weeks.append(fn[5:-5])

if not available_weeks:
    st.info(
        "No CFB team-total snapshots yet. Hit **🔄 Take snapshot now** to capture "
        "current lines for all games this week."
    )
    st.stop()

now_et = datetime.now(tz=EASTERN)
current_week_key = _week_monday(now_et).strftime("%Y-%m-%d")
default_idx = available_weeks.index(current_week_key) if current_week_key in available_weeks else 0

sel_week = st.selectbox(
    "📅 CFB Week (Monday start)",
    options=available_weeks,
    index=default_idx,
    format_func=lambda w: f"Week of {w}",
)

payload = _load_week(sel_week)
if not payload:
    st.error(f"Could not load week_{sel_week}.json")
    st.stop()

snapshots = payload.get("snapshots", []) or []
if not snapshots:
    st.info("No snapshots in this week's file yet.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Snapshots", len(snapshots))


def _first_populated(snaps):
    for s in snaps:
        if (s.get("n_games") or 0) > 0 and s.get("games"):
            return s
    return snaps[0] if snaps else {}


first_snap = _first_populated(snapshots)
try:
    first_t = datetime.fromisoformat(first_snap["captured_at"]).strftime("%a %b %d %I:%M %p")
except Exception:
    first_t = "?"
try:
    last_t = datetime.fromisoformat(snapshots[-1]["captured_at"]).strftime("%a %b %d %I:%M %p")
except Exception:
    last_t = "?"
c2.metric("First snapshot", first_t)
c3.metric("Latest snapshot", last_t)
c4.metric("Games this week", snapshots[-1].get("n_games", 0))

st.divider()

SANE_LINE_MIN = 7.0
SANE_LINE_MAX = 45.0


def _delta_price(open_p, cur_p):
    if open_p is None or cur_p is None:
        return None
    return cur_p - open_p


def _emoji(dl, dp):
    if dl is None: dl = 0
    if dp is None: dp = 0
    if abs(dl) >= 0.5 or abs(dp) >= 20:
        return "🔥"
    if abs(dl) >= 0.25 or abs(dp) >= 10:
        return "📈" if (dl > 0 or dp > 0) else "📉"
    return ""


def _rlm_check(open_game, curr_game, side, price_threshold=15):
    o_books = (open_game or {}).get("books") or {}
    c_books = (curr_game or {}).get("books") or {}
    common = set(o_books.keys()) & set(c_books.keys())
    if len(common) < 2:
        return None
    line_deltas = {}
    for b in common:
        o_line = (o_books[b].get(side) or {}).get("line")
        c_line = (c_books[b].get(side) or {}).get("line")
        if o_line is None or c_line is None:
            continue
        if not (SANE_LINE_MIN <= o_line <= SANE_LINE_MAX):
            continue
        if not (SANE_LINE_MIN <= c_line <= SANE_LINE_MAX):
            continue
        line_deltas[b] = c_line - o_line
    if len(line_deltas) < 2:
        return None
    up_movers   = sum(1 for d in line_deltas.values() if d >= 0.5)
    down_movers = sum(1 for d in line_deltas.values() if d <= -0.5)
    if up_movers + down_movers < 2:
        return None
    majority = "up" if up_movers > down_movers else ("down" if down_movers > up_movers else None)
    if majority is None:
        return None
    anchors = [b for b, d in line_deltas.items() if abs(d) < 0.5]
    if not anchors:
        return None
    for b in anchors:
        o_s = o_books[b].get(side) or {}
        c_s = c_books[b].get(side) or {}
        if majority == "up":
            o_u, c_u = o_s.get("under_price"), c_s.get("under_price")
            if o_u is not None and c_u is not None and (c_u - o_u) <= -price_threshold:
                return "Under"
        else:
            o_o, c_o = o_s.get("over_price"), c_s.get("over_price")
            if o_o is not None and c_o is not None and (c_o - o_o) <= -price_threshold:
                return "Over"
    return None


def _consensus_tag(open_game, curr_game, side):
    o_books = (open_game or {}).get("books") or {}
    c_books = (curr_game or {}).get("books") or {}
    common = set(o_books.keys()) & set(c_books.keys())
    if len(common) < 2:
        return ""
    deltas = {}
    for b in common:
        o = (o_books[b].get(side) or {}).get("line")
        c = (c_books[b].get(side) or {}).get("line")
        if o is not None and c is not None:
            deltas[b] = c - o
    if not deltas:
        return ""
    big = {b: d for b, d in deltas.items() if abs(d) >= 0.5}
    if not big:
        return ""
    if len(big) == 1:
        return "⚠️ DK only" if "draftkings" in big else "⚠️ single book"
    same_dir = all(d > 0 for d in big.values()) or all(d < 0 for d in big.values())
    if not same_dir:
        return "🟡 mixed"
    return "🟢🟢 strong consensus" if len(big) >= 3 else "🟢 consensus"


def _recommendation(open_game, curr_game, side, ctag, dl):
    dl = dl or 0
    rlm = _rlm_check(open_game, curr_game, side)
    if rlm:
        return f"🔄 {rlm} (RLM)"
    if "consensus" in ctag:
        if dl >= 0.5:  return "🎯 Over (consensus)"
        if dl <= -0.5: return "🎯 Under (consensus)"
    if "DK only" in ctag:
        if dl >= 0.5:  return "↩️ Under (fade DK)"
        if dl <= -0.5: return "↩️ Over (fade DK)"
    return ""


opening = first_snap.get("games", [])
opening_map = {g["game"]: g for g in opening}

current_map = {}
for snap in reversed(snapshots):
    for g in snap.get("games", []):
        if g["game"] not in current_map:
            current_map[g["game"]] = g


def _fp_sort(game_name):
    g = current_map.get(game_name) or opening_map.get(game_name) or {}
    return g.get("first_pitch") or ""


all_games = sorted(set(opening_map) | set(current_map), key=_fp_sort)


def _kickoff_label(game_name):
    g = current_map.get(game_name) or opening_map.get(game_name) or {}
    fp = g.get("first_pitch") or ""
    if not fp:
        return ""
    try:
        ct = datetime.fromisoformat(fp.replace("Z", "+00:00")).astimezone(EASTERN)
        fmt = "%a %b %d %#I:%M %p ET" if sys.platform == "win32" else "%a %b %d %-I:%M %p ET"
        return ct.strftime(fmt)
    except Exception:
        return fp


rows = []
for game in all_games:
    o = opening_map.get(game, {})
    c = current_map.get(game, {})
    for side in ("away", "home"):
        o_side = (o.get(side) or {}) if o else {}
        c_side = (c.get(side) or {}) if c else {}
        dl = None
        if c_side.get("line") is not None and o_side.get("line") is not None:
            dl = round(c_side["line"] - o_side["line"], 1)
        dp_over  = _delta_price(o_side.get("over_price"),  c_side.get("over_price"))
        dp_under = _delta_price(o_side.get("under_price"), c_side.get("under_price"))
        ctag = _consensus_tag(o, c, side)
        rec  = _recommendation(o, c, side, ctag, dl)
        team = (c.get(f"{side}_team") or o.get(f"{side}_team") or "?")
        rows.append({
            "Kickoff":       _kickoff_label(game),
            "Game":          game,
            "Team":          team,
            "Open":          o_side.get("line"),
            "Current":       c_side.get("line"),
            "Δ Line":        dl,
            "Consensus":     ctag,
            "🎯 Rec":         rec,
            "Over Open":     o_side.get("over_price"),
            "Over Current":  c_side.get("over_price"),
            "Δ Over":        dp_over,
            "Under Open":    o_side.get("under_price"),
            "Under Current": c_side.get("under_price"),
            "Δ Under":       dp_under,
            "Move":          _emoji(dl, max(abs(dp_over or 0), abs(dp_under or 0))
                                    * (1 if (dp_over or 0) + (dp_under or 0) >= 0 else -1)),
        })

df = pd.DataFrame(rows)

COL_CFG = {
    "Open":          st.column_config.NumberColumn(format="%.1f"),
    "Current":       st.column_config.NumberColumn(format="%.1f"),
    "Δ Line":        st.column_config.NumberColumn(format="%+.1f"),
    "Over Open":     st.column_config.NumberColumn(format="%+d"),
    "Over Current":  st.column_config.NumberColumn(format="%+d"),
    "Δ Over":        st.column_config.NumberColumn(format="%+d"),
    "Under Open":    st.column_config.NumberColumn(format="%+d"),
    "Under Current": st.column_config.NumberColumn(format="%+d"),
    "Δ Under":       st.column_config.NumberColumn(format="%+d"),
}

# Actionable plays
if not df.empty:
    plays = df[df["🎯 Rec"] != ""].copy()
    if not plays.empty:
        def _rank(r):
            if r.startswith("🔄"): return 0
            if r.startswith("🎯"): return 1
            if r.startswith("↩️"): return 2
            return 9
        plays["_rank"] = plays["🎯 Rec"].map(_rank)
        plays = plays.sort_values(["_rank", "Δ Line"], ascending=[True, False])
        st.markdown("### 🎯 This week's actionable plays")
        st.dataframe(
            plays[["Kickoff", "Team", "Open", "Current", "Δ Line", "Consensus", "🎯 Rec",
                   "Over Current", "Under Current"]],
            use_container_width=True, hide_index=True, column_config=COL_CFG,
        )
    else:
        st.info("🎯 No actionable plays yet — waiting for meaningful line movement.")
    st.divider()

fc1, _ = st.columns([1, 4])
with fc1:
    only_moves = st.toggle("🔥 Only show movement")

if only_moves and not df.empty:
    df = df[(df["Δ Line"].fillna(0) != 0) |
            (df["Δ Over"].fillna(0) != 0) |
            (df["Δ Under"].fillna(0) != 0)]

st.dataframe(df, use_container_width=True, hide_index=True, column_config=COL_CFG)

st.divider()
st.subheader("📊 Line trajectory")
game_options = sorted(set(current_map) | set(opening_map), key=_fp_sort)
if game_options:
    sel_game = st.selectbox("Pick a game", game_options,
                            format_func=lambda g: f"{g}  ({_kickoff_label(g)})")
    if sel_game:
        rows_ts = []
        for snap in snapshots:
            t = snap.get("captured_at")
            for g in snap.get("games", []):
                if g["game"] != sel_game: continue
                rows_ts.append({
                    "Time": t,
                    f"{g['away_team']} line": (g.get("away") or {}).get("line"),
                    f"{g['home_team']} line": (g.get("home") or {}).get("line"),
                })
        if rows_ts:
            ts_df = pd.DataFrame(rows_ts)
            ts_df["Time"] = pd.to_datetime(ts_df["Time"], errors="coerce")
            ts_df = ts_df.set_index("Time")
            st.line_chart(ts_df)
        else:
            st.caption("No time-series data yet — take more snapshots over time.")

st.divider()
st.caption(
    f"Books: DraftKings · FanDuel · BetMGM · Bovada · Caesars  ·  "
    f"Market: CFB team totals  ·  "
    f"{payload.get('n_snapshots', len(snapshots))} snapshots this week  ·  "
    f"On-demand only — no auto-cron."
)
