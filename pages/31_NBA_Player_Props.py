"""
🏅 Player Props — Sharp Movement Tracker
NBA · NFL · NHL · MLB props with Pinnacle as sharp anchor.
"""
import json
import os
import ssl
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN         = ZoneInfo("America/New_York")
MIN_REFRESH_SEC = 600
CACHE_DIR       = os.path.join(ROOT, "props_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── Sport configs ─────────────────────────────────────────────────────────────
SPORTS = {
    "🏀 NBA": {
        "key":      "basketball_nba",
        "markets":  [
            "player_points", "player_rebounds", "player_assists",
            "player_threes", "player_steals", "player_blocks", "player_turnovers",
        ],
        "display": {
            "player_points":    ("🏀", "Points"),
            "player_rebounds":  ("💪", "Rebounds"),
            "player_assists":   ("🎯", "Assists"),
            "player_threes":    ("3️⃣", "3-Pointers"),
            "player_steals":    ("🤚", "Steals"),
            "player_blocks":    ("🛡️", "Blocks"),
            "player_turnovers": ("🔄", "Turnovers"),
        },
    },
    "🏈 NFL": {
        "key":      "americanfootball_nfl",
        "markets":  [
            "player_pass_tds", "player_pass_yds", "player_rush_yds",
            "player_reception_yds", "player_receptions", "player_anytime_td",
        ],
        "display": {
            "player_pass_tds":      ("🎯", "Pass TDs"),
            "player_pass_yds":      ("📏", "Pass Yards"),
            "player_rush_yds":      ("🏃", "Rush Yards"),
            "player_reception_yds": ("📡", "Rec Yards"),
            "player_receptions":    ("🙌", "Receptions"),
            "player_anytime_td":    ("🏈", "Anytime TD"),
        },
    },
    "🏒 NHL": {
        "key":      "icehockey_nhl",
        "markets":  [
            "player_points", "player_goals", "player_assists",
            "player_shots_on_goal", "player_blocked_shots",
        ],
        "display": {
            "player_points":        ("⭐", "Points"),
            "player_goals":         ("🥅", "Goals"),
            "player_assists":       ("🎯", "Assists"),
            "player_shots_on_goal": ("🚀", "Shots on Goal"),
            "player_blocked_shots": ("🛡️", "Blocked Shots"),
        },
    },
    "🎓 CFB": {
        "key":      "americanfootball_ncaaf",
        "markets":  [
            "player_anytime_td", "player_pass_tds", "player_pass_yds",
            "player_rush_yds", "player_reception_yds", "player_receptions",
        ],
        "display": {
            "player_anytime_td":    ("🏈", "Anytime TD"),
            "player_pass_tds":      ("🎯", "Pass TDs"),
            "player_pass_yds":      ("📏", "Pass Yards"),
            "player_rush_yds":      ("🏃", "Rush Yards"),
            "player_reception_yds": ("📡", "Rec Yards"),
            "player_receptions":    ("🙌", "Receptions"),
        },
    },
    "⚾ MLB": {
        "key":      "baseball_mlb",
        "markets":  [
            "batter_home_runs", "batter_hits", "batter_rbis",
            "batter_strikeouts", "pitcher_strikeouts", "pitcher_hits_allowed",
        ],
        "display": {
            "batter_home_runs":     ("💣", "Home Runs"),
            "batter_hits":          ("🎯", "Hits"),
            "batter_rbis":          ("🏃", "RBIs"),
            "batter_strikeouts":    ("🌀", "Batter Ks"),
            "pitcher_strikeouts":   ("⚡", "Pitcher Ks"),
            "pitcher_hits_allowed": ("📊", "Hits Allowed"),
        },
    },
}

FOLLOWER_BOOKS = [
    "draftkings", "fanduel", "williamhill_us", "fanatics",
    "betrivers", "betonlineag", "espnbet",
]
TRACKED_BOOKS = {"pinnacle"} | set(FOLLOWER_BOOKS)
BOOK_DISPLAY = {
    "pinnacle":       "Pinnacle",
    "williamhill_us": "Caesars",
    "fanatics":       "Fanatics",
    "draftkings":     "DraftKings",
    "fanduel":        "FanDuel",
    "bovada":         "Bovada",
    "betrivers":      "BetRivers",
    "betonlineag":    "BetOnline",
    "espnbet":        "ESPN Bet",
}

LEVEL_CSS = {
    "alarm":  "background:#7f1d1d;color:#fecaca;border:1px solid #ef4444;",
    "red":    "background:#fef2f2;color:#7f1d1d;border:1px solid #fca5a5;",
    "orange": "background:#fff7ed;color:#7c2d12;border:1px solid #fdba74;",
    "yellow": "background:#fefce8;color:#713f12;border:1px solid #fde047;",
    "blue":   "background:#eff6ff;color:#1e3a5f;border:1px solid #93c5fd;",
}
LEVEL_ICON = {"alarm": "🚨", "red": "🔴", "orange": "🟠", "yellow": "🟡", "blue": "🔵"}

# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Player Props", page_icon="🏅", layout="wide")
st.title("🏅 Player Props — Sharp Movement Tracker")
st.caption(
    "Pinnacle as sharp anchor · Flags line splits and steam sequences · "
    "NBA · NFL · NHL · MLB · Refreshes every 10 min"
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

# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_sport(sport_key, markets):
    now_et     = datetime.now(tz=EASTERN)
    today_date = now_et.date()
    markets_param = ",".join(markets)

    events, quota = _get(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/events"
        f"?apiKey={ODDS_KEY}"
    )

    games = []
    for ev in events:
        eid  = ev["id"]
        home = ev["home_team"]
        away = ev["away_team"]
        ct   = ev["commence_time"]

        try:
            gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
            if gt.date() != today_date:
                continue
            if gt <= now_et:
                games.append({
                    "game": f"{away} @ {home}", "away": away, "home": home,
                    "commence_time": ct, "started": True, "players": {},
                })
                continue
        except Exception:
            continue

        try:
            data, _ = _get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{eid}/odds"
                f"?apiKey={ODDS_KEY}&regions=us,eu&markets={markets_param}&oddsFormat=american"
            )
        except Exception as e:
            st.warning(f"Skipped {away} @ {home}: {e}", icon="⚠️")
            continue

        players = {}
        for bm in data.get("bookmakers", []):
            bk = bm["key"]
            if bk not in TRACKED_BOOKS:
                continue
            for mkt in bm.get("markets", []):
                mkt_key = mkt.get("key")
                if mkt_key not in markets:
                    continue
                for out in mkt.get("outcomes", []):
                    player = out.get("description", "").strip()
                    name   = out.get("name", "")
                    price  = out.get("price")
                    point  = out.get("point")
                    if not player:
                        continue
                    players.setdefault(player, {}).setdefault(mkt_key, {}).setdefault(bk, {})
                    if name in ("Over", "Yes"):
                        # Yes/No markets (e.g. anytime scorer) have no point; use 0.5 as virtual line
                        players[player][mkt_key][bk]["line"] = point if point is not None else 0.5
                        players[player][mkt_key][bk]["over"] = price
                    elif name in ("Under", "No"):
                        players[player][mkt_key][bk]["under"] = price

        if not players:
            continue

        games.append({
            "game": f"{away} @ {home}", "away": away, "home": home,
            "commence_time": ct, "started": False, "players": players,
        })

    snap = {
        "captured_at": now_et.isoformat(),
        "quota":       quota,
        "games":       games,
    }
    today = now_et.strftime("%Y-%m-%d")
    path  = os.path.join(CACHE_DIR, f"{sport_key}_{today}.json")
    existing = []
    if os.path.exists(path):
        try:
            existing = json.load(open(path))
        except Exception:
            existing = []
    existing.append(snap)
    with open(path, "w") as f:
        json.dump(existing, f)
    return snap, quota


def load_history(sport_key, n=15):
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    path  = os.path.join(CACHE_DIR, f"{sport_key}_{today}.json")
    if not os.path.exists(path):
        return []
    try:
        return json.load(open(path))[-n:]
    except Exception:
        return []

# ── Signals ───────────────────────────────────────────────────────────────────
def _get_prop(snap, game_name, player, market):
    for g in snap.get("games", []):
        if g["game"] == game_name:
            return g.get("players", {}).get(player, {}).get(market, {})
    return {}


def detect_signals(game_name, player, market, history, current):
    cur = _get_prop(current, game_name, player, market)
    if not cur:
        return []
    signals  = []
    timeline = [h for h in [_get_prop(s, game_name, player, market) for s in history] if h] + [cur]

    if len(timeline) >= 2:
        prev     = timeline[-2]
        pin_prev = prev.get("pinnacle", {}).get("over")
        pin_curr = cur.get("pinnacle", {}).get("over")
        if pin_prev is not None and pin_curr is not None and pin_curr < pin_prev:
            signals.append({"type": "PINNACLE DROP", "level": "yellow",
                             "detail": f"Pinnacle over: {pin_prev:+d} → {pin_curr:+d}"})
        followers = []
        for bk in FOLLOWER_BOOKS:
            bk_prev = prev.get(bk, {}).get("over")
            bk_curr = cur.get(bk, {}).get("over")
            if bk_prev is not None and bk_curr is not None and bk_curr < bk_prev:
                followers.append(bk)
                signals.append({"type": "FOLLOWER", "level": "orange",
                                 "detail": f"{BOOK_DISPLAY.get(bk,bk)}: {bk_prev:+d} → {bk_curr:+d}"})
        for bk, v_cur in cur.items():
            v_prev = prev.get(bk, {})
            o_p, o_c = v_prev.get("over"), v_cur.get("over")
            if o_p is not None and o_c is not None:
                if (o_p > 0 and o_c < 0) or (o_p < 0 and o_c > 0):
                    signals.append({"type": "CROSSED ZERO", "level": "red",
                                     "detail": f"{BOOK_DISPLAY.get(bk,bk)}: {o_p:+d} → {o_c:+d}"})
        prev_lines = [v.get("line") for v in prev.values() if v.get("line") is not None]
        cur_lines  = [v.get("line") for v in cur.values()  if v.get("line") is not None]
        if prev_lines and cur_lines:
            pm = sorted(prev_lines)[len(prev_lines)//2]
            cm = sorted(cur_lines)[len(cur_lines)//2]
            if cm < pm:
                signals.append({"type": "LINE DROP", "level": "red",
                                 "detail": f"Line: {pm} → {cm}"})
        types = {s["type"] for s in signals}
        if "PINNACLE DROP" in types and followers and ("CROSSED ZERO" in types or "LINE DROP" in types):
            signals.insert(0, {"type": "FULL SEQUENCE", "level": "alarm",
                                "detail": "All 4 steps confirmed — BET NOW"})
        elif "PINNACLE DROP" in types and followers:
            signals.insert(0, {"type": "SEQUENCE BUILDING", "level": "red",
                                "detail": "Steps 1–2 confirmed"})

    all_lines = [v.get("line") for v in cur.values() if v.get("line") is not None]
    if len(set(all_lines)) > 1:
        signals.append({"type": "LINE SPLIT", "level": "blue",
                         "detail": f"Books disagree: {sorted(set(all_lines))}"})
    return signals


def streak_info(game_name, player, market, history, current):
    prices = []
    for s in [s for s in history if _get_prop(s, game_name, player, market)] + [current]:
        p = _get_prop(s, game_name, player, market).get("pinnacle", {}).get("over")
        if p is not None:
            prices.append(p)
    if len(prices) < 2:
        return None, 0
    direction, length = None, 1
    for i in range(len(prices)-1, 0, -1):
        d = prices[i] - prices[i-1]
        if d == 0:
            continue
        cur_d = "down" if d < 0 else "up"
        if direction is None:
            direction, length = cur_d, 1
        elif cur_d == direction:
            length += 1
        else:
            break
    return direction, length

# ── Render one sport tab ──────────────────────────────────────────────────────
def _price_str(p):
    return f"{p:+d}" if p is not None else "—"


def render_sport_tab(sport_label, sport_cfg):
    sport_key  = sport_cfg["key"]
    markets    = sport_cfg["markets"]
    mk_display = sport_cfg["display"]

    sk    = f"props_{sport_key}_snap"
    sk_q  = f"props_{sport_key}_quota"
    sk_ft = f"props_{sport_key}_fetched"
    for k, v in [(sk, None), (sk_q, "?"), (sk_ft, None)]:
        if k not in st.session_state:
            st.session_state[k] = v

    now   = datetime.now(tz=EASTERN)
    needs = (
        st.session_state[sk] is None
        or (st.session_state[sk_ft] is not None
            and (now - st.session_state[sk_ft]).total_seconds() >= MIN_REFRESH_SEC)
    )

    c1, c2, c3, c4 = st.columns([1, 1, 2, 2])
    with c1:
        force = st.button("🔄 Refresh", type="primary", use_container_width=True, key=f"ref_{sport_key}")
    with c2:
        snap_btn = st.button("📸 Snapshot", use_container_width=True, key=f"snap_{sport_key}")
    if force or snap_btn:
        needs = True

    if needs:
        with st.spinner(f"Fetching {sport_label.split()[-1]} player props…"):
            try:
                snap, quota = fetch_sport(sport_key, markets)
                st.session_state[sk]    = snap
                st.session_state[sk_q]  = quota
                st.session_state[sk_ft] = now
            except Exception as e:
                st.error(f"Fetch failed: {e}")
                return

    snap       = st.session_state[sk]
    quota      = st.session_state[sk_q]
    fetch_time = st.session_state[sk_ft]

    with c3:
        if fetch_time:
            st.caption(f"Updated: **{fetch_time.strftime('%I:%M:%S %p ET')}** · auto 10 min")
    with c4:
        st.caption(f"API quota: **{quota}**")

    if not snap:
        return

    history      = load_history(sport_key, n=15)
    sorted_games = sorted(snap.get("games", []), key=lambda g: g.get("commence_time", ""))

    all_plays  = []
    all_alerts = []
    game_data  = []

    for g in sorted_games:
        game_name = g["game"]
        ct        = g["commence_time"]
        try:
            gt        = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
            game_time = gt.strftime("%I:%M %p")
        except Exception:
            game_time = "?"

        if g.get("started"):
            game_data.append({"game": game_name, "time": game_time, "started": True, "rows": []})
            continue

        rows = []
        for player, player_markets in sorted(g.get("players", {}).items()):
            for market, book_data in player_markets.items():
                if "pinnacle" not in book_data:
                    continue
                signals      = detect_signals(game_name, player, market, history, snap)
                s_dir, s_len = streak_info(game_name, player, market, history, snap)
                pin          = book_data.get("pinnacle", {})
                pin_line     = pin.get("line")
                pin_over     = pin.get("over")
                pin_under    = pin.get("under")
                fdata        = {bk: book_data.get(bk, {}) for bk in FOLLOWER_BOOKS}
                all_lines    = [pin_line] + [fdata[bk].get("line") for bk in FOLLOWER_BOOKS]
                all_lines    = [l for l in all_lines if l is not None]
                lines_split  = len(set(all_lines)) > 1

                ap_bk = ap_bet = ap_line = ap_price = ap_why = None
                YES_NO_MARKETS = {"player_anytime_td", "player_anytime_scorer"}
                is_yes_no = market in YES_NO_MARKETS
                if lines_split and pin_line is not None and not is_yes_no:
                    non_pin = [
                        (bk, fdata[bk].get("line"), fdata[bk].get("over"), fdata[bk].get("under"))
                        for bk in FOLLOWER_BOOKS if fdata[bk].get("line") is not None
                    ]
                    if non_pin:
                        sig_types = {s["type"] for s in signals}
                        max_e = max(non_pin, key=lambda x: x[1])
                        min_e = min(non_pin, key=lambda x: x[1])
                        if pin_line < max_e[1] and max_e[3] is not None:
                            ap_bk, ap_line, ap_bet = max_e[0], max_e[1], "UNDER"
                            ap_price = max_e[3]
                            ap_why   = f"LINE SPLIT — Pinnacle dropped to {pin_line}"
                        elif pin_line > min_e[1] and min_e[2] is not None:
                            ap_bk, ap_line, ap_bet = min_e[0], min_e[1], "OVER"
                            ap_price = min_e[2]
                            ap_why   = f"LINE SPLIT — Pinnacle raised to {pin_line}"
                elif is_yes_no and pin_over is not None:
                    # Yes/No market — look for price split vs Pinnacle
                    non_pin_yes = [
                        (bk, fdata[bk].get("over"))
                        for bk in FOLLOWER_BOOKS if fdata[bk].get("over") is not None
                    ]
                    if non_pin_yes:
                        best_yes = max(non_pin_yes, key=lambda x: x[1])
                        if best_yes[1] > pin_over + 9:  # ≥10 pt price gap
                            ap_bk, ap_line, ap_bet = best_yes[0], 0.5, "YES"
                            ap_price = best_yes[1]
                            ap_why   = f"PRICE SPLIT — Pinnacle YES {pin_over:+d}, book still at {best_yes[1]:+d}"

                if ap_bk and ap_price is not None:
                    all_plays.append({
                        "game": game_name, "time": game_time, "commence_time": ct,
                        "player": player, "market": market, "bet": ap_bet,
                        "line": ap_line, "book": ap_bk, "price": ap_price,
                        "why": ap_why, "streak": (s_dir, s_len),
                        "priority": 1 if {"FULL SEQUENCE", "SEQUENCE BUILDING"} & {s["type"] for s in signals} else 2,
                    })
                if signals:
                    all_alerts.append((game_name, player, market, game_time, signals))

                rows.append({
                    "player": player, "market": market,
                    "pin_line": pin_line, "pin_over": pin_over, "pin_under": pin_under,
                    "fdata": fdata, "lines_split": lines_split,
                    "signals": signals, "streak_dir": s_dir, "streak_len": s_len,
                    "ap_bk": ap_bk, "ap_bet": ap_bet,
                })

        game_data.append({"game": game_name, "time": game_time, "started": False, "rows": rows})

    all_plays.sort(key=lambda x: (x["commence_time"], x["game"], 0 if x["bet"] == "OVER" else 1, x["player"], x["market"]))

    # ── Actionable Plays ──────────────────────────────────────────────────────
    st.markdown("### 🎯 Actionable Plays")
    if not all_plays:
        st.info("No actionable plays right now. Plays appear when Pinnacle's line splits from follower books.")
    else:
        for play in all_plays:
            is_yes_no  = play["bet"] == "YES"
            bet_color  = "#15803d" if play["bet"] in ("OVER", "YES") else "#1d4ed8"
            bet_bg     = "#f0fdf4" if play["bet"] in ("OVER", "YES") else "#eff6ff"
            bet_border = "#86efac" if play["bet"] in ("OVER", "YES") else "#93c5fd"
            seq_tag    = "🚨 " if play.get("priority") == 1 else ""
            icon, name = mk_display.get(play["market"], ("", play["market"]))
            book_name  = BOOK_DISPLAY.get(play["book"], play["book"])
            price_str  = _price_str(play["price"])
            sd, sl     = play["streak"]
            streak_html = ""
            if sd and sl >= 3:
                arrow   = "▼" if sd == "down" else "▲"
                s_color = "#dc2626" if sd == "down" else "#16a34a"
                streak_html = (f' <span style="background:{s_color};color:#fff;font-size:10px;'
                               f'font-weight:600;padding:1px 5px;border-radius:3px;">{arrow}×{sl}</span>')
            # For Yes/No markets show "YES" without the 0.5 line
            bet_label = play["bet"] if not is_yes_no else "YES"
            line_part  = "" if is_yes_no else f" {play['line']}"
            st.markdown(
                f'<div style="background:{bet_bg};border:1.5px solid {bet_border};'
                f'border-radius:8px;padding:10px 14px;margin-bottom:6px;">'
                f'<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">'
                f'<span style="font-size:14px;font-weight:700;color:#111827;">'
                f'{seq_tag}{play["player"]}{streak_html}'
                f'<span style="font-size:11px;color:#6b7280;font-weight:400;"> — {play["game"]}</span></span>'
                f'<span style="font-size:11px;color:#9ca3af;">{play["time"]} ET</span></div>'
                f'<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:2px;">'
                f'<span style="font-size:11px;color:#6b7280;">{icon} {name}</span>'
                f'<span style="font-size:18px;font-weight:800;color:{bet_color};">{bet_label}{line_part}</span>'
                f'<span style="font-size:13px;color:#374151;">@ <strong>{book_name}</strong> {price_str}</span></div>'
                f'<div style="font-size:11px;color:#6b7280;">{play["why"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("---")

    # ── Legend ────────────────────────────────────────────────────────────────
    st.markdown("#### 🗺️ Signal Guide")
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;">'
        '<div style="background:#eff6ff;color:#1e3a5f;border:1px solid #93c5fd;border-radius:6px;padding:5px 10px;font-size:12px;">🔵 LINE SPLIT — Books disagree on the line</div>'
        '<div style="background:#fefce8;color:#713f12;border:1px solid #fde047;border-radius:6px;padding:5px 10px;font-size:12px;">🟡 PINNACLE DROP — Pinnacle over fell</div>'
        '<div style="background:#fff7ed;color:#7c2d12;border:1px solid #fdba74;border-radius:6px;padding:5px 10px;font-size:12px;">🟠 FOLLOWER / LINE DROP</div>'
        '<div style="background:#fef2f2;color:#7f1d1d;border:1px solid #fca5a5;border-radius:6px;padding:5px 10px;font-size:12px;">🔴 SEQUENCE BUILDING</div>'
        '<div style="background:#7f1d1d;color:#fecaca;border:1px solid #ef4444;border-radius:6px;padding:5px 10px;font-size:12px;">🚨 FULL SEQUENCE — BET NOW</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.markdown("---")

    # ── Alerts ────────────────────────────────────────────────────────────────
    if all_alerts:
        def _sev(a):
            lvl = {"alarm": 0, "red": 1, "orange": 2, "yellow": 3, "blue": 4}
            return min(lvl.get(s["level"], 9) for s in a[4])
        all_alerts.sort(key=_sev)
        st.markdown(f"### 🚨 {len(all_alerts)} Active Signals")
        for gname, player, market, gtime, signals in all_alerts:
            top      = min({"alarm":0,"red":1,"orange":2,"yellow":3,"blue":4}.get(s["level"],9) for s in signals)
            top_name = ["alarm","red","orange","yellow","blue"][top]
            icon_m, name_m = mk_display.get(market, ("", market))
            st.markdown(
                f'<div style="padding:10px 14px;border-radius:8px;margin-bottom:8px;{LEVEL_CSS[top_name]}">'
                f'<strong>{LEVEL_ICON[top_name]} {player}</strong> — {icon_m} {name_m}'
                f'<span style="font-size:12px;font-weight:400;"> &nbsp;{gname} ({gtime} ET)</span><br>'
                + "".join(
                    f'<span style="margin-right:14px;font-size:12px;">'
                    f'{LEVEL_ICON[s["level"]]} <strong>{s["type"]}</strong>: {s["detail"]}</span>'
                    for s in signals
                )
                + "</div>",
                unsafe_allow_html=True,
            )
        st.markdown("---")

    # ── Full grid ─────────────────────────────────────────────────────────────
    st.markdown("### 📊 All Player Props Grid")
    f1, f2 = st.columns([2, 3])
    with f1:
        sel_markets = st.multiselect(
            "Markets", options=markets, default=markets,
            format_func=lambda x: f"{mk_display.get(x,('',''))[0]} {mk_display.get(x,('',x))[1]}",
            key=f"mkts_{sport_key}",
        )
    with f2:
        search = st.text_input("🔍 Search player", key=f"srch_{sport_key}")

    for gd in game_data:
        if gd.get("started"):
            with st.expander(f"⏸ {gd['game']} — IN PROGRESS", expanded=False):
                st.caption("Game already started.")
            continue
        rows = gd["rows"]
        if sel_markets:
            rows = [r for r in rows if r["market"] in sel_markets]
        if search.strip():
            rows = [r for r in rows if search.lower() in r["player"].lower()]
        if not rows:
            continue
        n_splits  = sum(1 for r in rows if r["lines_split"])
        n_signals = sum(1 for r in rows if r["signals"])
        with st.expander(f"**{gd['game']}** — {gd['time']} ET", expanded=bool(n_splits or n_signals)):
            by_player = defaultdict(list)
            for r in rows:
                by_player[r["player"]].append(r)
            for pname, p_rows in sorted(by_player.items()):
                any_s = next((r for r in p_rows if r["streak_len"] >= 3), None)
                sh = ""
                if any_s:
                    arr = "▼" if any_s["streak_dir"] == "down" else "▲"
                    sc  = "#dc2626" if any_s["streak_dir"] == "down" else "#16a34a"
                    sh  = (f' <span style="background:{sc};color:#fff;font-size:10px;'
                           f'font-weight:600;padding:1px 5px;border-radius:3px;">{arr}×{any_s["streak_len"]}</span>')
                has_ap = any(r["ap_bk"] for r in p_rows)
                nbg    = "background:#f0fdf4;" if has_ap else ""
                st.markdown(
                    f'<div style="font-size:13px;font-weight:700;color:#111827;margin:10px 0 4px;'
                    f'padding:4px 8px;border-radius:4px;{nbg}">{pname}{sh}</div>',
                    unsafe_allow_html=True,
                )
                for r in p_rows:
                    icon_m, name_m = mk_display.get(r["market"], ("", r["market"]))
                    sig_types = {s["type"] for s in r["signals"]}
                    border_c  = (
                        "#ef4444" if "FULL SEQUENCE" in sig_types else
                        "#fca5a5" if ("SEQUENCE BUILDING" in sig_types or "CROSSED ZERO" in sig_types) else
                        "#fdba74" if ("LINE DROP" in sig_types or "FOLLOWER" in sig_types) else
                        "#93c5fd" if "LINE SPLIT" in sig_types else "#e5e7eb"
                    )
                    split_badge = (
                        ' <span style="background:#fef3c7;color:#92400e;font-size:10px;'
                        'font-weight:600;padding:1px 6px;border-radius:3px;">LINE SPLIT</span>'
                        if r["lines_split"] else ""
                    )
                    books_data = [("pinnacle", r["pin_line"], r["pin_over"], r["pin_under"])]
                    for bk in FOLLOWER_BOOKS:
                        fd = r["fdata"].get(bk, {})
                        books_data.append((bk, fd.get("line"), fd.get("over"), fd.get("under")))

                    header_html = (
                        '<tr style="border-bottom:1px solid #e5e7eb;">'
                        '<th style="padding:2px 8px 2px 6px;font-size:11px;color:#9ca3af;font-weight:500;text-align:left;">Book</th>'
                        '<th style="padding:2px 8px;font-size:11px;color:#9ca3af;font-weight:500;">Over</th>'
                        '<th style="padding:2px 0;font-size:11px;color:#9ca3af;font-weight:500;">Under</th>'
                        '</tr>'
                    )
                    rows_html = ""
                    for bk, ln, ov, un in books_data:
                        if ln is None and ov is None:
                            continue
                        label    = BOOK_DISPLAY.get(bk, bk)
                        is_play  = bk == r["ap_bk"]
                        play_bet = r["ap_bet"]
                        pin_bold = "font-weight:700;" if bk == "pinnacle" else ""
                        row_bg   = "background:#fafafa;" if bk == "pinnacle" else ""
                        ln_str   = f"{ln} " if ln is not None else ""
                        if is_play and play_bet == "OVER":
                            row_bg  = "background:#f0fdf4;"
                            o_html  = f'<strong style="color:#15803d;">{ln_str}{_price_str(ov)} ✓</strong>'
                            u_html  = f'<span style="color:#9ca3af;">{_price_str(un)}</span>'
                        elif is_play and play_bet == "UNDER":
                            row_bg  = "background:#eff6ff;"
                            o_html  = f'<span style="color:#9ca3af;">{ln_str}{_price_str(ov)}</span>'
                            u_html  = f'<strong style="color:#1d4ed8;">{_price_str(un)} ✓</strong>'
                        else:
                            o_html = f'<span>{ln_str}{_price_str(ov)}</span>'
                            u_html = f'<span>{_price_str(un)}</span>'
                        rows_html += (
                            f'<tr style="{row_bg}">'
                            f'<td style="padding:3px 8px 3px 6px;font-size:12px;color:#6b7280;{pin_bold}white-space:nowrap;">{label}</td>'
                            f'<td style="padding:3px 6px;font-size:12px;">{o_html}</td>'
                            f'<td style="padding:3px 0;font-size:12px;">{u_html}</td>'
                            f'</tr>'
                        )
                    signals_html = ""
                    if r["signals"]:
                        signals_html = '<div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px;">'
                        for s in r["signals"]:
                            signals_html += (
                                f'<span style="font-size:11px;padding:2px 7px;border-radius:4px;{LEVEL_CSS.get(s["level"],"")}"> '
                                f'{LEVEL_ICON.get(s["level"],"•")} <strong>{s["type"]}</strong>: {s["detail"]}</span>'
                            )
                        signals_html += "</div>"
                    st.markdown(
                        f'<div style="border:1px solid {border_c};border-radius:6px;padding:8px 10px;margin-bottom:6px;">'
                        f'<div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:4px;">'
                        f'{icon_m} {name_m}{split_badge}</div>'
                        f'<table style="width:100%;border-collapse:collapse;">'
                        f'{header_html}{rows_html}</table>'
                        f'{signals_html}</div>',
                        unsafe_allow_html=True,
                    )


# ── Main tabs ─────────────────────────────────────────────────────────────────
tabs = st.tabs(list(SPORTS.keys()))
for tab, (label, cfg) in zip(tabs, SPORTS.items()):
    with tab:
        render_sport_tab(label, cfg)
