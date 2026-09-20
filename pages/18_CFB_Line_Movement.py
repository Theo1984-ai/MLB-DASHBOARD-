"""
🏈📈 CFB Team Totals — Line Movement.

Exact mirror of pages/10_Line_Movement.py for CFB.
Reads cfb_team_totals_history/week_YYYY-MM-DD.json (one file per week).
Uses ESPN scoreboard for grading instead of MLB Stats API.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")
HISTORY_DIR = os.path.join(ROOT, "cfb_team_totals_history")


# ---------- ESPN grading helpers ----------

@st.cache_data(ttl=120, show_spinner=False)
def _fetch_cfb_schedule(week_key):
    """ESPN CFB scoreboard for Thu–Sun of the given week (all CFB game days)."""
    import ssl as _ssl
    import urllib.request
    ctx = _ssl._create_unverified_context()
    week_start = datetime.strptime(week_key, "%Y-%m-%d")
    out = []
    for day_offset in (3, 4, 5, 6):  # Thu, Fri, Sat, Sun
        day = week_start + timedelta(days=day_offset)
        date_espn = day.strftime("%Y%m%d")
        url = (f"https://site.api.espn.com/apis/site/v2/sports/football/"
               f"college-football/scoreboard?dates={date_espn}&limit=200")
        try:
            raw = urllib.request.urlopen(url, timeout=15, context=ctx).read()
            d = json.loads(raw)
        except Exception:
            continue
        for ev in d.get("events", []):
            try:
                comps = ev.get("competitions", [{}])[0]
                competitors = comps.get("competitors", [])
                status_id = str(comps.get("status", {}).get("type", {}).get("id", ""))
                home_data = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away_data = next((c for c in competitors if c.get("homeAway") == "away"), {})
                home_team = home_data.get("team", {}).get("displayName", "")
                away_team = away_data.get("team", {}).get("displayName", "")
                home_score = home_data.get("score")
                away_score = away_data.get("score")
                out.append({
                    "away_team":   away_team,
                    "home_team":   home_team,
                    "away_runs":   int(away_score) if away_score is not None else None,
                    "home_runs":   int(home_score) if home_score is not None else None,
                    "status_code": status_id,  # "1"=pre, "2"=live, "3"=final
                })
            except Exception:
                continue
    return out


def _grade_play(sched, away_team, home_team, bet_team, bet_side, bet_line):
    """Grade one team-totals play. Returns (label, team_points).
      label ∈ {"✅ WIN", "❌ LOSS", "➖ PUSH", "⏳ Live", "⏳ Pending", "—"}
    """
    if not sched or not bet_team or bet_side not in ("Over", "Under") or bet_line is None:
        return ("—", None)

    def _last(s): return (s or "").split()[-1].lower()
    game = None
    for g in sched:
        if g["away_team"] == away_team and g["home_team"] == home_team:
            game = g; break
    if not game:
        for g in sched:
            if (_last(g["away_team"]) == _last(away_team)
                    and _last(g["home_team"]) == _last(home_team)):
                game = g; break
    if not game:
        return ("—", None)

    if _last(bet_team) == _last(game["away_team"]):
        team_pts = game["away_runs"]
    elif _last(bet_team) == _last(game["home_team"]):
        team_pts = game["home_runs"]
    else:
        return ("—", None)

    status = game.get("status_code", "")
    if status == "2":
        return ("⏳ Live", team_pts)
    if status != "3":
        return ("⏳ Pending", team_pts)
    if team_pts is None:
        return ("—", None)

    diff = team_pts - bet_line
    is_whole = abs(bet_line - round(bet_line)) < 0.001
    if is_whole and diff == 0:
        return ("➖ PUSH", team_pts)
    if bet_side == "Over":
        return ("✅ WIN", team_pts) if diff > 0 else ("❌ LOSS", team_pts)
    else:
        return ("✅ WIN", team_pts) if diff < 0 else ("❌ LOSS", team_pts)


def _parse_rec_side(rec_str):
    if not rec_str:
        return None
    if "Over" in rec_str: return "Over"
    if "Under" in rec_str: return "Under"
    return None


st.set_page_config(page_title="CFB Line Movement", page_icon="🏈", layout="wide")
st.title("🏈📈 CFB Team Totals — Line Movement")
st.caption(
    "DraftKings CFB team-total snapshots + comparison against **FanDuel, "
    "BetMGM, Bovada, and Caesars**. One file per week — all games in one view.  \n"
    "**Consensus** 🟢🟢 strong (3+ books agree) · 🟢 consensus (2 books) · "
    "⚠️ DK only = single-book move (fadeable) · 🔄 RLM = sharpest signal.  \n"
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
                            use_container_width=True, disabled=not ODDS_KEY,
                            help="Runs the scanner now and appends a new snapshot.")
with rc2:
    if not ODDS_KEY:
        st.error("`THE_ODDS_API_KEY` not configured — refresh disabled.")

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
    st.error(f"Could not read week_{sel_week}.json")
    st.stop()

snapshots = payload.get("snapshots", []) or []
if not snapshots:
    st.info(f"No snapshots in week {sel_week} yet.")
    st.stop()


def _first_populated(snaps):
    for s in snaps:
        if (s.get("n_games") or 0) > 0 and s.get("games"):
            return s
    return snaps[0] if snaps else {}


c1, c2, c3, c4 = st.columns(4)
c1.metric("Snapshots", len(snapshots))

first_populated_snap = _first_populated(snapshots)
try:
    first_t = datetime.fromisoformat(first_populated_snap.get("captured_at", "")).strftime("%a %b %d %I:%M %p")
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
        o_side_data = o_books[b].get(side) or {}
        c_side_data = c_books[b].get(side) or {}
        if majority == "up":
            o_u = o_side_data.get("under_price")
            c_u = c_side_data.get("under_price")
            if o_u is not None and c_u is not None and (c_u - o_u) <= -price_threshold:
                return "Under"
        else:
            o_o = o_side_data.get("over_price")
            c_o = c_side_data.get("over_price")
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
    big_movers = {b: d for b, d in deltas.items() if abs(d) >= 0.5}
    if not big_movers:
        return ""
    if len(big_movers) == 1:
        return "⚠️ DK only" if "draftkings" in big_movers else "⚠️ single book"
    same_dir = all(d > 0 for d in big_movers.values()) or all(d < 0 for d in big_movers.values())
    if not same_dir:
        return "🟡 mixed"
    if len(big_movers) >= 3:
        return "🟢🟢 strong consensus"
    return "🟢 consensus"


def _recommendation(open_game, curr_game, side, consensus_tag, dl):
    dl = dl or 0
    rlm = _rlm_check(open_game, curr_game, side)
    if rlm:
        return f"🔄 {rlm} (RLM)"
    if "consensus" in consensus_tag:
        if dl >= 0.5:  return "🎯 Over (consensus)"
        if dl <= -0.5: return "🎯 Under (consensus)"
    if "DK only" in consensus_tag:
        if dl >= 0.5:  return "↩️ Under (fade DK)"
        if dl <= -0.5: return "↩️ Over (fade DK)"
    return ""


opening = first_populated_snap.get("games", [])
opening_map = {g["game"]: g for g in opening}

current_map = {}
for snap in reversed(snapshots):
    for g in snap.get("games", []):
        if g["game"] not in current_map:
            current_map[g["game"]] = g
current = list(current_map.values())


def _first_pitch(game_name):
    g = current_map.get(game_name) or opening_map.get(game_name) or {}
    fp = g.get("first_pitch") or ""
    return (fp, game_name)


all_games = sorted(set(opening_map) | set(current_map), key=_first_pitch)


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
    o_away = o.get("away", {}) if o else {}
    o_home = o.get("home", {}) if o else {}
    c_away = c.get("away", {}) if c else {}
    c_home = c.get("home", {}) if c else {}
    dl_a = None
    if c_away.get("line") is not None and o_away.get("line") is not None:
        dl_a = round(c_away["line"] - o_away["line"], 1)
    dp_a_over = _delta_price(o_away.get("over_price"), c_away.get("over_price"))
    dp_a_under = _delta_price(o_away.get("under_price"), c_away.get("under_price"))
    consensus_a = _consensus_tag(o, c, "away")
    rec_a = _recommendation(o, c, "away", consensus_a, dl_a)
    rows.append({
        "Kickoff":       _kickoff_label(game),
        "Game":          game,
        "Team":          (c.get("away_team") or o.get("away_team") or "?"),
        "Open":          o_away.get("line"),
        "Current":       c_away.get("line"),
        "Δ Line":        dl_a,
        "Consensus":     consensus_a,
        "🎯 Rec":         rec_a,
        "Over Open":     o_away.get("over_price"),
        "Over Current":  c_away.get("over_price"),
        "Δ Over":        dp_a_over,
        "Under Open":    o_away.get("under_price"),
        "Under Current": c_away.get("under_price"),
        "Δ Under":       dp_a_under,
        "Move":          _emoji(dl_a, max(abs(dp_a_over or 0), abs(dp_a_under or 0))
                                * (1 if (dp_a_over or 0) + (dp_a_under or 0) >= 0 else -1)),
    })
    dl_h = None
    if c_home.get("line") is not None and o_home.get("line") is not None:
        dl_h = round(c_home["line"] - o_home["line"], 1)
    dp_h_over = _delta_price(o_home.get("over_price"), c_home.get("over_price"))
    dp_h_under = _delta_price(o_home.get("under_price"), c_home.get("under_price"))
    consensus_h = _consensus_tag(o, c, "home")
    rec_h = _recommendation(o, c, "home", consensus_h, dl_h)
    rows.append({
        "Kickoff":       _kickoff_label(game),
        "Game":          game,
        "Team":          (c.get("home_team") or o.get("home_team") or "?"),
        "Open":          o_home.get("line"),
        "Current":       c_home.get("line"),
        "Δ Line":        dl_h,
        "Consensus":     consensus_h,
        "🎯 Rec":         rec_h,
        "Over Open":     o_home.get("over_price"),
        "Over Current":  c_home.get("over_price"),
        "Δ Over":        dp_h_over,
        "Under Open":    o_home.get("under_price"),
        "Under Current": c_home.get("under_price"),
        "Δ Under":       dp_h_under,
        "Move":          _emoji(dl_h, max(abs(dp_h_over or 0), abs(dp_h_under or 0))
                                * (1 if (dp_h_over or 0) + (dp_h_under or 0) >= 0 else -1)),
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

        sched = _fetch_cfb_schedule(sel_week)
        results = []
        team_pts_list = []
        for _, r in plays.iterrows():
            game_str = r.get("Game", "")
            if " @ " in game_str:
                away_full, home_full = game_str.split(" @ ", 1)
            else:
                away_full = home_full = ""
            bet_side = _parse_rec_side(r["🎯 Rec"])
            bet_team = r.get("Team", "")
            bet_line = r.get("Current")
            label, team_pts = _grade_play(sched, away_full, home_full,
                                          bet_team, bet_side, bet_line)
            results.append(label)
            team_pts_list.append(team_pts)
        plays["Result"] = results
        plays["Team pts"] = team_pts_list

        st.markdown("### 🎯 This week's actionable plays")
        st.caption(
            "🔄 **RLM** = line and price disagree (sharpest signal, fade) · "
            "🎯 **Consensus** = 2+ books agree (follow the move) · "
            "↩️ **Fade DK** = DK-only mover · "
            "**Result** grades each play against ESPN once games finish."
        )
        st.dataframe(
            plays[["Kickoff", "Team", "Open", "Current", "Δ Line", "Consensus", "🎯 Rec",
                   "Over Current", "Under Current", "Team pts", "Result"]],
            use_container_width=True, hide_index=True,
            column_config={
                "Open":          st.column_config.NumberColumn(format="%.1f"),
                "Current":       st.column_config.NumberColumn(format="%.1f"),
                "Δ Line":        st.column_config.NumberColumn(format="%+.1f"),
                "Over Current":  st.column_config.NumberColumn(format="%+d"),
                "Under Current": st.column_config.NumberColumn(format="%+d"),
                "Team pts":      st.column_config.NumberColumn(format="%d"),
            },
        )

        wins = sum(1 for x in results if x == "✅ WIN")
        losses = sum(1 for x in results if x == "❌ LOSS")
        pushes = sum(1 for x in results if x == "➖ PUSH")
        pending = sum(1 for x in results if x in ("⏳ Live", "⏳ Pending"))
        if wins + losses + pushes > 0:
            settled = wins + losses + pushes
            hit_rate = (wins / (wins + losses) * 100) if (wins + losses) else 0
            st.caption(
                f"📊 **Week of {sel_week} record:** {wins}-{losses}"
                + (f"-{pushes}" if pushes else "")
                + f" ({hit_rate:.0f}% hit) · {pending} still pending"
            )
    else:
        st.info("🎯 No actionable plays yet — waiting for meaningful line movement.")
    st.divider()

fc1, _ = st.columns([1, 4])
with fc1:
    only_moves = st.toggle("🔥 Only show movement",
                           help="Hide teams where line & price haven't moved")

if only_moves and not df.empty:
    df = df[(df["Δ Line"].fillna(0) != 0) |
            (df["Δ Over"].fillna(0) != 0) |
            (df["Δ Under"].fillna(0) != 0)]

st.dataframe(df, use_container_width=True, hide_index=True, column_config=COL_CFG)

st.divider()
st.subheader("📊 Line trajectory")

game_options = sorted(set(current_map) | set(opening_map), key=_first_pitch)
if game_options:
    sel_game = st.selectbox("Pick a game to see movement over time", game_options,
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
            st.caption("No time-series data for this game yet — take more snapshots over time.")

st.divider()
st.caption(
    f"Books: DraftKings · FanDuel · BetMGM · Bovada · Caesars  ·  "
    f"Market: CFB team totals  ·  "
    f"{payload.get('n_snapshots', len(snapshots))} snapshots this week  ·  "
    f"On-demand only — no auto-cron."
)
