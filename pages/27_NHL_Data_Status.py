"""
NHL Data Status — health + results dashboard for every saved NHL data feed.

Same layout as 7_Data_Status.py (MLB):
  - Odds API quota indicator
  - Status strip (one-liner per tracker)
  - Day picker + per-tracker pick detail tabs
  - All-time performance summary
  - Deep detail (14-day grid + per-tracker history) in expanders
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

st.set_page_config(page_title="NHL Data Status", page_icon="🏒", layout="wide")
st.title("🏒 NHL Data Status")
st.caption("Daily picks + results across all NHL trackers. Auto-updates from the daily cron.")


# ---------- Odds API quota indicator ----------

def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


@st.cache_data(ttl=600, show_spinner=False)
def check_quota():
    key = _resolve_secret("THE_ODDS_API_KEY")
    if not key:
        return None
    try:
        import urllib.request, ssl as _ssl
        ctx = _ssl._create_unverified_context()
        r = urllib.request.urlopen(
            f"https://api.the-odds-api.com/v4/sports?apiKey={key}",
            timeout=10, context=ctx)
        return {
            "used":      int(r.headers.get("x-requests-used", -1)),
            "remaining": int(r.headers.get("x-requests-remaining", -1)),
            "status":    "ok",
        }
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Unauthorized" in msg:
            return {"used": None, "remaining": 0, "status": "exhausted"}
        return {"used": None, "remaining": None, "status": f"error: {msg[:60]}"}


QUOTA_RESET_DATE = "2026-06-30"


def _days_until(date_str):
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        today  = datetime.now(tz=EASTERN).date()
        return (target - today).days
    except Exception:
        return None


_q = check_quota()
if _q is None:
    pass
elif _q["status"] == "exhausted":
    days_left = _days_until(QUOTA_RESET_DATE)
    if days_left and days_left > 0:
        reset_msg = (f"Quota resets in **{days_left} day{'s' if days_left != 1 else ''}** "
                     f"on **{QUOTA_RESET_DATE}**. Snapshots resume automatically.")
    elif days_left == 0:
        reset_msg = "**Quota resets today.** Snapshots should resume on the next cron firing."
    else:
        reset_msg = "Reset date may have passed — quota may still be syncing."
    st.error(
        "🚨 **Odds API quota exhausted.** NHL snapshots are paused. "
        "Historical data and this page still work normally.  \n"
        f"{reset_msg}",
        icon="🚨",
    )
elif _q["remaining"] is not None and _q["remaining"] < 200:
    st.warning(
        f"⚠️ **Odds API quota low: {_q['remaining']} calls remaining** "
        f"({_q['used']} used). Each daily NHL cron uses ~30-60 calls. "
        f"Resets {QUOTA_RESET_DATE}.",
        icon="⚠️",
    )
elif _q["remaining"] is not None:
    days_left = _days_until(QUOTA_RESET_DATE)
    suffix = f" · resets in {days_left}d" if days_left and days_left > 0 else ""
    st.caption(
        f"Odds API quota: {_q['used']} used · {_q['remaining']} remaining{suffix}"
    )


# ---------- Helpers ----------

def _recompute_summary(picks):
    """Rebuild summary from a filtered list of picks."""
    wins   = sum(1 for p in picks if p.get("result") == "WIN")
    losses = sum(1 for p in picks if p.get("result") == "LOSS")
    pushes = sum(1 for p in picks if p.get("result") == "PUSH")
    voids  = sum(1 for p in picks if p.get("result") == "VOID")
    settled = wins + losses + pushes
    risk_total = profit_total = 0.0
    for p in picks:
        r = p.get("result")
        if r not in ("WIN", "LOSS", "PUSH"):
            continue
        am = p.get("best_price") or 0
        if am > 0: risk, payout = 100, am
        else:      risk, payout = abs(am), 100
        risk_total += risk
        if r == "WIN":    profit_total += payout
        elif r == "LOSS": profit_total -= risk
    return {
        "n_total":      len(picks),
        "n_settled":    settled,
        "n_void":       voids,
        "wins":         wins,
        "losses":       losses,
        "pushes":       pushes,
        "hit_rate":     round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0,
        "risk_total":   round(risk_total, 2),
        "profit_total": round(profit_total, 2),
        "roi_pct":      round(profit_total / risk_total * 100, 2) if risk_total else 0,
    }


def _pick_count(payload):
    """Works for both picks-based files and snapshot-based files."""
    if payload is None:
        return 0
    # picks-based (True Prob, Sharp Money)
    n = payload.get("n_picks")
    if n is not None:
        return n
    picks = payload.get("picks")
    if picks is not None:
        return len(picks)
    # snapshot-based (Team Totals, Props)
    return payload.get("n_snapshots", 0)


def freshness_emoji(date_str):
    if not date_str:
        return "⚪"
    try:
        days_stale = (datetime.now(tz=EASTERN).date()
                      - datetime.strptime(date_str, "%Y-%m-%d").date()).days
    except Exception:
        return "⚪"
    if days_stale == 0: return "🟢"
    if days_stale == 1: return "🟡"
    return "🔴"


def fmt_line(x):
    if x is None:
        return ""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if pd.isna(v):
        return ""
    if v == int(v):
        return str(int(v))
    s = f"{v:g}"
    if s.startswith("0."):    return s[1:]
    if s.startswith("-0."):   return "-" + s[2:]
    return s


# ---------- Tracker config ----------

TRACKERS = [
    {"name": "True Prob",   "icon": "🎯", "dir": "nhl_true_prob_history"},
    {"name": "Sharp Money", "icon": "💰", "dir": "nhl_sharp_money_history"},
    {"name": "Team Totals", "icon": "📊", "dir": "nhl_team_totals_history", "data_feed": True},
    {"name": "Props",       "icon": "🏒", "dir": "nhl_props_history",        "data_feed": True},
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
            with open(full, encoding="utf-8") as f:
                payload = json.load(f)
            mtime = datetime.fromtimestamp(os.path.getmtime(full), tz=EASTERN)
            size  = os.path.getsize(full)
            out.append((date_str, payload, mtime, size))
        except Exception:
            pass
    return out


all_data   = {t["name"]: load_files(t["dir"]) for t in TRACKERS}
today_et   = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")


# =============================================================================
# 1) STATUS STRIP
# =============================================================================

status_cols = st.columns(len(TRACKERS))
for i, t in enumerate(TRACKERS):
    files = all_data[t["name"]]
    if files:
        last_date    = files[-1][0]
        last_payload = files[-1][1]
        n     = _pick_count(last_payload)
        emoji = freshness_emoji(last_date)
        unit  = "snapshots" if t.get("data_feed") else "picks"
        status_cols[i].markdown(
            f"### {t['icon']} {t['name']}\n"
            f"{emoji} **{last_date}** — {n} {unit}"
        )
    else:
        status_cols[i].markdown(
            f"### {t['icon']} {t['name']}\n"
            "⚪ no data yet"
        )

st.divider()


# =============================================================================
# 2) DAY RESULTS
# =============================================================================

all_dates = set()
for files in all_data.values():
    for f in files:
        all_dates.add(f[0])
all_dates_sorted = sorted(all_dates, reverse=True)

if not all_dates_sorted:
    st.info("No saved NHL data yet — the cron hasn't run, or trackers haven't been populated.")
    st.stop()

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

# Aggregate for the selected day
day_summary = {}
total_w = total_l = total_p = total_picks = 0
for t in TRACKERS:
    files_by_date = {f[0]: f[1] for f in all_data[t["name"]]}
    payload = files_by_date.get(sel_date)
    if not payload:
        day_summary[t["name"]] = {"picks": 0, "w": 0, "l": 0, "p": 0,
                                  "settled": 0, "payload": None,
                                  "data_feed": t.get("data_feed", False)}
        continue
    picks  = payload.get("picks", [])
    n      = _pick_count(payload)
    w      = sum(1 for p in picks if p.get("result") == "WIN")
    l      = sum(1 for p in picks if p.get("result") == "LOSS")
    pu     = sum(1 for p in picks if p.get("result") == "PUSH")
    day_summary[t["name"]] = {"picks": n, "w": w, "l": l, "p": pu,
                              "settled": w + l + pu, "payload": payload,
                              "data_feed": t.get("data_feed", False)}
    total_w += w; total_l += l; total_p += pu; total_picks += n

# Badge row
badge_cols = st.columns(len(TRACKERS) + 1)
for i, t in enumerate(TRACKERS):
    s = day_summary[t["name"]]
    if s["data_feed"]:
        n = s["picks"]
        badge_cols[i].metric(
            label=f"{t['icon']} {t['name']}",
            value=f"{n} snapshots" if n else "—",
            delta="data feed" if n else "no save",
            delta_color="off",
        )
    elif s["picks"] == 0:
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

# Day total (pick-based only)
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
                          value=f"{total_picks} picks",
                          delta="no settled", delta_color="off")


# Detail tabs
detail_tabs = st.tabs([f"{t['icon']} {t['name']}" for t in TRACKERS])
for i, t in enumerate(TRACKERS):
    with detail_tabs[i]:
        s       = day_summary[t["name"]]
        payload = s["payload"]
        if not payload:
            st.caption(f"No {t['name']} save for {sel_date}.")
            continue

        # ── Data-feed trackers: show snapshot summary ──
        if t.get("data_feed"):
            snaps = payload.get("snapshots", [])
            if not snaps:
                st.caption(f"{t['name']}: {payload.get('n_snapshots', 0)} snapshots saved, no detail.")
                continue
            snap_rows = []
            for sn in snaps:
                row = {"Captured at": sn.get("captured_at", "")}
                if "n_games" in sn:
                    row["Games"] = sn["n_games"]
                if "n_props" in sn:
                    row["Props"] = sn["n_props"]
                snap_rows.append(row)
            st.caption(
                f"{t['name']}: **{len(snaps)} snapshots** on {sel_date}  ·  "
                f"book: {payload.get('book', '—')}  ·  "
                f"market: {payload.get('market', '—')}"
            )
            st.dataframe(pd.DataFrame(snap_rows), use_container_width=True, hide_index=True)
            continue

        # ── Pick-based trackers: show W/L pick table ──
        picks = payload.get("picks", [])
        if not picks:
            st.caption(f"{t['name']} ran but produced 0 picks for {sel_date}.")
            continue

        rows = []
        for p in picks:
            sel_val = (p.get("selection") or p.get("player") or
                       p.get("sharp_pick") or "?")
            market  = p.get("market") or p.get("category") or ""
            side    = p.get("side", "")
            line    = p.get("point")
            price   = p.get("best_price")
            book    = p.get("best_book", "") or p.get("sb_book", "")
            tp      = p.get("true_prob_pct")
            ev      = p.get("ev_per_100")
            result  = p.get("result") or (
                "pending" if s["settled"] > 0 else "—")

            row = {
                "Selection": sel_val,
                "Market":    market,
                "Side":      side,
                "Line":      line,
                "Price":     price,
                "Book":      book,
                "TrueP %":   tp,
                "EV/$100":   ev,
                "Result":    result,
            }
            rows.append(row)

        df = pd.DataFrame(rows)
        if "Line" in df.columns:
            df["Line"] = df["Line"].apply(fmt_line)
        # Drop empty columns
        for c in list(df.columns):
            if df[c].isna().all() or (df[c].astype(str).str.strip() == "").all():
                df = df.drop(columns=c)

        def color_result(val):
            if val == "WIN":     return "background-color: #1f7a1f; color: white"
            if val == "LOSS":    return "background-color: #a52a2a; color: white"
            if val == "PUSH":    return "background-color: #666; color: white"
            if val == "VOID":    return "background-color: #4a6fa5; color: white"
            if val == "NO_DATA": return "background-color: #999; color: white"
            return ""

        cfg = {}
        if "TrueP %" in df.columns:
            df["TrueP %"] = pd.to_numeric(df["TrueP %"], errors="coerce")
            cfg["TrueP %"] = st.column_config.NumberColumn(format="%.1f%%")
        if "EV/$100" in df.columns:
            df["EV/$100"] = pd.to_numeric(df["EV/$100"], errors="coerce")
            cfg["EV/$100"] = st.column_config.NumberColumn(format="$%+.2f")
        if "Price" in df.columns:
            df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
            cfg["Price"] = st.column_config.NumberColumn(format="%+d")

        styled = (df.style.map(color_result, subset=["Result"])
                  if "Result" in df.columns else df)
        st.dataframe(styled, use_container_width=True, hide_index=True, column_config=cfg)

st.divider()


# =============================================================================
# 3) ALL-TIME PERFORMANCE SUMMARY
# =============================================================================

st.markdown("### 📈 All-time performance")

perf_rows = []
for t in TRACKERS:
    files = all_data[t["name"]]
    days_total   = len(files)
    days_settled = 0
    w = l = p = 0
    risk = profit = 0.0

    if t.get("data_feed"):
        total_snaps = sum(
            f[1].get("n_snapshots", 0) for f in files
        )
        perf_rows.append({
            "Tracker":    f"{t['icon']} {t['name']}",
            "Days saved": days_total,
            "Settled":    "—",
            "W-L-P":      "data feed",
            "Hit %":      None,
            "Net $":      None,
            "ROI %":      None,
        })
        continue

    for _d, payload, _m, _s in files:
        summ = payload.get("summary") or {}
        if not summ:
            # Recompute from picks if summary missing
            picks = payload.get("picks", [])
            if picks:
                summ = _recompute_summary(picks)
        if summ.get("n_settled", 0) > 0:
            days_settled += 1
            w      += summ.get("wins", 0)
            l      += summ.get("losses", 0)
            p      += summ.get("pushes", 0)
            risk   += summ.get("risk_total", 0)
            profit += summ.get("profit_total", 0)

    hit = (w / (w + l) * 100) if (w + l) else None
    roi = (profit / risk * 100) if risk else None
    perf_rows.append({
        "Tracker":    f"{t['icon']} {t['name']}",
        "Days saved": days_total,
        "Settled":    days_settled,
        "W-L-P":      f"{w}-{l}-{p}" if days_settled else "—",
        "Hit %":      hit,
        "Net $":      profit if days_settled else None,
        "ROI %":      roi,
    })

perf_df = pd.DataFrame(perf_rows)
st.dataframe(
    perf_df, use_container_width=True, hide_index=True,
    column_config={
        "Hit %": st.column_config.NumberColumn(format="%.1f%%"),
        "Net $": st.column_config.NumberColumn(format="$%+.0f"),
        "ROI %": st.column_config.NumberColumn(format="%+.1f%%"),
    },
)


# =============================================================================
# 4) DEEP DETAIL — collapsed by default
# =============================================================================

with st.expander("🗓️ Last 14 days activity grid"):
    end  = datetime.now(tz=EASTERN).date()
    days = [end - timedelta(days=i) for i in range(13, -1, -1)]
    grid_rows = []
    for d in days:
        ds  = d.strftime("%Y-%m-%d")
        row = {"Date": ds, "Day": d.strftime("%a")}
        for t in TRACKERS:
            files_by_date = {f[0]: f[1] for f in all_data[t["name"]]}
            if ds in files_by_date:
                payload = files_by_date[ds]
                n = _pick_count(payload)
                summ = payload.get("summary") or {}
                if t.get("data_feed"):
                    row[t["name"]] = f"📥 {n}" if n else "0"
                elif summ.get("n_settled", 0) > 0:
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
            files = all_data[t["name"]]
            if not files:
                st.caption(f"No data saved yet in `{t['dir']}/`.")
                continue
            rows = []
            for date_str, payload, mtime, size in reversed(files):
                n    = _pick_count(payload)
                summ = payload.get("summary") or {}
                if not summ and not t.get("data_feed"):
                    picks = payload.get("picks", [])
                    if picks:
                        summ = _recompute_summary(picks)
                if t.get("data_feed"):
                    rows.append({
                        "Date":      date_str,
                        "Snapshots": n,
                        "W-L-P":     "—",
                        "Hit %":     None,
                        "Net $":     None,
                        "ROI %":     None,
                    })
                else:
                    rows.append({
                        "Date":  date_str,
                        "Picks": n,
                        "W-L-P": (f"{summ.get('wins',0)}-{summ.get('losses',0)}"
                                  f"-{summ.get('pushes',0)}"
                                  if summ.get("n_settled", 0) > 0 else "pending"),
                        "Hit %": summ.get("hit_rate"),
                        "Net $": summ.get("profit_total"),
                        "ROI %": summ.get("roi_pct"),
                    })
            st.dataframe(
                pd.DataFrame(rows), use_container_width=True, hide_index=True,
                column_config={
                    "Hit %": st.column_config.NumberColumn(format="%.1f%%"),
                    "Net $": st.column_config.NumberColumn(format="$%+.0f"),
                    "ROI %": st.column_config.NumberColumn(format="%+.1f%%"),
                },
            )


with st.expander("ℹ️ How this works"):
    st.markdown(
        "- **Daily cron** — GitHub Actions runs the NHL snapshot scripts once per day\n"
        "- Settles yesterday's True Prob + Sharp Money picks vs actual results\n"
        "- Takes today's Team Totals + Props snapshots and commits everything to GitHub\n"
        "- If a tracker shows 🔴 above, check "
        "[GitHub Actions](https://github.com/Theo1984-ai/MLB-DASHBOARD-/actions) "
        "for the failed run.\n"
        "\n"
        "**Tracker types:**\n"
        "- 🎯 True Prob — 75%+ consensus plays from 3+ sharp books; forward-tested\n"
        "- 💰 Sharp Money — Polymarket order-book skew signals; forward-tested\n"
        "- 📊 Team Totals — raw odds snapshots (no W/L tracking)\n"
        "- 🏒 Props — raw prop line snapshots (no W/L tracking)"
    )

st.caption(
    f"Generated {datetime.now(tz=EASTERN).strftime('%I:%M:%S %p %Z')}  ·  "
    f"reads local files only · no API quota burn"
)
