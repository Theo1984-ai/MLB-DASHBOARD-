"""
Bankroll Tracker & Kelly Bet Sizer — Alt Under parlay strategy.

Features:
  • Kelly Bet Sizer  — enter bankroll + leg count → Full / Half / Quarter Kelly stake
  • Result Logger    — log each day's parlay result (persisted to data/bankroll_log.json)
  • Growth Chart     — actual bankroll history + 90-day median/percentile projection
  • Performance Stats — hit rate vs model, ROI, streak, time-to-target
"""

import os, sys, json, math
from datetime import date, datetime, timedelta

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from models.kelly import kelly_fraction

st.set_page_config(page_title="Bankroll Tracker", page_icon="💰", layout="wide")
st.title("💰 Bankroll Tracker")
st.caption(
    "Kelly bet sizing + daily result logging + growth projection "
    "for the Alt Under parlay strategy."
)

# ── Constants ──────────────────────────────────────────────────────────────────
TRUE_P_PER_LEG  = 0.848   # 14-day backtest hit rate per leg
AVG_LEG_DECIMAL = 1.466   # avg per-leg payout (≈ -215 odds)
LOG_FILE        = os.path.join(ROOT, "data", "bankroll_log.json")


# ── Helpers ───────────────────────────────────────────────────────────────────
def load_log() -> list:
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_log(log: list) -> None:
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def parlay_decimal(n_legs: int) -> float:
    return AVG_LEG_DECIMAL ** n_legs


def parlay_american(dec: float) -> int:
    if dec >= 2.0:
        return int((dec - 1) * 100)
    return int(-100 / (dec - 1))


def kelly_stats(n_legs: int, bankroll: float):
    """Return (p, dec, f_full, f_half, f_quarter) for an N-leg parlay."""
    p   = TRUE_P_PER_LEG ** n_legs
    dec = parlay_decimal(n_legs)
    f   = kelly_fraction(p, dec)
    return p, dec, f, f * 0.5, f * 0.25


# ── Load log ──────────────────────────────────────────────────────────────────
log = load_log()
default_bankroll = float(log[-1]["bankroll_after"]) if log else 500.0


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Kelly Bet Sizer
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("## 🎯 Kelly Bet Sizer")

sz_c1, sz_c2 = st.columns([1, 2])

with sz_c1:
    bankroll = st.number_input(
        "Current bankroll ($)",
        min_value=10.0, max_value=1_000_000.0,
        value=default_bankroll,
        step=50.0, format="%.2f",
    )
    n_legs = st.slider("Parlay legs today", min_value=3, max_value=9, value=6,
                       help="Match this to the leg count you plan to play today")
    st.caption(
        "**Stage guide:**  \n"
        "< $500 → 3–4 legs  \n"
        "$500–$1,500 → 4–5 legs  \n"
        "$1,500–$5,000 → 5–6 legs  \n"
        "$5,000+ → 6–8 legs"
    )

with sz_c2:
    p, dec, f_full, f_half, f_quarter = kelly_stats(n_legs, bankroll)
    am = parlay_american(dec)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Win Rate",        f"{p * 100:.1f}%")
    m2.metric("Est Payout",      f"{am:+,}")
    m3.metric("Full Kelly %",    f"{f_full * 100:.1f}%")
    m4.metric("Half Kelly ✅",   f"{f_half * 100:.1f}%",
              delta="recommended", delta_color="normal")

    sizing_rows = []
    for label, frac in [
        ("Full Kelly",                   f_full),
        ("Half Kelly ✅ (recommended)",  f_half),
        ("Quarter Kelly",                f_quarter),
        ("Flat 2% (conservative)",       0.02),
        ("Flat 1% (very safe)",          0.01),
    ]:
        stake         = bankroll * frac
        profit_if_win = stake * (dec - 1)
        exp_profit    = p * profit_if_win - (1 - p) * stake
        sizing_rows.append({
            "Strategy":        label,
            "Bet Size":        round(stake, 2),
            "% of Bankroll":   round(frac * 100, 2),
            "Profit If Win":   round(profit_if_win, 2),
            "Expected Profit": round(exp_profit, 2),
        })

    st.dataframe(
        pd.DataFrame(sizing_rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Bet Size":        st.column_config.NumberColumn(format="$%.2f"),
            "% of Bankroll":   st.column_config.NumberColumn(format="%.2f%%"),
            "Profit If Win":   st.column_config.NumberColumn(format="$%.2f"),
            "Expected Profit": st.column_config.NumberColumn(format="$%+.2f"),
        },
        height=215,
    )
    st.caption(
        f"Half Kelly recommended: gives ~75% of max growth with much lower variance. "
        f"At {n_legs} legs your win rate is **{p*100:.1f}%** "
        f"and est payout **{am:+,}** per $100 wagered."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Log a Result
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.markdown("## 📝 Log Today's Result")

with st.form("log_form", clear_on_submit=True):
    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        log_date = st.date_input("Date", value=date.today())
    with fc2:
        log_legs = st.number_input("Legs played", min_value=2, max_value=10, value=n_legs)
    with fc3:
        log_stake = st.number_input(
            "Stake ($)", min_value=1.0,
            value=round(bankroll * f_half, 2), format="%.2f",
        )
    with fc4:
        log_result = st.radio("Result", ["Win", "Loss"], horizontal=True)

    fc5, fc6 = st.columns(2)
    with fc5:
        log_payout_odds = st.number_input(
            "Actual payout (american, e.g. +1250) — only needed for Win",
            min_value=100, max_value=1_000_000,
            value=max(100, parlay_american(parlay_decimal(log_legs))),
            help="Enter the real payout shown on your bet slip",
        )
    with fc6:
        log_note = st.text_input(
            "Note (optional)",
            placeholder="e.g. KC@CWS, SD@MIL, COL@PIT",
        )

    submitted = st.form_submit_button(
        "💾 Log Result", type="primary", use_container_width=True
    )

if submitted:
    prev_br = float(log[-1]["bankroll_after"]) if log else bankroll
    if log_result == "Win":
        dec_slip = 1 + log_payout_odds / 100.0
        profit   = round(log_stake * (dec_slip - 1), 2)
    else:
        profit = round(-log_stake, 2)

    new_br = round(prev_br + profit, 2)
    entry  = {
        "date":            log_date.isoformat(),
        "legs":            int(log_legs),
        "stake":           round(log_stake, 2),
        "result":          log_result.lower(),
        "payout_odds":     log_payout_odds if log_result == "Win" else None,
        "profit":          profit,
        "bankroll_before": prev_br,
        "bankroll_after":  new_br,
        "note":            log_note,
    }
    log.append(entry)
    save_log(log)
    pnl_str = f"+${profit:,.2f}" if profit >= 0 else f"-${abs(profit):,.2f}"
    st.success(
        f"✅ Logged! Bankroll: **${prev_br:,.2f}** → **${new_br:,.2f}** ({pnl_str})"
    )
    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Growth Chart
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.markdown("## 📈 Bankroll Growth Chart")

if not log:
    st.info(
        "No results logged yet. Log your first bet above to see your growth chart. "
        "The projection lines will appear immediately."
    )
    # Still show a projection from the current bankroll input
    log_for_proj = []
    cur_br_proj  = bankroll
    start_date   = date.today().isoformat()
else:
    log_for_proj = log
    cur_br_proj  = log[-1]["bankroll_after"]
    start_date   = log[0]["date"]

# 90-day log-normal projection
PROJ_DAYS = 90
f_proj    = f_half
b_proj    = dec - 1
mu_day    = (p * math.log(1 + f_proj * b_proj)
             + (1 - p) * math.log(max(1e-12, 1 - f_proj)))
var_day   = (p * (1 - p)
             * (math.log(1 + f_proj * b_proj)
                - math.log(max(1e-12, 1 - f_proj))) ** 2)

last_proj_date = datetime.fromisoformat(
    log_for_proj[-1]["date"] if log_for_proj else date.today().isoformat()
).date()
proj_dates  = [(last_proj_date + timedelta(days=i)).isoformat() for i in range(PROJ_DAYS + 1)]
med_proj    = [cur_br_proj * math.exp(mu_day * i) for i in range(PROJ_DAYS + 1)]
upper_proj  = [cur_br_proj * math.exp(mu_day * i + 0.674 * math.sqrt(max(0, var_day * i)))
               for i in range(PROJ_DAYS + 1)]
lower_proj  = [cur_br_proj * math.exp(mu_day * i - 0.674 * math.sqrt(max(0, var_day * i)))
               for i in range(PROJ_DAYS + 1)]

fig = go.Figure()

# 25th–75th percentile band
fig.add_trace(go.Scatter(
    x=proj_dates + proj_dates[::-1],
    y=upper_proj + lower_proj[::-1],
    fill="toself",
    fillcolor="rgba(34,197,94,0.10)",
    line=dict(color="rgba(255,255,255,0)"),
    name="25th–75th %ile",
    hoverinfo="skip",
))

# Median projection line
fig.add_trace(go.Scatter(
    x=proj_dates, y=med_proj,
    mode="lines",
    line=dict(color="#22c55e", width=2, dash="dash"),
    name=f"{n_legs}-leg Half-Kelly projection (median)",
))

# Actual history
if log_for_proj:
    hist_dates = (
        [(datetime.fromisoformat(log_for_proj[0]["date"]) - timedelta(days=1)).date().isoformat()]
        + [e["date"] for e in log_for_proj]
    )
    hist_brs = [log_for_proj[0]["bankroll_before"]] + [e["bankroll_after"] for e in log_for_proj]
    fig.add_trace(go.Scatter(
        x=hist_dates, y=hist_brs,
        mode="lines+markers",
        line=dict(color="#60a5fa", width=3),
        marker=dict(
            size=8,
            color=["#4ade80" if e["result"] == "win" else "#f87171"
                   for e in [{"result": "start"}] + log_for_proj],
        ),
        name="Actual bankroll",
        hovertemplate="%{x}<br><b>$%{y:,.2f}</b><extra></extra>",
    ))

# Target dotted lines
max_proj = max(upper_proj[-1], cur_br_proj)
for target, label in [(2500, "$2,500"), (5000, "$5k"), (10000, "$10k"),
                      (25000, "$25k"), (50000, "$50k")]:
    if cur_br_proj * 0.5 < target < max_proj * 1.5:
        fig.add_hline(
            y=target,
            line_dash="dot",
            line_color="rgba(148,163,184,0.4)",
            annotation_text=label,
            annotation_position="right",
            annotation_font_color="#94a3b8",
        )

fig.update_layout(
    title=f"{n_legs}-Leg Alt Under Parlay — Half-Kelly Growth Projection (90 days)",
    xaxis_title="Date",
    yaxis_title="Bankroll ($)",
    yaxis_tickprefix="$",
    yaxis_tickformat=",.0f",
    legend=dict(x=0.01, y=0.99, bgcolor="rgba(15,23,42,0.7)",
                bordercolor="rgba(255,255,255,0.1)", borderwidth=1),
    height=440,
    plot_bgcolor="#0f172a",
    paper_bgcolor="#0f172a",
    font=dict(color="#e2e8f0"),
    xaxis=dict(gridcolor="#1e293b", showgrid=True),
    yaxis=dict(gridcolor="#1e293b", showgrid=True),
    hovermode="x unified",
)
st.plotly_chart(fig, use_container_width=True)

# ── Time-to-target row ────────────────────────────────────────────────────────
st.markdown("#### ⏱ Median Days to Target (at Half-Kelly, current leg count)")
tt_cols = st.columns(5)
for i, target in enumerate([2_500, 5_000, 10_000, 25_000, 50_000]):
    if cur_br_proj >= target:
        tt_cols[i].metric(f"${target:,}", "✅ Reached!")
    elif mu_day <= 0:
        tt_cols[i].metric(f"${target:,}", "∞ (no edge)")
    else:
        days_needed = math.ceil(math.log(target / cur_br_proj) / mu_day)
        tt_cols[i].metric(f"${target:,}", f"~{days_needed} days")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Performance Stats + Bet Log
# ═══════════════════════════════════════════════════════════════════════════════
if log:
    st.markdown("---")
    st.markdown("## 📊 Performance Stats")

    total_bets    = len(log)
    wins          = sum(1 for e in log if e["result"] == "win")
    losses        = total_bets - wins
    actual_hr     = wins / total_bets if total_bets else 0
    total_wagered = sum(e["stake"] for e in log)
    total_pnl     = sum(e["profit"] for e in log)
    roi           = total_pnl / total_wagered * 100 if total_wagered else 0
    avg_legs      = sum(e["legs"] for e in log) / total_bets if total_bets else n_legs
    model_hr      = TRUE_P_PER_LEG ** avg_legs

    # Current streak
    streak_n    = 1
    streak_type = log[-1]["result"] if log else "win"
    for e in reversed(log[:-1]):
        if e["result"] == streak_type:
            streak_n += 1
        else:
            break
    streak_str  = f"{streak_n}× {'✅' if streak_type == 'win' else '❌'}"

    # Best / worst single day
    best_pnl  = max(e["profit"] for e in log)
    worst_pnl = min(e["profit"] for e in log)

    st_cols = st.columns(8)
    st_cols[0].metric("Total Bets",    total_bets)
    st_cols[1].metric("Wins / Losses", f"{wins}W / {losses}L")
    st_cols[2].metric("Actual Hit Rate",
                      f"{actual_hr * 100:.1f}%",
                      delta=f"{(actual_hr - model_hr) * 100:+.1f}pp vs model")
    st_cols[3].metric("Net P&L",       f"${total_pnl:+,.2f}")
    st_cols[4].metric("ROI",           f"{roi:+.1f}%")
    st_cols[5].metric("Current Streak", streak_str)
    st_cols[6].metric("Best Day",      f"+${best_pnl:,.2f}")
    st_cols[7].metric("Worst Day",     f"${worst_pnl:+,.2f}")

    # ── Bet log table ─────────────────────────────────────────────────────────
    with st.expander("📋 Full Bet Log", expanded=True):
        log_df = pd.DataFrame(log)[
            ["date", "legs", "stake", "result", "payout_odds",
             "profit", "bankroll_after", "note"]
        ].copy()
        log_df.columns = [
            "Date", "Legs", "Stake ($)", "Result",
            "Odds", "Profit ($)", "Bankroll After ($)", "Note"
        ]
        log_df["Result"] = log_df["Result"].str.upper()
        log_df = log_df.iloc[::-1].reset_index(drop=True)   # newest first

        st.dataframe(
            log_df,
            use_container_width=True,
            hide_index=True,
            height=min(38 * len(log_df) + 42, 420),
            column_config={
                "Stake ($)":          st.column_config.NumberColumn(format="$%.2f"),
                "Profit ($)":         st.column_config.NumberColumn(format="$%+.2f"),
                "Bankroll After ($)": st.column_config.NumberColumn(format="$,.2f"),
                "Odds":               st.column_config.NumberColumn(format="%+d"),
            },
        )

        # Delete last entry
        if st.button("🗑️ Delete last entry", type="secondary"):
            log.pop()
            save_log(log)
            st.rerun()
