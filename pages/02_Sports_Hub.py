"""
🏟️ Sports Hub — Today's Lines
NFL · CFB · NHL · MLB odds at a glance.
"""
import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN = ZoneInfo("America/New_York")
_CTX    = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode    = ssl.CERT_NONE

SPORTS = {
    "🏈 NFL":  "americanfootball_nfl",
    "🎓 CFB":  "americanfootball_ncaaf",
    "🏒 NHL":  "icehockey_nhl",
    "⚾ MLB":  "baseball_mlb",
}

DISPLAY_BOOKS = ["pinnacle", "draftkings", "fanduel", "williamhill_us", "betrivers", "bovada"]
BOOK_LABEL = {
    "pinnacle":       "Pinnacle",
    "draftkings":     "DK",
    "fanduel":        "FD",
    "williamhill_us": "Caesars",
    "betrivers":      "BetRivers",
    "bovada":         "Bovada",
}

st.set_page_config(page_title="Sports Hub", page_icon="🏟️", layout="wide")
st.title("🏟️ Sports Hub — Today's Lines")
st.caption("Spreads · Totals · Moneylines across NFL, CFB, NHL, MLB. Today's games only.")

def _resolve_secret(name):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)

ODDS_KEY = _resolve_secret("THE_ODDS_API_KEY")
if not ODDS_KEY:
    st.error("Missing `THE_ODDS_API_KEY` in secrets.")
    st.stop()

def _get(url, timeout=20):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    return json.loads(resp.read()), resp.headers.get("x-requests-remaining", "?")

# ── Cache ─────────────────────────────────────────────────────────────────────
for k, v in [("hub_data", {}), ("hub_quota", "?"), ("hub_fetched", None)]:
    if k not in st.session_state:
        st.session_state[k] = v

now = datetime.now(tz=EASTERN)
needs = (
    st.session_state.hub_fetched is None
    or (now - st.session_state.hub_fetched).total_seconds() >= 300
)

hcol1, hcol2, hcol3 = st.columns([1, 2, 2])
with hcol1:
    force = st.button("🔄 Refresh", type="primary", use_container_width=True)
if force:
    needs = True

if needs:
    with st.spinner("Fetching today's odds…"):
        hub_data = {}
        quota    = "?"
        today    = now.date()
        for label, sport_key in SPORTS.items():
            try:
                data, quota = _get(
                    f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
                    f"?apiKey={ODDS_KEY}&regions=us,eu&markets=h2h,spreads,totals"
                    f"&oddsFormat=american&bookmakers="
                    + ",".join(DISPLAY_BOOKS)
                )
                today_games = []
                for ev in data:
                    ct = ev.get("commence_time", "")
                    try:
                        gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
                        if gt.date() != today:
                            continue
                        ev["_gt"] = gt
                    except Exception:
                        continue
                    today_games.append(ev)
                today_games.sort(key=lambda e: e.get("_gt", datetime.min.replace(tzinfo=EASTERN)))
                hub_data[label] = today_games
            except Exception as e:
                hub_data[label] = {"error": str(e)}
        st.session_state.hub_data    = hub_data
        st.session_state.hub_quota   = quota
        st.session_state.hub_fetched = now

with hcol2:
    if st.session_state.hub_fetched:
        st.caption(f"Updated: **{st.session_state.hub_fetched.strftime('%I:%M %p ET')}** • auto 5 min")
with hcol3:
    st.caption(f"API quota remaining: **{st.session_state.hub_quota}**")

st.markdown("---")

# ── Helpers ───────────────────────────────────────────────────────────────────
def _pr(p):
    if p is None:
        return "—"
    return f"{p:+d}"

def _sp(p):
    if p is None:
        return "—"
    s = f"{p:+.1f}" if isinstance(p, float) else f"{p:+d}"
    return s

def _get_market(bm, key):
    for m in bm.get("markets", []):
        if m.get("key") == key:
            return m
    return None

def _outcome(market, name):
    if not market:
        return None, None
    for o in market.get("outcomes", []):
        if o.get("name") == name:
            return o.get("price"), o.get("point")
    return None, None

def _row_bg(val, ref):
    """Highlight when this book's line differs from Pinnacle's line."""
    if val is None or ref is None:
        return ""
    return "background:#fef9c3;" if abs(val - ref) >= 0.5 else ""

def render_game(ev):
    home = ev.get("home_team", "?")
    away = ev.get("away_team", "?")
    gt   = ev.get("_gt")
    time_str = gt.strftime("%I:%M %p ET").lstrip("0") if gt else "?"

    bm_map = {bm["key"]: bm for bm in ev.get("bookmakers", [])}

    # Gather Pinnacle lines as reference
    pin_bm      = bm_map.get("pinnacle", {})
    pin_spread  = _get_market(pin_bm, "spreads")
    pin_total   = _get_market(pin_bm, "totals")
    pin_h2h     = _get_market(pin_bm, "h2h")

    _, pin_hsp   = _outcome(pin_spread, home)
    pin_ou, _    = _outcome(pin_total, "Over")
    pin_hml, _   = _outcome(pin_h2h,   home)

    # Header
    st.markdown(
        f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;'
        f'padding:10px 14px;margin-bottom:4px;">'
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
        f'<span style="font-size:15px;font-weight:700;color:#1e293b;">{away} <span style="color:#94a3b8;">@</span> {home}</span>'
        f'<span style="font-size:12px;color:#94a3b8;">{time_str}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Table header
    header = (
        '<table style="width:100%;border-collapse:collapse;margin-top:6px;">'
        '<tr style="border-bottom:1px solid #e2e8f0;">'
        '<th style="text-align:left;padding:3px 6px;font-size:11px;color:#94a3b8;font-weight:500;">Book</th>'
        '<th style="padding:3px 6px;font-size:11px;color:#94a3b8;font-weight:500;text-align:center;">'
        f'Spread ({away[:3].upper()})</th>'
        '<th style="padding:3px 6px;font-size:11px;color:#94a3b8;font-weight:500;text-align:center;">'
        f'Spread ({home[:3].upper()})</th>'
        '<th style="padding:3px 6px;font-size:11px;color:#94a3b8;font-weight:500;text-align:center;">O/U</th>'
        '<th style="padding:3px 6px;font-size:11px;color:#94a3b8;font-weight:500;text-align:center;">ML Away</th>'
        '<th style="padding:3px 6px;font-size:11px;color:#94a3b8;font-weight:500;text-align:center;">ML Home</th>'
        '</tr>'
    )

    rows = ""
    for bk in DISPLAY_BOOKS:
        bm = bm_map.get(bk)
        if not bm:
            continue
        label  = BOOK_LABEL.get(bk, bk)
        spread = _get_market(bm, "spreads")
        total  = _get_market(bm, "totals")
        h2h    = _get_market(bm, "h2h")

        a_sp_pr, a_sp_pt = _outcome(spread, away)
        h_sp_pr, h_sp_pt = _outcome(spread, home)
        ou_pr,   ou_pt   = _outcome(total,  "Over")
        a_ml,    _       = _outcome(h2h,    away)
        h_ml,    _       = _outcome(h2h,    home)

        is_pin   = bk == "pinnacle"
        row_bg   = "background:#f1f5f9;" if is_pin else ""
        pin_bold = "font-weight:700;" if is_pin else ""

        # Highlight spreads/totals that differ from Pinnacle
        asp_bg = _row_bg(a_sp_pt, (-pin_hsp if pin_hsp else None))
        hsp_bg = _row_bg(h_sp_pt, pin_hsp)
        ou_bg  = _row_bg(ou_pt,   pin_ou)

        asp_str = f"{_sp(a_sp_pt)} ({_pr(a_sp_pr)})" if a_sp_pt is not None else "—"
        hsp_str = f"{_sp(h_sp_pt)} ({_pr(h_sp_pr)})" if h_sp_pt is not None else "—"
        ou_str  = f"{ou_pt} ({_pr(ou_pr)})" if ou_pt is not None else "—"
        aml_str = _pr(a_ml)
        hml_str = _pr(h_ml)

        rows += (
            f'<tr style="{row_bg}">'
            f'<td style="padding:3px 6px;font-size:12px;color:#475569;{pin_bold}">{label}</td>'
            f'<td style="padding:3px 6px;font-size:12px;text-align:center;{asp_bg}">{asp_str}</td>'
            f'<td style="padding:3px 6px;font-size:12px;text-align:center;{hsp_bg}">{hsp_str}</td>'
            f'<td style="padding:3px 6px;font-size:12px;text-align:center;{ou_bg}">{ou_str}</td>'
            f'<td style="padding:3px 6px;font-size:12px;text-align:center;">{aml_str}</td>'
            f'<td style="padding:3px 6px;font-size:12px;text-align:center;">{hml_str}</td>'
            f'</tr>'
        )

    if not rows:
        rows = '<tr><td colspan="6" style="padding:6px;font-size:12px;color:#94a3b8;">No book data available</td></tr>'

    st.markdown(header + rows + "</table></div>", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_labels = list(SPORTS.keys())
tabs = st.tabs(tab_labels)

for tab, label in zip(tabs, tab_labels):
    with tab:
        data = st.session_state.hub_data.get(label)
        if data is None:
            st.info("Not loaded yet — hit Refresh.")
        elif isinstance(data, dict) and "error" in data:
            st.error(f"Fetch error: {data['error']}")
        elif not data:
            st.info(f"No games today for {label.split()[-1]}.")
        else:
            st.caption(
                f"🟡 Highlighted spread/total = line differs from Pinnacle by ≥ 0.5 pts"
            )
            for ev in data:
                render_game(ev)
            st.caption(f"{len(data)} game{'s' if len(data) != 1 else ''} today")
