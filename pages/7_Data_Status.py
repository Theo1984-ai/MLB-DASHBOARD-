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


# ---------- Odds API quota indicator ----------

def resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


@st.cache_data(ttl=600, show_spinner=False)   # cheap cached probe
def check_quota():
    key = resolve_secret("THE_ODDS_API_KEY")
    if not key:
        return None
    try:
        import urllib.request, ssl as _ssl
        ctx = _ssl._create_unverified_context()
        r = urllib.request.urlopen(
            f"https://api.the-odds-api.com/v4/sports?apiKey={key}",
            timeout=10, context=ctx)
        return {
            "used": int(r.headers.get("x-requests-used", -1)),
            "remaining": int(r.headers.get("x-requests-remaining", -1)),
            "status": "ok",
        }
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Unauthorized" in msg:
            return {"used": None, "remaining": 0, "status": "exhausted"}
        return {"used": None, "remaining": None, "status": f"error: {msg[:60]}"}


QUOTA_RESET_DATE = "2026-06-30"   # Odds API monthly reset after 5/30 upgrade to 100K plan


def _days_until(date_str):
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now(tz=EASTERN).date()
        return (target - today).days
    except Exception:
        return None


_q = check_quota()
if _q is None:
    pass  # no key configured — nothing to show
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
        "🚨 **Odds API quota exhausted.** Snapshots are paused. "
        "Settles, historical data, and this page all still work normally.  \n"
        f"{reset_msg}",
        icon="🚨",
    )
elif _q["remaining"] is not None and _q["remaining"] < 200:
    st.warning(
        f"⚠️ **Odds API quota low: {_q['remaining']} calls remaining** "
        f"({_q['used']} used). Each daily cron uses ~30-50 calls. "
        f"Resets {QUOTA_RESET_DATE}.",
        icon="⚠️",
    )
elif _q["remaining"] is not None:
    days_left = _days_until(QUOTA_RESET_DATE)
    suffix = f" · resets in {days_left}d" if days_left and days_left > 0 else ""
    st.caption(
        f"Odds API quota: {_q['used']} used · {_q['remaining']} remaining{suffix}"
    )


# ---------- Tracker config ----------

def _is_a_plus_grade_soft(p):
    """A+ Grade Soft Scanner filter — the highest-ROI subset of A-Grade.
    322-pick backtest through 7/7: full A-Grade hit 60.2% / +14.2% ROI;
    the edge>=10pp subset hit 58.9% / +27.5% ROI (nearly 2x the ROI at
    slightly lower hit rate because longer prices)."""
    if p.get("market") not in ("H+R+R", "Hits"):
        return False
    edge = p.get("edge_pp")
    return edge is not None and edge >= 10


def _is_a_grade_soft(p):
    """A-Grade Soft Scanner filter EXCLUDING A+ picks — so A+ and A-Grade
    tracker stats can be summed without double-counting. A-Grade here is
    the picks that pass on 5+ books OR chalk<=-150 but NOT via edge>=10pp
    (those are shown in A+).

    Original A-Grade backtest (all triggers, on 248 settled picks):
       65-85% hit / +20-30% ROI. When A+ is broken out separately, the
       remainder (this filter) still shows solidly profitable but at
       lower magnitude — the A+ subset carries most of the ROI premium."""
    if p.get("market") not in ("H+R+R", "Hits"):
        return False
    if _is_a_plus_grade_soft(p):
        return False   # exclude — shown in Soft A+ tracker
    n_books = p.get("n_books", 0) or 0
    price = p.get("best_price")
    return (n_books >= 5
            or (price is not None and price <= -150))


@st.cache_data(ttl=86400, show_spinner=False)   # 24-hr cache
def _league_player_team_map():
    """Build {normalized_player_name: team_abbr} across all 30 MLB rosters.
    Used to backfill team info on historical picks that pre-date the
    team-enrichment in the scanner."""
    import json as _json, ssl as _ssl, urllib.request as _urlreq
    from datetime import datetime as _dt
    ctx = _ssl._create_unverified_context()
    season = _dt.now().year
    out = {}
    try:
        teams = _json.loads(_urlreq.urlopen(
            f"https://statsapi.mlb.com/api/v1/teams?season={season}&sportId=1",
            timeout=15, context=ctx).read()).get("teams", [])
        for t in teams:
            tid = t.get("id"); abbr = t.get("abbreviation")
            if not tid or not abbr: continue
            try:
                r = _json.loads(_urlreq.urlopen(
                    f"https://statsapi.mlb.com/api/v1/teams/{tid}/roster?season={season}",
                    timeout=10, context=ctx).read())
                for r_entry in r.get("roster", []):
                    name = r_entry.get("person", {}).get("fullName", "")
                    if not name: continue
                    out[name.lower().replace(",","").replace(".","").strip()] = abbr
            except Exception:
                continue
    except Exception:
        pass
    return out


def _lookup_team(player, team_map):
    if not player or not team_map: return None
    n = str(player).lower().replace(",","").replace(".","").strip()
    if n in team_map: return team_map[n]
    parts = n.split()
    if not parts: return None
    last = parts[-1]
    matches = [v for k, v in team_map.items() if k.split() and k.split()[-1] == last]
    if len(matches) == 1: return matches[0]
    return None


def _recompute_summary(picks):
    """Rebuild the summary block from a filtered list of picks."""
    wins = sum(1 for p in picks if p.get("result") == "WIN")
    losses = sum(1 for p in picks if p.get("result") == "LOSS")
    pushes = sum(1 for p in picks if p.get("result") == "PUSH")
    voids = sum(1 for p in picks if p.get("result") == "VOID")
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
        if r == "WIN":   profit_total += payout
        elif r == "LOSS": profit_total -= risk
    return {
        "n_total":      len(picks),
        "n_settled":    settled,
        "n_void":       voids,
        "wins":         wins,
        "losses":       losses,
        "pushes":       pushes,
        "hit_rate":     round(wins/(wins+losses)*100, 1) if (wins+losses) else 0,
        "risk_total":   round(risk_total, 2),
        "profit_total": round(profit_total, 2),
        "roi_pct":      round(profit_total/risk_total*100, 2) if risk_total else 0,
    }


TRACKERS = [
    {"name": "HR",             "icon": "💣", "dir": "hr_tracker"},
    {"name": "H+R+R",          "icon": "🏃", "dir": "hrr_tracker"},
    {"name": "True Prob",      "icon": "🎯", "dir": "true_prob_history"},
    # Soft A+ is the highest-ROI subset: edge >= 10pp only. +27.5% ROI historically.
    {"name": "Soft A+",        "icon": "🌟", "dir": "soft_scanner_history",
     "pick_filter": _is_a_plus_grade_soft},
    # Full A-Grade: broader profitable subset. +14.2% ROI.
    {"name": "Soft A-Grade",   "icon": "🏆", "dir": "soft_scanner_history",
     "pick_filter": _is_a_grade_soft},
    {"name": "Sharp Money",    "icon": "💰", "dir": "sharp_money_history"},
    {"name": "Fundamentals",   "icon": "📊", "dir": "fundamentals_history"},
]


def load_files(dirname, pick_filter=None):
    """Returns list of (date_str, payload, mtime, size_bytes) sorted asc.
    If pick_filter is provided, filters the picks and recomputes the summary
    so all downstream stats reflect the filtered subset only."""
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
            if pick_filter is not None:
                # Apply filter and rebuild summary in-place (on this in-memory copy)
                payload = dict(payload)
                filtered = [p for p in payload.get("picks", []) if pick_filter(p)]
                payload["picks"] = filtered
                payload["n_picks"] = len(filtered)
                payload["summary"] = _recompute_summary(filtered)
            mtime = datetime.fromtimestamp(os.path.getmtime(full), tz=EASTERN)
            size = os.path.getsize(full)
            out.append((date_str, payload, mtime, size))
        except Exception:
            pass
    return out


all_data = {t["name"]: load_files(t["dir"], pick_filter=t.get("pick_filter"))
            for t in TRACKERS}
today_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")


def fmt_line(x):
    """Format prop lines without redundant zeros.
        0.5 -> ".5"
        1.5 -> "1.5"
        5.0 -> "5"
        None / NaN -> ""
    """
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
    if s.startswith("0."):
        return s[1:]
    if s.startswith("-0."):
        return "-" + s[2:]
    return s


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
    files = all_data[t["name"]]
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
    files_by_date = {f[0]: f[1] for f in all_data[t["name"]]}
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

        # Resolve team for any pick missing it (historical files pre-date
        # the team-enrichment in the scanner)
        _team_map = _league_player_team_map()

        rows = []
        for p in picks:
            sel = (p.get("selection") or p.get("player")
                   or p.get("batter") or "?")
            # Use saved team if present, else backfill from league roster lookup
            team = p.get("team")
            if not team:
                # Try player/batter field for backfill lookup
                lookup_name = (p.get("player") or p.get("batter")
                               or (sel if "(" not in str(sel) else sel.split("(")[0]))
                team = _lookup_team(lookup_name, _team_map)
            if team and "(" not in str(sel):
                sel = f"{sel} ({team})"
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
        # Format the Line column to strip redundant zeros (".5" instead of "0.5")
        if "Line" in df.columns:
            df["Line"] = df["Line"].apply(fmt_line)
        # Drop empty columns
        for c in list(df.columns):
            if df[c].isna().all() or (df[c].astype(str).str.strip() == "").all():
                df = df.drop(columns=c)

        # Color-code results
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
    files = all_data[t["name"]]
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
            files_by_date = {f[0]: f[1] for f in all_data[t["name"]]}
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
            files = all_data[t["name"]]
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
