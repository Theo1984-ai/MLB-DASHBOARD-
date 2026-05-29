"""
Data Status — quick health dashboard for every saved data feed.

Shows:
  - Which trackers have data saved, and through what date
  - How many picks per day per system
  - W/L/ROI on systems that have a settled `summary` block
  - File counts, sizes, last-modified times
  - Recent activity ribbon (last 14 days, what was saved each day)

Reads everything from local repo files (cheap, no API calls). Use this
to verify the daily cron is actually doing its job.
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

st.set_page_config(page_title="Data Status", page_icon="📊", layout="wide")
st.title("📊 Data Status — All Tracker Feeds")
st.caption(
    "Health dashboard for every saved data feed. Confirms the daily 12 PM ET "
    "cron is running and saving picks for HR, H+R+R, True Probability, and "
    "Soft Scanner. Settled days show win rate + ROI."
)


# ---------- Tracker config ----------

TRACKERS = [
    {
        "name":     "HR Tracker",
        "icon":     "💣",
        "dir":      "hr_tracker",
        "page":     "2_HR_Tracker",
        "has_settle": False,
        "desc":     "Top 7 HR picks (STRICT filter: odds attached, edge >= -2pp, confidence >= 45)",
    },
    {
        "name":     "H+R+R Tracker",
        "icon":     "🏃",
        "dir":      "hrr_tracker",
        "page":     "2_HR_Tracker",
        "has_settle": False,
        "desc":     "Top 6 H+R+R Over 1.5 picks (juice cap -180, implied >= 50%)",
    },
    {
        "name":     "True Probability",
        "icon":     "🎯",
        "dir":      "true_prob_history",
        "page":     "6_True_Probability",
        "has_settle": True,
        "desc":     "All markets at 75%+ consensus true probability (4+ books)",
    },
    {
        "name":     "Soft Scanner",
        "icon":     "🔍",
        "dir":      "soft_scanner_history",
        "page":     "3_Soft_Scanner",
        "has_settle": True,
        "desc":     "Top 30 cross-book disagreements (5pp+ edge, 3+ books, -300/+300 cap)",
    },
]


def load_files(dirname):
    """Returns list of (date_str, payload, mtime, size_bytes) sorted asc by date."""
    path = os.path.join(ROOT, dirname)
    if not os.path.isdir(path):
        return []
    out = []
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".json"):
            continue
        date_str = fn[:-5]
        full = os.path.join(path, fn)
        try:
            with open(full) as f:
                payload = json.load(f)
            mtime = datetime.fromtimestamp(os.path.getmtime(full), tz=EASTERN)
            size = os.path.getsize(full)
            out.append((date_str, payload, mtime, size))
        except Exception:
            pass
    return out


# Load everything once
all_data = {t["dir"]: load_files(t["dir"]) for t in TRACKERS}
today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")


# ---------- Date picker + per-day results ----------

st.markdown("### 🎯 Results for a specific day")

# Build the universe of dates that have ANY saved data
all_dates = set()
for files in all_data.values():
    for f in files:
        all_dates.add(f[0])
all_dates_sorted = sorted(all_dates, reverse=True)

if not all_dates_sorted:
    st.info("No saved data yet — the cron hasn't run, or the trackers haven't been populated.")
else:
    # Default to today if it exists, else most recent
    default_idx = 0 if today_et in all_dates_sorted else 0
    if today_et in all_dates_sorted:
        default_idx = all_dates_sorted.index(today_et)
    sel_date = st.selectbox(
        "Pick a date",
        options=all_dates_sorted,
        index=default_idx,
        format_func=lambda d: (
            f"{d}  ({datetime.strptime(d, '%Y-%m-%d').strftime('%a, %b %d')})"
            + ("  — TODAY" if d == today_et else "")
        ),
    )

    # Aggregate counts for the picked day
    summary_row = st.columns(len(TRACKERS) + 1)
    total_picks_day = 0
    total_w = total_l = total_p = 0
    for i, t in enumerate(TRACKERS):
        files_by_date = {f[0]: f[1] for f in all_data[t["dir"]]}
        payload = files_by_date.get(sel_date)
        if payload:
            n = payload.get("n_picks") or len(payload.get("picks", []))
            total_picks_day += n
            picks = payload.get("picks", [])
            settled_w = sum(1 for p in picks if p.get("result") == "WIN")
            settled_l = sum(1 for p in picks if p.get("result") == "LOSS")
            settled_p = sum(1 for p in picks if p.get("result") == "PUSH")
            total_w += settled_w; total_l += settled_l; total_p += settled_p
            if settled_w + settled_l > 0:
                rate = settled_w / (settled_w + settled_l) * 100
                summary_row[i].metric(
                    f"{t['icon']} {t['name']}",
                    f"{n} picks",
                    f"{settled_w}W-{settled_l}L ({rate:.0f}%)",
                )
            else:
                summary_row[i].metric(
                    f"{t['icon']} {t['name']}",
                    f"{n} picks",
                    "pending" if n else "",
                    delta_color="off",
                )
        else:
            summary_row[i].metric(f"{t['icon']} {t['name']}", "—", "no save")
    if total_w + total_l > 0:
        day_rate = total_w / (total_w + total_l) * 100
        summary_row[-1].metric("📊 Day total",
                               f"{total_picks_day} picks",
                               f"{total_w}W-{total_l}L-{total_p}P ({day_rate:.0f}%)")
    else:
        summary_row[-1].metric("📊 Day total", f"{total_picks_day} picks", "no settled")

    # Detail tables per tracker for this date
    st.markdown(f"#### Picks for {sel_date}")
    detail_tabs = st.tabs([f"{t['icon']} {t['name']}" for t in TRACKERS])
    for i, t in enumerate(TRACKERS):
        with detail_tabs[i]:
            files_by_date = {f[0]: f[1] for f in all_data[t["dir"]]}
            payload = files_by_date.get(sel_date)
            if not payload:
                st.info(f"No {t['name']} save for {sel_date}.")
                continue
            picks = payload.get("picks", [])
            if not picks:
                st.warning(f"{t['name']} ran but produced 0 picks (filter rejected all).")
                continue

            # Normalize fields across tracker formats
            rows = []
            for p in picks:
                # Player/selection
                sel = p.get("selection") or p.get("player") or p.get("batter") or "?"
                # Market
                mkt = p.get("market") or "HR" if t["dir"] == "hr_tracker" else p.get("market", "")
                if t["dir"] == "hrr_tracker":
                    mkt = "H+R+R"
                if t["dir"] == "hr_tracker":
                    mkt = "HR"
                # Side / line
                side = p.get("side", "")
                line = p.get("point")
                # Price
                price = p.get("best_price") or p.get("best_odds")
                book = p.get("best_book", "")
                # Edge / probability
                edge = p.get("edge_pp")
                true_prob = p.get("true_prob_pct") or p.get("consensus_pct") or p.get("model_p_pct")
                # Result
                result = p.get("result", "")
                detail = p.get("settle_detail", "")
                # Game
                game = p.get("game") or p.get("matchup", "")
                row = {
                    "Game":     game,
                    "Player":   sel,
                    "Market":   mkt,
                    "Side":     side,
                    "Line":     line,
                    "Price":    price,
                    "Book":     book,
                    "TrueP %":  true_prob,
                    "Edge pp":  edge,
                    "Result":   result if result else ("pending" if t["has_settle"] else ""),
                    "Detail":   detail,
                }
                rows.append(row)

            df = pd.DataFrame(rows)
            # Drop columns that are entirely empty/None for cleanliness
            for c in list(df.columns):
                if df[c].isna().all() or (df[c].astype(str).str.strip() == "").all():
                    df = df.drop(columns=c)

            # Color-code Result column
            def color_result(val):
                if val == "WIN":  return "background-color: #1f7a1f; color: white"
                if val == "LOSS": return "background-color: #a52a2a; color: white"
                if val == "PUSH": return "background-color: #888; color: white"
                return ""
            cfg = {}
            if "TrueP %" in df.columns:
                df["TrueP %"] = pd.to_numeric(df["TrueP %"], errors="coerce")
                cfg["TrueP %"] = st.column_config.NumberColumn(format="%.1f%%")
            if "Edge pp" in df.columns:
                df["Edge pp"] = pd.to_numeric(df["Edge pp"], errors="coerce")
                cfg["Edge pp"] = st.column_config.NumberColumn(format="%+.1f")
            if "Price" in df.columns:
                df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
                cfg["Price"] = st.column_config.NumberColumn(format="%+d")

            styled = df.style.map(color_result, subset=["Result"]) if "Result" in df.columns else df
            st.dataframe(styled, use_container_width=True, hide_index=True, column_config=cfg)

st.markdown("---")


# ---------- Hero metrics ----------

st.markdown("### 🏥 Health overview")

cols = st.columns(len(TRACKERS))
for i, t in enumerate(TRACKERS):
    files = all_data[t["dir"]]
    n_days = len(files)
    if files:
        last_date = files[-1][0]
        last_picks = files[-1][1].get("n_picks") or len(files[-1][1].get("picks", []))
        days_stale = (datetime.now(tz=EASTERN).date() -
                      datetime.strptime(last_date, "%Y-%m-%d").date()).days
        if days_stale == 0:
            status = "🟢 today"
        elif days_stale == 1:
            status = "🟡 1 day ago"
        else:
            status = f"🔴 {days_stale} days stale"
    else:
        last_date = "—"
        last_picks = 0
        status = "🔴 no data"

    cols[i].metric(
        label=f"{t['icon']} {t['name']}",
        value=f"{n_days} days",
        delta=f"last: {last_date}",
        delta_color="off",
    )
    cols[i].caption(f"{status}  •  {last_picks} picks last save")


# ---------- Recent activity ribbon ----------

st.markdown("---")
st.markdown("### 🗓️ Last 14 days — what was saved each day")

# Build a date × tracker grid
end = datetime.now(tz=EASTERN).date()
days = [end - timedelta(days=i) for i in range(13, -1, -1)]
grid_rows = []
for d in days:
    ds = d.strftime("%Y-%m-%d")
    row = {"Date": ds, "Day": d.strftime("%a")}
    for t in TRACKERS:
        files_by_date = {f[0]: f[1] for f in all_data[t["dir"]]}
        if ds in files_by_date:
            payload = files_by_date[ds]
            n = payload.get("n_picks") or len(payload.get("picks", []))
            summ = payload.get("summary") or {}
            if summ.get("n_settled", 0) > 0:
                row[t["name"]] = f"✅ {n} picks ({summ['wins']}W-{summ['losses']}L)"
            elif n > 0:
                row[t["name"]] = f"📥 {n} picks"
            else:
                row[t["name"]] = "0 picks"
        else:
            row[t["name"]] = "—"
    grid_rows.append(row)

grid_df = pd.DataFrame(grid_rows)
st.dataframe(grid_df, use_container_width=True, hide_index=True)


# ---------- Per-tracker detail tabs ----------

st.markdown("---")
st.markdown("### 📂 Per-tracker history")

tabs = st.tabs([f"{t['icon']} {t['name']}" for t in TRACKERS])

for idx, t in enumerate(TRACKERS):
    with tabs[idx]:
        files = all_data[t["dir"]]
        st.caption(t["desc"])

        if not files:
            st.warning(f"No data saved yet in `{t['dir']}/`.")
            continue

        # Summary metrics across all saved days
        total_picks = sum(
            (f[1].get("n_picks") or len(f[1].get("picks", []))) for f in files
        )
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total days saved", len(files))
        c2.metric("Total picks", total_picks)
        c3.metric("Avg picks/day", f"{total_picks / len(files):.1f}" if files else "0")
        total_size_kb = sum(f[3] for f in files) / 1024
        c4.metric("Total file size", f"{total_size_kb:.1f} KB")

        # If settled, roll up performance
        if t["has_settle"]:
            tot_w = tot_l = tot_p = 0
            tot_risk = tot_profit = 0.0
            settled_days = 0
            for _date, payload, _m, _s in files:
                summ = payload.get("summary") or {}
                if summ.get("n_settled", 0) > 0:
                    settled_days += 1
                    tot_w += summ.get("wins", 0)
                    tot_l += summ.get("losses", 0)
                    tot_p += summ.get("pushes", 0)
                    tot_risk += summ.get("risk_total", 0)
                    tot_profit += summ.get("profit_total", 0)

            if settled_days > 0:
                st.markdown("#### 📈 Aggregate performance (settled days only)")
                pc1, pc2, pc3, pc4 = st.columns(4)
                hit_rate = (tot_w / (tot_w + tot_l) * 100) if (tot_w + tot_l) else 0
                roi = (tot_profit / tot_risk * 100) if tot_risk else 0
                pc1.metric("Settled days", settled_days)
                pc2.metric("W-L-P", f"{tot_w}-{tot_l}-{tot_p}")
                pc3.metric("Hit rate", f"{hit_rate:.1f}%")
                pc4.metric("ROI", f"{roi:+.1f}%",
                           delta=f"${tot_profit:+.0f} on ${tot_risk:.0f}")

        # Day-by-day table
        st.markdown("#### 📅 Day-by-day")
        rows = []
        for date_str, payload, mtime, size in reversed(files):
            n = payload.get("n_picks") or len(payload.get("picks", []))
            summ = payload.get("summary") or {}
            settled = summ.get("n_settled", 0)
            row = {
                "Date":   date_str,
                "Picks":  n,
                "Saved":  mtime.strftime("%m/%d %I:%M %p"),
                "Size":   f"{size / 1024:.1f} KB",
            }
            if t["has_settle"]:
                if settled > 0:
                    row["Settled"] = f"{summ['wins']}W-{summ['losses']}L-{summ['pushes']}P"
                    row["Hit %"]   = summ.get("hit_rate")
                    row["Net $"]   = summ.get("profit_total")
                    row["ROI %"]   = summ.get("roi_pct")
                else:
                    row["Settled"] = "pending"
                    row["Hit %"]   = None
                    row["Net $"]   = None
                    row["ROI %"]   = None
            rows.append(row)
        rdf = pd.DataFrame(rows)
        col_cfg = {}
        if t["has_settle"]:
            col_cfg = {
                "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
                "Net $":  st.column_config.NumberColumn(format="$%+.0f"),
                "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
            }
        st.dataframe(rdf, use_container_width=True, hide_index=True, column_config=col_cfg)


# ---------- Tonight's Picks file ----------

st.markdown("---")
st.markdown("### 🌟 Tonight's Picks file")

latest_path = os.path.join(ROOT, "tonight_picks", "latest.json")
if os.path.exists(latest_path):
    mtime = datetime.fromtimestamp(os.path.getmtime(latest_path), tz=EASTERN)
    size = os.path.getsize(latest_path)
    try:
        with open(latest_path) as f:
            tn = json.load(f)
        n_picks = len(tn.get("picks", []))
        scanned_at = tn.get("scanned_at", "?")
        c1, c2, c3 = st.columns(3)
        c1.metric("Picks in file", n_picks)
        c2.metric("Last saved", mtime.strftime("%m/%d %I:%M %p"))
        c3.metric("File size", f"{size / 1024:.1f} KB")
        st.caption(f"Scanned at: {scanned_at}  •  Path: `tonight_picks/latest.json`")
    except Exception as e:
        st.error(f"Couldn't read latest.json: {e}")
else:
    st.warning("`tonight_picks/latest.json` not found — has the Tier A scanner ever run?")


# ---------- Cron expectations + footer ----------

st.markdown("---")
st.markdown("### ⏰ Cron schedule")
st.markdown(
    "- **12:00 PM ET daily** — GitHub Actions runs `daily_all.py`\n"
    "  - Settles yesterday's True Probability + Soft Scanner snapshots\n"
    "  - Takes today's True Prob + Soft Scanner snapshots\n"
    "  - Generates HR + H+R+R picks with STRICT filter\n"
    "  - Commits + pushes everything to GitHub\n"
    "\n"
    "If a tracker shows `🔴 N days stale` above, the cron failed that day. "
    "Check https://github.com/Theo1984-ai/MLB-DASHBOARD-/actions for the failed run."
)

st.caption(
    f"Page generated at {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  •  "
    f"Reads only local files (no API quota burn)"
)
