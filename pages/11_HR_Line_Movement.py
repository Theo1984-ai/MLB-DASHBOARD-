"""
🎯 HR Line Movement — DraftKings HR props tracked twice-daily.

Same schedule as Team Totals: 11 PM ET the night before (opener) +
12 PM ET on game day (pre-first-pitch), plus manual refresh.

Shows per-player DK HR odds side-by-side (Open vs Current) and highlights
players where the price moved by 100+ points. Big moves = where sharps
have pounded a line or the book has adjusted after news.
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")
HISTORY_DIR = os.path.join(ROOT, "hr_line_movement")

st.set_page_config(page_title="HR Line Movement", page_icon="🎯", layout="wide")
st.title("🎯 HR Line Movement — DraftKings")
st.caption(
    "DraftKings HR props (batter-to-hit-a-home-run). Snapshots at "
    "**11 PM ET the night before** and **12 PM ET on game day**, plus "
    "manual refresh on demand. Shows Open vs Current price side-by-side; "
    "**highlights players where the price moved ≥ 100 points**."
)


# ---------- Refresh button ----------

def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


GH_TOKEN = _resolve_secret("GITHUB_TOKEN")
ODDS_KEY = _resolve_secret("THE_ODDS_API_KEY")
OWNER = "Theo1984-ai"
REPO = "MLB-DASHBOARD-"

rc1, rc2 = st.columns([1, 5])
with rc1:
    refresh_btn = st.button("🔄 Take snapshot now", type="primary",
                            use_container_width=True,
                            disabled=not (GH_TOKEN and ODDS_KEY),
                            help="Runs the DK HR scanner now and appends a "
                                 "new snapshot to today's file. Pushes to "
                                 "GitHub so it persists.")
with rc2:
    if not ODDS_KEY:
        st.error("`THE_ODDS_API_KEY` not configured — refresh disabled.")
    elif not GH_TOKEN:
        st.error("`GITHUB_TOKEN` not configured — refresh disabled.")

if refresh_btn and ODDS_KEY and GH_TOKEN:
    with st.spinner("Pulling DraftKings HR props (~15 API calls)..."):
        try:
            os.environ["THE_ODDS_API_KEY"] = ODDS_KEY
            from scripts.hr_line_movement_snapshot import main as run_snapshot
            result = run_snapshot(force=True)
            if result.get("status") != "ok":
                st.error(f"Snapshot returned: {result}")
            else:
                from data import github_storage as gh
                path = result["path"]
                rel_path = os.path.relpath(path, ROOT).replace(os.sep, "/")
                target_date = result["target_date"]
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
                try:
                    gh.save_json(
                        GH_TOKEN, OWNER, REPO, rel_path, payload,
                        commit_msg=f"Manual HR line-movement snapshot {target_date}",
                    )
                    st.success(
                        f"✅ Snapshot #{result['snapshot_n']} for {target_date} · "
                        f"{result['n_players']} players captured · pushed to GitHub."
                    )
                except Exception as e:
                    st.warning(
                        f"Saved locally ({result['n_players']} players) but "
                        f"GitHub push failed: {e}"
                    )
                st.cache_data.clear()
                st.rerun()
        except Exception as e:
            import traceback
            st.error(f"Snapshot failed: {e}")
            with st.expander("Traceback"):
                st.code(traceback.format_exc()[:2000])


# ---------- Load day file ----------

def _load_day(date_str):
    path = os.path.join(HISTORY_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


available_dates = []
if os.path.isdir(HISTORY_DIR):
    for fn in sorted(os.listdir(HISTORY_DIR), reverse=True):
        if fn.endswith(".json"):
            available_dates.append(fn[:-5])

if not available_dates:
    st.info(
        "No HR line-movement snapshots yet. Take one with the button above, "
        "or wait for the scheduled 11 PM ET / 12 PM ET runs."
    )
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


# ---------- Summary strip ----------

# Use FIRST NON-EMPTY snapshot as the opening. DK often hasn't posted HR
# props at 11 PM ET the night before, so snapshot #1 can be empty.
def _first_populated(snaps):
    for s in snaps:
        if (s.get("n_players") or 0) > 0 and s.get("players"):
            return s
    return snaps[0] if snaps else {}

first_populated_snap = _first_populated(snapshots)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Snapshots", len(snapshots))
try:
    first_t = datetime.fromisoformat(
        first_populated_snap.get("captured_at", "")
    ).strftime("%I:%M %p")
except Exception:
    first_t = "?"
try:
    last_t = datetime.fromisoformat(snapshots[-1]["captured_at"]).strftime("%I:%M %p")
except Exception:
    last_t = "?"
c2.metric("First snapshot", first_t,
          help="First snapshot that actually captured HR props. Empty "
               "snapshots (DK hadn't posted yet) are skipped.")
c3.metric("Latest snapshot", last_t)
c4.metric("Players tracked", snapshots[-1].get("n_players", 0))

st.divider()


# ---------- Build the delta table ----------

opening = first_populated_snap.get("players", []) or []
current = snapshots[-1].get("players", []) or []
opening_map = {(p["player"], p["game"]): p for p in opening}
current_map = {(p["player"], p["game"]): p for p in current}
all_keys = sorted(set(opening_map) | set(current_map))

rows = []
for k in all_keys:
    o = opening_map.get(k, {})
    c = current_map.get(k, {})
    o_price = o.get("price")
    c_price = c.get("price")
    diff = None
    if o_price is not None and c_price is not None:
        diff = c_price - o_price
    rows.append({
        "Player":  k[0],
        "Game":    k[1],
        "Open":    o_price,
        "Current": c_price,
        "Δ":       diff,
    })

df = pd.DataFrame(rows)

# Filter controls
fc1, fc2 = st.columns([1.5, 3])
with fc1:
    min_diff = st.number_input(
        "Minimum |Δ| to show",
        min_value=0, max_value=500, value=100, step=10,
        help="Only show players whose price moved by at least this many "
             "American-odds points. Default 100 = the user's requested "
             "threshold. Lower = more results.",
    )
with fc2:
    only_big_moves = st.toggle(
        "🔥 Only show big moves",
        value=True,
        help="Hide players whose price didn't change or changed less than "
             "the minimum threshold.",
    )

if only_big_moves and not df.empty:
    df = df[df["Δ"].fillna(0).abs() >= min_diff]

if df.empty:
    st.info(
        f"No players with price moves ≥ {min_diff} points on {sel_date}. "
        f"Try lowering the threshold or wait for more snapshots to accumulate."
    )
else:
    # Sort by absolute magnitude of the move, biggest first
    df["_abs"] = df["Δ"].fillna(0).abs()
    df = df.sort_values("_abs", ascending=False).drop(columns="_abs")

    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={
            "Open":    st.column_config.NumberColumn(format="%+d",
                        help="DK American odds at first snapshot of the day"),
            "Current": st.column_config.NumberColumn(format="%+d",
                        help="DK American odds at latest snapshot"),
            "Δ":       st.column_config.NumberColumn(format="%+d",
                        help="Δ = Current − Open. Positive = price got LONGER "
                             "(less likely per market). Negative = price got "
                             "SHORTER (more likely per market)."),
        },
    )

    st.caption(
        f"Showing {len(df)} players with |Δ| ≥ {min_diff}. Positive Δ (e.g. "
        f"+150) means DK made the player LESS likely to hit a HR — sharps may "
        f"have taken the Under or the book adjusted after news. Negative Δ "
        f"(e.g. −180) means DK made the player MORE likely — money came in "
        f"on the Yes side."
    )


st.divider()
st.caption(
    f"Data source: DraftKings via The Odds API · Book: {payload.get('book','?')} · "
    f"Market: {payload.get('market','?')} · Snapshots merged: {payload.get('n_snapshots', 0)}"
)
