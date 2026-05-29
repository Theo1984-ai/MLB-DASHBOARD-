"""
Data Status — health + results dashboard for every saved data feed.

Cleaned-up version: lead with today's results, hide deep history behind
expanders, single status strip up top instead of repeated metric grids.
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
st.title("📊 Data Status")
st.caption("Daily picks + results across all four trackers. Auto-updates from the 12 PM ET cron.")


# ---------- Tracker config ----------

TRACKERS = [
    {"name": "HR",            "icon": "💣", "dir": "hr_tracker"},
    {"name": "H+R+R",         "icon": "🏃", "dir": "hrr_tracker"},
    {"name": "True Prob",     "icon": "🎯", "dir": "true_prob_history"},
    {"name": "Soft Scanner",  "icon": "🔍", "dir": "soft_scanner_history"},
]


def load_files(dirname):
    """Returns list of (date_str, payload, mtime, size_bytes) sorted asc."""
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


all_data = {t["dir"]: load_files(t["dir"]) for t in TRACKERS}
today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")


def freshness_emoji(date_str):
    if not date_str:
        return "⚪"
    try:
        days_stale = (datetime.now(tz=EASTERN).date()
                      - datetime.strptime(date_str, "%Y-%m-%d").date()).days
    except Exception:
        return "⚪"
    if days_stale == 0:  return "🟢"
    if days_stale == 1:  return "🟡"
    return "🔴"


# =============================================================================
# 1) STATUS STRIP — compact one-liner per tracker
# =============================================================================

status_cols = st.columns(len(TRACKERS))
for i, t in enumerate(TRACKERS):
    files = all_data[t["dir"]]
    if files:
        last_date = files[-1][0]
        last_payload = files[-1][1]
        n = last_payload.get("n_picks") or len(last_payload.get("picks", []))
        emoji = freshness_emoji(last_date)
        status_cols[i].markdown(
            f"### {t['icon']} {t['name']}\n"
            f"{emoji} **{last_date}** — {n} picks"
        )
    else:
        status_cols[i].markdown(
            f"### {t['icon']} {t['name']}\n"
            "⚪ no data yet"
        )

st.divider()


# =============================================================================
# 2) DAY RESULTS — date picker + per-tracker view
# =============================================================================

# Universe of dates with any saved data
all_dates = set()
for files in all_data.values():
    for f in files:
        all_dates.add(f[0])
all_dates_sorted = sorted(all_dates, reverse=True)

if not all_dates_sorted:
    st.info("No saved data yet — the cron hasn't run, or trackers haven't been populated.")
    st.stop()

# Date selector
default_idx = (all_dates_sorted.index(today_et)
               if today_et in all_dates_sorted else 0)
sel_date = st.selectbox(
    "📅 Select a date",
    options=all_dates_sorted,
    index=default_idx,
    format_func=lambda d: (
        f"{d}  ·  {datetime.strptime(d, '%Y-%m-%d').strftime('%a %b %d')}"
        + ("  · TODAY" if d == today_et else "")
    ),
)


# Aggregate the picked day
day_summary = {}
total_w = total_l = total_p = total_picks = 0
for t in TRACKERS:
    files_by_date = {f[0]: f[1] for f in all_data[t["dir"]]}
    payload = files_by_date.get(sel_date)
    if not payload:
        day_summary[t["name"]] = {"picks": 0, "w": 0, "l": 0, "p": 0,
                                  "settled": 0, "payload": None}
        continue
    picks = payload.get("picks", [])
    n = len(picks)
    w = sum(1 for p in picks if p.get("result") == "WIN")
    l = sum(1 for p in picks if p.get("result") == "LOSS")
    pu = sum(1 for p in picks if p.get("result") == "PUSH")
    day_summary[t["name"]] = {"picks": n, "w": w, "l": l, "p": pu,
                              "settled": w + l + pu, "payload": payload}
    total_w += w; total_l += l; total_p += pu; total_picks += n


# Day-level snapshot (5 small badges in a row)
badge_cols = st.columns(len(TRACKERS) + 1)
for i, t in enumerate(TRACKERS):
    s = day_summary[t["name"]]
    if s["picks"] == 0:
        badge_cols[i].metric(
            label=f"{t['icon']} {t['name']}",
            value="—",
            delta="no save",
            delta_color="off",
        )
    elif s["settled"] == 0:
        badge_cols[i].metric(
            label=f"{t['icon']} {t['name']}",
            value=f"{s['picks']} picks",
            delta="pending",
            delta_color="off",
        )
    else:
        rate = s["w"] / (s["w"] + s["l"]) * 100 if (s["w"] + s["l"]) else 0
        badge_cols[i].metric(
            label=f"{t['icon']} {t['name']}",
            value=f"{s['w']}-{s['l']}-{s['p']}",
            delta=f"{rate:.0f}% hit",
            delta_color="normal" if rate >= 50 else "inverse",
        )

# Day total
if total_w + total_l > 0:
    day_rate = total_w / (total_w + total_l) * 100
    badge_cols[-1].metric(
        label="📊 Day total",
        value=f"{total_w}-{total_l}-{total_p}",
        delta=f"{day_rate:.0f}% hit · {total_picks} picks",
        delta_color="normal" if day_rate >= 50 else "inverse",
    )
else:
    badge_cols[-1].metric(label="📊 Day total",
                          value=f"{total_picks} picks", delta="no settled",
                          delta_color="off")


# Detail tabs — picks for the selected day, color-coded
detail_tabs = st.tabs([f"{t['icon']} {t['name']}" for t in TRACKERS])
for i, t in enumerate(TRACKERS):
    with detail_tabs[i]:
        s = day_summary[t["name"]]
        payload = s["payload"]
        if not payload:
            st.caption(f"No {t['name']} save for {sel_date}.")
            continue
        picks = payload.get("picks", [])
        if not picks:
            st.caption(f"{t['name']} ran but produced 0 picks (filter rejected all).")
            continue

        rows = []
        for p in picks:
            sel = (p.get("selection") or p.get("player")
                   or p.get("batter") or "?")
            mkt = p.get("market") or ""
            if t["dir"] == "hr_tracker": mkt = "HR"
            if t["dir"] == "hrr_tracker": mkt = "H+R+R"
            side = p.get("side", "")
            line = p.get("point")
            price = p.get("best_price") or p.get("best_odds")
            book = p.get("best_book", "")
            edge = p.get("edge_pp")
            tp = (p.get("true_prob_pct") or p.get("consensus_pct")
                  or p.get("model_p_pct"))
            result = p.get("result") or ("pending" if s["settled"] > 0
                                          else "—")
            row = {
                "Player": sel, "Mkt": mkt, "Side": side,
                "Line": line, "Price": price, "Book": book,
                "TrueP %": tp, "Edge pp": edge,
                "Result": result,
                "Detail": p.get("settle_detail", ""),
            }
            rows.append(row)

        df = pd.DataFrame(rows)
        # Drop empty columns
        for c in list(df.columns):
            if df[c].isna().all() or (df[c].astype(str).str.strip() == "").all():
                df = df.drop(columns=c)

        # Color-code results
        def color_result(val):
            if val == "WIN":  return "background-color: #1f7a1f; color: white"
            if val == "LOSS": return "background-color: #a52a2a; color: white"
            if val == "PUSH": return "background-color: #666; color: white"
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

        styled = (df.style.map(color_result, subset=["Result"])
                  if "Result" in df.columns else df)
        st.dataframe(styled, use_container_width=True, hide_index=True,
                     column_config=cfg)


st.divider()


# =============================================================================
# 3) PERFORMANCE SUMMARY — aggregate across all settled days
# =============================================================================

st.markdown("### 📈 All-time performance")

perf_rows = []
for t in TRACKERS:
    files = all_data[t["dir"]]
    days_total = len(files)
    days_settled = 0
    w = l = p = 0
    risk = profit = 0.0
    for _d, payload, _m, _s in files:
        summ = payload.get("summary") or {}
        if summ.get("n_settled", 0) > 0:
            days_settled += 1
            w += summ.get("wins", 0)
            l += summ.get("losses", 0)
            p += summ.get("pushes", 0)
            risk += summ.get("risk_total", 0)
            profit += summ.get("profit_total", 0)
    hit = (w / (w + l) * 100) if (w + l) else None
    roi = (profit / risk * 100) if risk else None
    perf_rows.append({
        "Tracker":   f"{t['icon']} {t['name']}",
        "Days saved": days_total,
        "Settled":   days_settled,
        "W-L-P":     f"{w}-{l}-{p}" if days_settled else "—",
        "Hit %":     hit,
        "Net $":     profit if days_settled else None,
        "ROI %":     roi,
    })

perf_df = pd.DataFrame(perf_rows)
st.dataframe(
    perf_df, use_container_width=True, hide_index=True,
    column_config={
        "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
        "Net $":  st.column_config.NumberColumn(format="$%+.0f"),
        "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
    },
)


# =============================================================================
# 4) DEEP DETAIL — collapsed by default
# =============================================================================

with st.expander("🗓️ Last 14 days activity grid"):
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
                    row[t["name"]] = f"✅ {n} ({summ['wins']}-{summ['losses']})"
                elif n > 0:
                    row[t["name"]] = f"📥 {n}"
                else:
                    row[t["name"]] = "0"
            else:
                row[t["name"]] = "—"
        grid_rows.append(row)
    st.dataframe(pd.DataFrame(grid_rows), use_container_width=True, hide_index=True)


with st.expander("📂 Per-tracker day-by-day history"):
    history_tabs = st.tabs([f"{t['icon']} {t['name']}" for t in TRACKERS])
    for i, t in enumerate(TRACKERS):
        with history_tabs[i]:
            files = all_data[t["dir"]]
            if not files:
                st.caption(f"No data saved yet in `{t['dir']}/`.")
                continue
            rows = []
            for date_str, payload, mtime, size in reversed(files):
                n = payload.get("n_picks") or len(payload.get("picks", []))
                summ = payload.get("summary") or {}
                row = {
                    "Date":  date_str,
                    "Picks": n,
                    "W-L-P": (f"{summ.get('wins',0)}-{summ.get('losses',0)}"
                              f"-{summ.get('pushes',0)}"
                              if summ.get("n_settled", 0) > 0 else "pending"),
                    "Hit %": summ.get("hit_rate"),
                    "Net $": summ.get("profit_total"),
                    "ROI %": summ.get("roi_pct"),
                }
                rows.append(row)
            st.dataframe(
                pd.DataFrame(rows), use_container_width=True, hide_index=True,
                column_config={
                    "Hit %":  st.column_config.NumberColumn(format="%.1f%%"),
                    "Net $":  st.column_config.NumberColumn(format="$%+.0f"),
                    "ROI %":  st.column_config.NumberColumn(format="%+.1f%%"),
                },
            )


with st.expander("ℹ️ How this works"):
    st.markdown(
        "- **12:00 PM ET daily** — GitHub Actions runs `daily_all.py`\n"
        "- Settles yesterday's picks across all 4 trackers via MLB Stats API\n"
        "- Takes today's snapshots and commits everything back to GitHub\n"
        "- If a tracker shows 🔴 above, check "
        "[GitHub Actions](https://github.com/Theo1984-ai/MLB-DASHBOARD-/actions) "
        "for the failed run.\n"
        "\n"
        "Tonight's Picks file: read live by the Tonight's Picks page — "
        "doesn't accumulate history, so no W/L column here."
    )

st.caption(
    f"Generated {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"reads local files only · no API quota burn"
)
