"""
🏈🥅 TD & Goal Scanner
Sharp money model for anytime TD (NFL) and anytime goal scorer (NHL).
Detects Pinnacle price edges on Yes/No markets + team total movement.
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

SPORT_CONFIGS = {
    "nfl": {
        "key":        "americanfootball_nfl",
        "market":     "player_anytime_td",
        "yes_point":  None,   # Yes/No market — no point filter needed
        "tt_market":  "team_totals",
        "label":      "ANYTIME TD",
        "color":      "#dc2626",
        "followers": [
            "draftkings", "fanduel", "williamhill_us", "fanatics",
            "betrivers", "betonlineag", "espnbet", "mybookieag", "pointsbetus",
        ],
    },
    "nhl": {
        "key":        "icehockey_nhl",
        "market":     "player_goals",   # OVER 0.5 = anytime goal scorer
        "yes_point":  0.5,              # only collect Over 0.5 outcomes
        "tt_market":  None,             # team_totals not available for NHL
        "label":      "ANYTIME GOAL",
        "color":      "#1d4ed8",
        # BetMGM and Caesars (williamhill_us) excluded from all NHL models
        "followers": [
            "draftkings", "fanduel", "bovada", "betrivers",
        ],
    },
}

PINNACLE = "pinnacle"
BOOK_DISPLAY = {
    "pinnacle":       "Pinnacle",
    "draftkings":     "DraftKings",
    "fanduel":        "FanDuel",
    "williamhill_us": "Caesars",
    "fanatics":       "Fanatics",
    "betrivers":      "BetRivers",
    "betonlineag":    "BetOnline",
    "espnbet":        "ESPN Bet",
    "mybookieag":     "MyBookie",
    "pointsbetus":    "PointsBet",
    "bovada":         "Bovada",
}

# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="TD & Goal Scanner", page_icon="🏈", layout="wide")
st.title("🏈🥅 TD & Goal Scanner")
st.caption(
    "Pinnacle sharp anchor · Price edge on Yes/No scorer props · "
    "Team total movement confirms scoring environment · Refreshes every 10 min"
)

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

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode    = ssl.CERT_NONE

def _get(url, timeout=20):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    return json.loads(resp.read()), resp.headers.get("x-requests-remaining", "?")

# ── Helpers ───────────────────────────────────────────────────────────────────
def american_to_prob(price):
    if price is None:
        return None
    if price < 0:
        return abs(price) / (abs(price) + 100)
    return 100 / (price + 100)

def price_str(p):
    if p is None:
        return "N/A"
    return f"+{p}" if p > 0 else str(p)

def format_time(ct):
    gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
    h  = gt.strftime("%I").lstrip("0") or "12"
    return gt.strftime(f"{h}:%M %p")

# ── Fetch ─────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def fetch_data(sport_id):
    cfg        = SPORT_CONFIGS[sport_id]
    sport_key  = cfg["key"]
    market     = cfg["market"]
    followers  = cfg["followers"]
    tracked    = {PINNACLE} | set(followers)
    now_et     = datetime.now(tz=EASTERN)
    today_date = now_et.date()

    events, quota = _get(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/events"
        f"?apiKey={ODDS_KEY}"
    )

    games = []
    for ev in events:
        ct   = ev["commence_time"]
        home = ev["home_team"]
        away = ev["away_team"]

        try:
            gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
            if gt.date() != today_date:
                continue
            if gt <= now_et:
                games.append({
                    "game": f"{away} @ {home}", "away": away, "home": home,
                    "commence_time": ct, "started": True,
                    "players": {}, "team_totals": {},
                })
                continue
        except Exception:
            continue

        tt_market  = cfg["tt_market"]
        markets_param = market if tt_market is None else f"{market},{tt_market}"
        try:
            data, _ = _get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{ev['id']}/odds"
                f"?apiKey={ODDS_KEY}&regions=us,eu&markets={markets_param}&oddsFormat=american"
            )
        except Exception as e:
            st.warning(f"Skipped {away} @ {home}: {e}", icon="⚠️")
            continue

        yes_point   = cfg["yes_point"]
        players     = {}
        team_totals = {}

        for bm in data.get("bookmakers", []):
            bk = bm["key"]
            if bk not in tracked:
                continue
            for mkt in bm.get("markets", []):
                mkt_key = mkt.get("key", "")

                if mkt_key == market:
                    for out in mkt.get("outcomes", []):
                        player = (out.get("description") or out.get("name", "")).strip()
                        name   = out.get("name", "")
                        price  = out.get("price")
                        point  = out.get("point")
                        if not player or price is None:
                            continue
                        if yes_point is not None:
                            # Line market (e.g. player_goals): only collect Over at the target line
                            if name == "Over" and point == yes_point:
                                players.setdefault(player, {})[bk] = price
                        else:
                            # Yes/No market (e.g. player_anytime_td)
                            if name in ("Over", "Yes", player):
                                players.setdefault(player, {})[bk] = price

                elif tt_market and mkt_key == tt_market:
                    for out in mkt.get("outcomes", []):
                        team  = out.get("description", "").strip()
                        side  = out.get("name", "")
                        line  = out.get("point")
                        if not team or side != "Over" or line is None:
                            continue
                        team_totals.setdefault(team, {})[bk] = line

        games.append({
            "game": f"{away} @ {home}", "away": away, "home": home,
            "commence_time": ct, "started": False,
            "players": players, "team_totals": team_totals,
        })

    return games, quota, now_et.strftime("%I:%M %p").lstrip("0")

# ── Detection ─────────────────────────────────────────────────────────────────
def detect_team_total_signal(team_totals, team, followers):
    bk_data  = team_totals.get(team, {})
    pin_line = bk_data.get(PINNACLE)
    if pin_line is None:
        return False
    fol_lines = [bk_data[bk] for bk in followers if bk in bk_data]
    return bool(fol_lines) and pin_line > max(fol_lines)

def scan_signals(game, followers):
    game_total_signal = (
        detect_team_total_signal(game["team_totals"], game["home"], followers) or
        detect_team_total_signal(game["team_totals"], game["away"], followers)
    )

    plays = []
    for player, prices in game["players"].items():
        pin_price     = prices.get(PINNACLE)
        follower_data = [
            (bk, prices[bk], american_to_prob(prices[bk]))
            for bk in followers if bk in prices
        ]
        if len(follower_data) < 2:
            continue

        follower_probs = [fd[2] for fd in follower_data]
        avg_fol_prob   = sum(follower_probs) / len(follower_probs)
        best_bk, best_price, best_prob = max(follower_data, key=lambda x: x[1])
        worst_prob = min(follower_probs)

        signal_type = None
        edge        = 0.0
        stale_count = 0

        if pin_price is not None:
            pin_prob = american_to_prob(pin_price)
            pin_edge = pin_prob - avg_fol_prob
            if pin_edge >= 0.005:
                signal_type = "SHARP"
                edge        = pin_edge
                stale_count = sum(1 for fp in follower_probs if (pin_prob - fp) > 0.005)

        if signal_type is None:
            discrepancy = best_prob - worst_prob
            if discrepancy >= 0.015:
                signal_type = "SPLIT"
                edge        = discrepancy
                stale_count = sum(1 for fp in follower_probs if (best_prob - fp) > 0.005)

        if signal_type is None:
            continue

        plays.append({
            "player":        player,
            "game":          game["game"],
            "time":          format_time(game["commence_time"]),
            "commence_time": game["commence_time"],
            "pin_price":     pin_price,
            "avg_fol_prob":  avg_fol_prob,
            "edge":          edge,
            "signal_type":   signal_type,
            "stale_count":   stale_count,
            "best_book":     best_bk,
            "best_price":    best_price,
            "team_signal":   game_total_signal,
        })

    return plays

def get_confidence(play):
    score    = play["edge"] + (0.01 if play["team_signal"] else 0)
    is_sharp = play.get("signal_type") == "SHARP"
    if is_sharp or score >= 0.04:
        return "HIGH", "#dc2626", "#fef2f2"
    elif score >= 0.025:
        return "MED", "#d97706", "#fffbeb"
    return "LOW", "#6b7280", "#f3f4f6"

def render_card(play, market_label, market_color, best_bet=False):
    conf_label, conf_color, conf_bg = get_confidence(play)
    edge_pct  = round(play["edge"] * 100, 1)
    fol_pct   = round(play["avg_fol_prob"] * 100, 1)
    book_name = BOOK_DISPLAY.get(play["best_book"], play["best_book"])

    border_color = "#f97316" if best_bet else "#e5e7eb"
    bg_color     = "#fff7ed" if best_bet else "#f9fafb"
    star         = "⭐ " if best_bet else ""
    name_size    = "16px" if best_bet else "14px"
    name_weight  = "800" if best_bet else "700"
    border_width = "2px" if best_bet else "1.5px"

    team_badge = ""
    if play["team_signal"]:
        team_badge = (
            ' <span style="background:#dcfce7;color:#16a34a;border:1px solid #86efac;'
            'font-size:10px;font-weight:600;padding:1px 6px;border-radius:3px;">📈 team total up</span>'
        )
    juiced = ""
    if play["best_price"] < -150:
        juiced = ' <span style="color:#dc2626;font-size:10px;font-weight:600;">⚠️ juiced</span>'

    sig_label = "📌 Pinnacle sharp" if play.get("signal_type") == "SHARP" else "📊 Book split"
    pin_line  = (f' · PIN: {round(american_to_prob(play["pin_price"]) * 100, 1)}%'
                 if play.get("pin_price") is not None else "")

    st.markdown(
        f'<div style="background:{bg_color};border:{border_width} solid {border_color};'
        f'border-radius:10px;padding:12px 16px;margin-bottom:10px;">'
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">'
        f'<span style="font-size:{name_size};font-weight:{name_weight};color:#111827;">'
        f'{star}{play["player"]}</span>'
        f'<span style="font-size:11px;color:#9ca3af;">{play["time"]} ET</span></div>'
        f'<div style="font-size:12px;color:#6b7280;margin-bottom:4px;">{play["game"]}</div>'
        f'<div style="display:flex;align-items:baseline;gap:10px;margin-bottom:3px;">'
        f'<span style="font-size:18px;font-weight:800;color:{market_color};">{market_label}</span>'
        f'<span style="font-size:13px;color:#374151;">@ <strong>{book_name}</strong> {price_str(play["best_price"])}</span>'
        f'{juiced}</div>'
        f'<div style="font-size:11px;color:#6b7280;margin-bottom:4px;">'
        f'{sig_label} · Avg: {fol_pct}%{pin_line} · <strong>Edge: +{edge_pct}%</strong></div>'
        f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">'
        f'<span style="background:{conf_bg};color:{conf_color};border:1px solid {conf_color};'
        f'font-size:10px;font-weight:700;padding:1px 7px;border-radius:3px;">{conf_label}</span>'
        f'<span style="font-size:11px;color:#6b7280;">{play["stale_count"]} '
        f'book{"s" if play["stale_count"] != 1 else ""} stale</span>'
        f'{team_badge}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

def render_sport_tab(sport_id):
    cfg = SPORT_CONFIGS[sport_id]

    col_hdr, col_btn = st.columns([5, 1])
    with col_btn:
        if st.button("🔄 Refresh", key=f"refresh_{sport_id}", use_container_width=True):
            st.cache_data.clear()

    with st.spinner(f"Fetching today's {sport_id.upper()} games and props..."):
        try:
            games, quota, fetch_time = fetch_data(sport_id)
        except Exception as e:
            st.error(f"API error: {e}")
            return

    st.caption(f"Last fetch: {fetch_time} ET · API quota remaining: {quota}")

    today_games   = [g for g in games if not g["started"]]
    started_games = [g for g in games if g["started"]]

    if not games:
        sport_name = "NFL" if sport_id == "nfl" else "NHL"
        days       = "Thu / Sun / Mon" if sport_id == "nfl" else "daily Oct–Apr"
        st.info(f"No {sport_name} games scheduled for today ({days}).")
        return

    all_plays = []
    for game in today_games:
        all_plays.extend(scan_signals(game, cfg["followers"]))

    for play in all_plays:
        play["score"] = play["edge"] + (0.01 if play["team_signal"] else 0)
    all_plays.sort(key=lambda x: -x["score"])

    if started_games:
        st.warning(
            f"**{len(started_games)} game{'s' if len(started_games) > 1 else ''} already started** — "
            f"{', '.join(g['game'] for g in started_games)}",
            icon="⏱️"
        )

    if not all_plays:
        if today_games:
            st.warning(
                "No sharp signals detected yet. "
                "Props typically post 2–3 hours before game time — check back soon.",
                icon="🔍"
            )
        st.markdown("**Today's games:**")
        for g in today_games:
            st.write(f"• {g['game']} — {format_time(g['commence_time'])} ET")
        return

    st.markdown(f"### {len(all_plays)} signal{'s' if len(all_plays) != 1 else ''} found")

    with st.expander("🔍 Debug — raw player counts per game", expanded=False):
        for game in today_games:
            pin_count = sum(1 for p in game["players"].values() if PINNACLE in p)
            fol_count = sum(1 for p in game["players"].values() if any(bk in p for bk in cfg["followers"]))
            st.write(f"**{game['game']}** — {len(game['players'])} players · {pin_count} with Pinnacle · {fol_count} with follower data")

    best_bets = all_plays[:2]
    rest      = all_plays[2:]

    st.markdown("---")
    st.markdown("## ⭐ Best Bets")
    st.caption("Top 2 plays by combined edge score (price gap + team scoring environment)")
    for play in best_bets:
        render_card(play, cfg["label"], cfg["color"], best_bet=True)

    if rest:
        st.markdown("---")
        st.markdown("## 📋 All Signals")
        for play in rest:
            render_card(play, cfg["label"], cfg["color"], best_bet=False)

    nhl_note = " · BetMGM and Caesars excluded from all NHL models" if sport_id == "nhl" else ""
    st.markdown("---")
    st.caption(
        f"Signal: Pinnacle implied prob vs follower avg · "
        f"Books: Pinnacle + {len(cfg['followers'])} followers{nhl_note}"
    )

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_nfl, tab_nhl = st.tabs(["🏈 NFL", "🥅 NHL"])

with tab_nfl:
    render_sport_tab("nfl")

with tab_nhl:
    render_sport_tab("nhl")
