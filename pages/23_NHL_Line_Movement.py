"""
🏒 NHL Line Movement — team totals tracked hourly.

Mirrors 10_Line_Movement.py (MLB) exactly, adapted for NHL:
  - sport: icehockey_nhl
  - history: nhl_team_totals_history/
  - sane line range: 1.0–4.5 goals (vs MLB 2.0–6.5 runs)
  - movement threshold: 0.25 goals meaningful (vs 0.5 runs)
"""
import json
import os
import ssl as _ssl
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

_SSL = _ssl._create_unverified_context()

# NHL team abbreviation → full name (matches Odds API naming)
_NHL_ABBREV = {
    "ANA": "Anaheim Ducks",    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",   "CGY": "Calgary Flames",
    "CAR": "Carolina Hurricanes", "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",  "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars",     "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",  "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings","MIN": "Minnesota Wild",
    "MTL": "Montréal Canadiens", "NSH": "Nashville Predators",
    "NJD": "New Jersey Devils","NYI": "New York Islanders",
    "NYR": "New York Rangers", "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins",
    "SEA": "Seattle Kraken",   "SJS": "San Jose Sharks",
    "STL": "St. Louis Blues",  "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs", "UTA": "Utah Mammoth",
    "VAN": "Vancouver Canucks","VGK": "Vegas Golden Knights",
    "WSH": "Washington Capitals", "WPG": "Winnipeg Jets",
}


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_live_scores(date_str):
    """Pull live NHL scores from the official NHL API (free, no key needed)."""
    try:
        url = f"https://api-web.nhle.com/v1/score/{date_str}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                    "Accept": "application/json"})
        data = json.loads(urllib.request.urlopen(req, timeout=10, context=_SSL).read())
    except Exception:
        return {}

    scores = {}
    for g in data.get("games", []):
        away_abbr = g.get("awayTeam", {}).get("abbrev", "")
        home_abbr = g.get("homeTeam", {}).get("abbrev", "")
        away_name = _NHL_ABBREV.get(away_abbr, away_abbr)
        home_name = _NHL_ABBREV.get(home_abbr, home_abbr)
        away_score = g.get("awayTeam", {}).get("score", "")
        home_score = g.get("homeTeam", {}).get("score", "")
        state = g.get("gameState", "")
        period = g.get("period", "")
        clock = (g.get("clock") or {}).get("timeRemaining", "")
        in_int = (g.get("clock") or {}).get("inIntermission", False)

        if state in ("FUT", "PRE"):
            away_label = home_label = "–"
        elif state in ("FINAL", "OFF", "OVER"):
            away_label = f"{away_score} F"
            home_label = f"{home_score} F"
        elif in_int:
            away_label = f"{away_score} INT"
            home_label = f"{home_score} INT"
        else:
            away_label = f"{away_score} (P{period})"
            home_label = f"{home_score} (P{period})"

        scores[away_name] = away_label
        scores[home_name] = home_label
    return scores

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")
HISTORY_DIR = os.path.join(ROOT, "nhl_team_totals_history")

SANE_LINE_MIN = 1.0   # NHL team totals rarely below 1
SANE_LINE_MAX = 4.5   # rarely above 4.5


def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


GH_TOKEN = _resolve_secret("GITHUB_TOKEN")
ODDS_KEY  = _resolve_secret("THE_ODDS_API_KEY")
OWNER = "Theo1984-ai"
REPO  = "MLB-DASHBOARD-"

st.set_page_config(page_title="NHL Line Movement", page_icon="🏒", layout="wide")
st.title("🏒 NHL Team Totals — Line Movement")
st.caption(
    "Fanatics NHL team-total snapshots (primary — most coverage) + FanDuel (sharper but fewer games).  \n"
    "**Consensus** = 🟢🟢 both books agree / 🟢 one book moved.  "
    "**0.25+ goal moves are highlighted. Hit 🔄 to take a fresh snapshot.**"
)


# ---------- Refresh ----------

rc1, rc2 = st.columns([1, 5])
with rc1:
    refresh_btn = st.button("🔄 Take snapshot now", type="primary",
                            use_container_width=True,
                            disabled=not (GH_TOKEN and ODDS_KEY))
with rc2:
    if not ODDS_KEY:
        st.error("`THE_ODDS_API_KEY` not configured.")
    elif not GH_TOKEN:
        st.error("`GITHUB_TOKEN` not configured.")

if refresh_btn and ODDS_KEY and GH_TOKEN:
    with st.spinner("Running NHL team-totals scan..."):
        try:
            os.environ["THE_ODDS_API_KEY"] = ODDS_KEY
            from scripts.nhl_team_totals_snapshot import main as run_snapshot
            result = run_snapshot(force=True)
            if result.get("status") != "ok":
                st.error(f"Snapshot returned: {result}")
            else:
                from data import github_storage as gh
                path = result["path"]
                rel_path = os.path.relpath(path, ROOT).replace(os.sep, "/")
                target_date = result["target_date"]
                with open(path, encoding="utf-8") as f:
                    payload_gh = json.load(f)
                try:
                    gh.save_json(GH_TOKEN, OWNER, REPO, rel_path, payload_gh,
                                 commit_msg=f"NHL team totals snapshot for {target_date}")
                    st.success(f"✅ Snapshot #{result['snapshot_n']} saved · "
                               f"{result['n_games']} games · pushed to GitHub.")
                except Exception as e:
                    st.warning(f"Saved locally but GitHub push failed: {e}")
                st.cache_data.clear()
                st.rerun()
        except Exception as e:
            import traceback
            st.error(f"Snapshot failed: {e}")
            with st.expander("Traceback"):
                st.code(traceback.format_exc()[:2000])


# ---------- Load ----------

def _load_day(date_str):
    path = os.path.join(HISTORY_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
available_dates = []
if os.path.isdir(HISTORY_DIR):
    for fn in sorted(os.listdir(HISTORY_DIR), reverse=True):
        if fn.endswith(".json"):
            available_dates.append(fn[:-5])

if not available_dates:
    st.info("No NHL team-totals snapshots yet. Hit **🔄 Take snapshot now** to capture the current slate.")
    st.stop()

sel_date = st.selectbox("📅 Date", options=available_dates, index=0)
payload = _load_day(sel_date)
if not payload:
    st.error(f"Could not read {sel_date}.json")
    st.stop()

snapshots = payload.get("snapshots", []) or []
if not snapshots:
    st.info(f"No snapshots saved for {sel_date} yet.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Snapshots today", len(snapshots))


def _first_populated(snaps):
    for s in snaps:
        if (s.get("n_games") or 0) > 0 and s.get("games"):
            return s
    return snaps[0] if snaps else {}


first_populated_snap = _first_populated(snapshots)
try:
    first_t = datetime.fromisoformat(first_populated_snap.get("captured_at", "")).strftime("%I:%M %p")
except Exception:
    first_t = "?"
try:
    last_t = datetime.fromisoformat(snapshots[-1]["captured_at"]).strftime("%I:%M %p")
except Exception:
    last_t = "?"
c2.metric("First snapshot", first_t)
c3.metric("Latest snapshot", last_t)
c4.metric("Games tracked", snapshots[-1].get("n_games", 0))
st.divider()


# ---------- Delta helpers ----------

opening     = first_populated_snap.get("games", [])
opening_map = {g["game"]: g for g in opening}

current_map = {}
for snap in reversed(snapshots):
    for g in snap.get("games", []):
        if g["game"] not in current_map:
            current_map[g["game"]] = g
current = list(current_map.values())


def _first_pitch(game_name):
    g = current_map.get(game_name) or opening_map.get(game_name) or {}
    return (g.get("first_pitch") or "", game_name)


all_games = sorted(set(opening_map) | set(current_map), key=_first_pitch)


def _delta_price(open_p, cur_p):
    if open_p is None or cur_p is None:
        return None
    return cur_p - open_p


def _emoji(dl, dp):
    if dl is None: dl = 0
    if dp is None: dp = 0
    # NHL: 0.25 goals = meaningful (vs 0.5 runs for MLB)
    if abs(dl) >= 0.25 or abs(dp) >= 15:
        return "🔥"
    if abs(dl) >= 0.1 or abs(dp) >= 8:
        return "📈" if (dl > 0 or dp > 0) else "📉"
    return ""


def _rlm_check(open_game, curr_game, side, price_threshold=15):
    """Anchor-book RLM detection — same logic as MLB version."""
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
    up_movers   = sum(1 for d in line_deltas.values() if d >= 0.25)
    down_movers = sum(1 for d in line_deltas.values() if d <= -0.25)
    if up_movers + down_movers < 2:
        return None
    majority = "up" if up_movers > down_movers else ("down" if down_movers > up_movers else None)
    if majority is None:
        return None
    anchors = [b for b, d in line_deltas.items() if abs(d) < 0.25]
    if not anchors:
        return None
    for b in anchors:
        o_side_data = o_books[b].get(side) or {}
        c_side_data = c_books[b].get(side) or {}
        if majority == "up":
            o_u = o_side_data.get("under_price")
            c_u = c_side_data.get("under_price")
            if o_u is not None and c_u is not None and c_u - o_u <= -price_threshold:
                return "Under"
        else:
            o_o = o_side_data.get("over_price")
            c_o = c_side_data.get("over_price")
            if o_o is not None and c_o is not None and c_o - o_o <= -price_threshold:
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
    big_movers = {b: d for b, d in deltas.items() if abs(d) >= 0.25}
    if not big_movers:
        return ""
    if len(big_movers) == 1:
        bk = next(iter(big_movers))
        label = {"fanduel": "FD only", "fanatics": "Fanatics only"}.get(bk, "single book")
        return f"⚠️ {label}"
    same_dir = all(d > 0 for d in big_movers.values()) or all(d < 0 for d in big_movers.values())
    if not same_dir:
        return "🟡 mixed"
    if len(big_movers) >= 3:
        return "🟢🟢 strong consensus"
    return "🟢 consensus"


def _book_gap(curr_game, side_key):
    """Return a label when Fanatics and FanDuel disagree on the current line (gap >= 0.5)."""
    bks = (curr_game or {}).get("books") or {}
    fan_line = (bks.get("fanatics") or {}).get(side_key, {}).get("line")
    fd_line  = (bks.get("fanduel")  or {}).get(side_key, {}).get("line")
    if fan_line is None or fd_line is None:
        return ""
    gap = abs(fan_line - fd_line)
    if gap >= 0.5:
        return f"Fan {fan_line} / FD {fd_line}"
    return ""


def _recommendation(open_game, curr_game, side, consensus_tag, dl):
    dl = dl or 0
    rlm = _rlm_check(open_game, curr_game, side)
    if rlm:
        return f"🔄 {rlm} (RLM)"
    if "consensus" in consensus_tag:
        if dl >= 0.25:
            return "🎯 Over (consensus)"
        if dl <= -0.25:
            return "🎯 Under (consensus)"
    if "only" in consensus_tag and "consensus" not in consensus_tag:
        if dl >= 0.25:
            return "↩️ Under (single-book move)"
        if dl <= -0.25:
            return "↩️ Over (single-book move)"
    return ""


# ---------- Build table ----------

rows = []
for game in all_games:
    o = opening_map.get(game, {})
    c = current_map.get(game, {})
    o_away = o.get("away", {}) if o else {}
    o_home = o.get("home", {}) if o else {}
    c_away = c.get("away", {}) if c else {}
    c_home = c.get("home", {}) if c else {}

    for side_key, side_label, o_side, c_side, team_key in (
        ("away", "Away", o_away, c_away, "away_team"),
        ("home", "Home", o_home, c_home, "home_team"),
    ):
        dl = None
        if c_side.get("line") is not None and o_side.get("line") is not None:
            dl = c_side["line"] - o_side["line"]
        dp_over  = _delta_price(o_side.get("over_price"),  c_side.get("over_price"))
        dp_under = _delta_price(o_side.get("under_price"), c_side.get("under_price"))
        consensus = _consensus_tag(o, c, side_key)
        rec       = _recommendation(o, c, side_key, consensus, dl)
        book_gap  = _book_gap(c if c else o, side_key)
        rows.append({
            "Game":          game,
            "Team":          (c.get(team_key) or o.get(team_key) or "?"),
            "Open":          o_side.get("line"),
            "Current":       c_side.get("line"),
            "Δ Line":        dl,
            "Book Gap":      book_gap,
            "Consensus":     consensus,
            "🎯 Rec":        rec,
            "Over Open":     o_side.get("over_price"),
            "Over Current":  c_side.get("over_price"),
            "Δ Over":        dp_over,
            "Under Open":    o_side.get("under_price"),
            "Under Current": c_side.get("under_price"),
            "Δ Under":       dp_under,
            "Move":          _emoji(dl, max(abs(dp_over or 0), abs(dp_under or 0))),
        })

df = pd.DataFrame(rows)

# ---------- Today's plays ----------

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

        st.markdown("### 🎯 Today's actionable plays")
        st.caption(
            "🔄 **RLM** = line and price disagree (sharpest) · "
            "🎯 **Consensus** = 2+ books agree (follow the move) · "
            "↩️ **Fade FD** = FD-only mover · "
            "Score refreshes every 60s"
        )
        live_scores = _fetch_live_scores(sel_date)
        plays["Score"] = plays["Team"].map(lambda t: live_scores.get(t, "–"))
        st.dataframe(
            plays[["Score", "Team", "Open", "Current", "Δ Line", "Consensus", "🎯 Rec",
                   "Over Current", "Under Current"]],
            use_container_width=True, hide_index=True,
            column_config={
                "Score":         st.column_config.TextColumn("🏒 Score"),
                "Open":          st.column_config.NumberColumn(format="%.2f"),
                "Current":       st.column_config.NumberColumn(format="%.2f"),
                "Δ Line":        st.column_config.NumberColumn(format="%+.2f"),
                "Over Current":  st.column_config.NumberColumn(format="%+d"),
                "Under Current": st.column_config.NumberColumn(format="%+d"),
            },
        )
    else:
        st.info("🎯 No actionable plays yet — waiting for meaningful line movement (0.25+ goals).")

# Book Disagreements — Fanatics vs FanDuel line gap ≥ 0.5
if not df.empty:
    gaps = df[df["Book Gap"] != ""].copy()
    if not gaps.empty:
        st.markdown("### 📋 Book Disagreements (Fanatics ≠ FanDuel)")
        st.caption(
            "These teams have **different lines on Fanatics vs FanDuel right now** — "
            "one book may be sharper or slower to move. "
            "Fanatics is the anchor used for Open/Current above."
        )
        live_scores_gap = _fetch_live_scores(sel_date)
        gaps["Score"] = gaps["Team"].map(lambda t: live_scores_gap.get(t, "–"))
        st.dataframe(
            gaps[["Score", "Team", "Game", "Current", "Book Gap"]],
            use_container_width=True, hide_index=True,
            column_config={
                "Score":   st.column_config.TextColumn("🏒 Score"),
                "Current": st.column_config.NumberColumn("Fanatics Line", format="%.2f"),
                "Book Gap": st.column_config.TextColumn("Fan vs FD"),
            },
        )
    st.divider()

# Filter
fc1, fc2 = st.columns([1, 4])
with fc1:
    only_moves = st.toggle("🔥 Only show movement")

if only_moves and not df.empty:
    df = df[(df["Δ Line"].fillna(0) != 0) |
            (df["Δ Over"].fillna(0) != 0) |
            (df["Δ Under"].fillna(0) != 0)]

st.dataframe(
    df, use_container_width=True, hide_index=True,
    column_config={
        "Open":          st.column_config.NumberColumn(format="%.2f"),
        "Current":       st.column_config.NumberColumn(format="%.2f"),
        "Δ Line":        st.column_config.NumberColumn(format="%+.2f"),
        "Book Gap":      st.column_config.TextColumn("Book Gap"),
        "Over Open":     st.column_config.NumberColumn(format="%+d"),
        "Over Current":  st.column_config.NumberColumn(format="%+d"),
        "Δ Over":        st.column_config.NumberColumn(format="%+d"),
        "Under Open":    st.column_config.NumberColumn(format="%+d"),
        "Under Current": st.column_config.NumberColumn(format="%+d"),
        "Δ Under":       st.column_config.NumberColumn(format="%+d"),
    },
)

# ---------- Line trajectory chart ----------

st.divider()
st.subheader("📊 Line trajectory")

game_options = sorted({g["game"] for g in current} | {g["game"] for g in opening})
if game_options:
    sel_game = st.selectbox("Pick a game", game_options)
    if sel_game:
        rows_ts = []
        for snap in snapshots:
            t = snap.get("captured_at")
            for g in snap.get("games", []):
                if g["game"] != sel_game:
                    continue
                rows_ts.append({
                    "Time": t,
                    f"{g['away_team']} line": g.get("away", {}).get("line"),
                    f"{g['home_team']} line": g.get("home", {}).get("line"),
                })
        if rows_ts:
            ts_df = pd.DataFrame(rows_ts)
            ts_df["Time"] = pd.to_datetime(ts_df["Time"], errors="coerce")
            ts_df = ts_df.set_index("Time")
            st.line_chart(ts_df)
        else:
            st.caption("No time-series data for this game yet.")

st.divider()
st.caption(
    f"Data source: Fanatics (primary — best NHL coverage) + FanDuel via The Odds API · "
    f"Snapshots merged: {payload.get('n_snapshots', len(snapshots))}"
)
