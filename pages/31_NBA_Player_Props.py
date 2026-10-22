"""
🏀 NBA Player Props — Sharp Movement Tracker
Monitors Pinnacle vs follower books across all major NBA player prop markets.
"""
import json
import os
import ssl
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EASTERN          = ZoneInfo("America/New_York")
MIN_REFRESH_SEC  = 600  # 10 min (player props cost more quota per refresh)
SPORT_KEY        = "basketball_nba"
CACHE_DIR        = os.path.join(ROOT, "nba_props_cache")
HISTORY_DIR      = os.path.join(ROOT, "nba_props_history")

PLAYER_MARKETS = [
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_steals",
    "player_blocks",
    "player_turnovers",
]
MARKETS_PARAM = ",".join(PLAYER_MARKETS)

MARKET_DISPLAY = {
    "player_points":    "Points",
    "player_rebounds":  "Rebounds",
    "player_assists":   "Assists",
    "player_threes":    "3-Pointers",
    "player_steals":    "Steals",
    "player_blocks":    "Blocks",
    "player_turnovers": "Turnovers",
}
MARKET_ICON = {
    "player_points":    "🏀",
    "player_rebounds":  "💪",
    "player_assists":   "🎯",
    "player_threes":    "3️⃣",
    "player_steals":    "🤚",
    "player_blocks":    "🛡️",
    "player_turnovers": "🔄",
}

FOLLOWER_BOOKS = [
    "draftkings", "fanduel", "williamhill_us", "fanatics",
    "bovada", "betrivers", "betonlineag", "espnbet",
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
LEVEL_ICON = {
    "alarm": "🚨", "red": "🔴", "orange": "🟠", "yellow": "🟡", "blue": "🔵",
}

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="NBA Player Props", page_icon="🏀", layout="wide")
st.title("🏀 NBA Player Props — Sharp Movement Tracker")
st.caption(
    "Tracks Pinnacle vs follower books across Points, Rebounds, Assists, 3PM, Steals, Blocks, Turnovers. "
    "Flags line splits and steam sequences. Refreshes every 10 min. "
    "⚠️ Player props cost ~7 API credits per game per refresh."
)

# ── Secrets ───────────────────────────────────────────────────────────────────
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

os.makedirs(CACHE_DIR,   exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode    = ssl.CERT_NONE

def _get(url, timeout=20):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    return json.loads(resp.read()), resp.headers.get("x-requests-remaining", "?")

# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_snapshot():
    now_et     = datetime.now(tz=EASTERN)
    today_date = now_et.date()

    events, quota = _get(
        f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/events"
        f"?apiKey={ODDS_KEY}"
    )

    games = []
    for ev in events:
        eid  = ev["id"]
        home = ev["home_team"]
        away = ev["away_team"]
        ct   = ev["commence_time"]

        game_started = False
        try:
            gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
            if gt.date() != today_date:
                continue
            game_started = gt <= now_et
        except Exception:
            pass

        players = {}
        if not game_started:
            try:
                data, _ = _get(
                    f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/events/{eid}/odds"
                    f"?apiKey={ODDS_KEY}&regions=us,eu&markets={MARKETS_PARAM}&oddsFormat=american"
                )
            except Exception:
                continue

            for bm in data.get("bookmakers", []):
                bk = bm["key"]
                if bk not in TRACKED_BOOKS:
                    continue
                for mkt in bm.get("markets", []):
                    mkt_key = mkt.get("key")
                    if mkt_key not in PLAYER_MARKETS:
                        continue
                    for out in mkt.get("outcomes", []):
                        player = out.get("description", "").strip()
                        name   = out.get("name", "")
                        price  = out.get("price")
                        point  = out.get("point")
                        if not player:
                            continue
                        players.setdefault(player, {}).setdefault(mkt_key, {}).setdefault(bk, {})
                        if name == "Over":
                            players[player][mkt_key][bk]["line"] = point
                            players[player][mkt_key][bk]["over"] = price
                        elif name == "Under":
                            players[player][mkt_key][bk]["under"] = price

            if not players:
                continue

        games.append({
            "game":          f"{away} @ {home}",
            "away":          away,
            "home":          home,
            "commence_time": ct,
            "started":       game_started,
            "players":       players,
        })

    snap = {
        "captured_at": now_et.isoformat(),
        "quota":       quota,
        "games":       games,
    }
    today = now_et.strftime("%Y-%m-%d")
    path  = os.path.join(CACHE_DIR, f"{today}.json")
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


def load_history(n=10):
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    path  = os.path.join(CACHE_DIR, f"{today}.json")
    if not os.path.exists(path):
        return []
    try:
        return json.load(open(path))[-n:]
    except Exception:
        return []

# ── Signal detection ──────────────────────────────────────────────────────────
def _get_prop(snap, game_name, player, market):
    for g in snap.get("games", []):
        if g["game"] == game_name:
            return g.get("players", {}).get(player, {}).get(market, {})
    return {}


def detect_signals(game_name, player, market, history, current):
    cur = _get_prop(current, game_name, player, market)
    if not cur:
        return []

    signals    = []
    timeline   = [h for h in [_get_prop(s, game_name, player, market) for s in history] if h] + [cur]

    if len(timeline) >= 2:
        prev = timeline[-2]

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
            prev_med = sorted(prev_lines)[len(prev_lines)//2]
            cur_med  = sorted(cur_lines)[len(cur_lines)//2]
            if cur_med < prev_med:
                signals.append({"type": "LINE DROP", "level": "red",
                                 "detail": f"Line: {prev_med} → {cur_med}"})

        types = {s["type"] for s in signals}
        if "PINNACLE DROP" in types and followers and ("CROSSED ZERO" in types or "LINE DROP" in types):
            signals.insert(0, {"type": "FULL SEQUENCE", "level": "alarm",
                                "detail": "All 4 steps confirmed — BET NOW"})
        elif "PINNACLE DROP" in types and followers:
            signals.insert(0, {"type": "SEQUENCE BUILDING", "level": "red",
                                "detail": "Steps 1–2 confirmed, watching for more"})

    # Line split (static — works on single snapshot)
    all_lines = [v.get("line") for v in cur.values() if v.get("line") is not None]
    unique    = set(all_lines)
    if len(unique) > 1:
        signals.append({"type": "LINE SPLIT", "level": "blue",
                         "detail": f"Books disagree: {sorted(unique)}"})

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

# ── Controls ──────────────────────────────────────────────────────────────────
for _k, _v in [("nba_props_snap", None), ("nba_props_quota", "?"), ("nba_props_fetch_time", None)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v

col_r, col_s, col_t, col_q = st.columns([1, 1, 2, 2])
with col_r:
    force_refresh = st.button("🔄 Refresh Now", type="primary", use_container_width=True)
with col_s:
    snap_now = st.button("📸 Snapshot Now", use_container_width=True)

now = datetime.now(tz=EASTERN)
needs_refresh = (
    force_refresh
    or st.session_state.nba_props_snap is None
    or (
        st.session_state.nba_props_fetch_time is not None
        and (now - st.session_state.nba_props_fetch_time).total_seconds() >= MIN_REFRESH_SEC
    )
)

if needs_refresh:
    with st.spinner("Fetching NBA player props (this may take a moment)…"):
        try:
            snap, quota = fetch_snapshot()
            st.session_state.nba_props_snap       = snap
            st.session_state.nba_props_quota      = quota
            st.session_state.nba_props_fetch_time = now
        except Exception as e:
            st.error(f"Fetch failed: {e}")
            st.stop()

snap       = st.session_state.nba_props_snap
quota      = st.session_state.nba_props_quota
fetch_time = st.session_state.nba_props_fetch_time

with col_t:
    if fetch_time:
        st.caption(f"Last updated: **{fetch_time.strftime('%I:%M:%S %p ET')}** • auto-refreshes every 10 min")
with col_q:
    st.caption(f"API quota remaining: **{quota}**" if quota != "?" else "Quota: ?")

history = load_history(n=15)

# ── Build all plays and alerts ────────────────────────────────────────────────
all_plays  = []
all_alerts = []
game_data  = []

sorted_games = sorted(
    snap.get("games", []) if snap else [],
    key=lambda g: g.get("commence_time", "")
)

def _price_str(p):
    return f"{p:+d}" if p is not None else "—"

for g in sorted_games:
    game_name = g["game"]
    ct = g["commence_time"]
    try:
        gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
        game_time = gt.strftime("%I:%M %p")
    except Exception:
        game_time = "?"

    if g.get("started"):
        game_data.append({"game": game_name, "time": game_time, "started": True, "player_rows": []})
        continue

    player_rows = []
    for player, markets in sorted(g.get("players", {}).items()):
        for market, book_data in markets.items():
            if "pinnacle" not in book_data:
                continue

            signals = detect_signals(game_name, player, market, history, snap)
            s_dir, s_len = streak_info(game_name, player, market, history, snap)

            pin       = book_data.get("pinnacle", {})
            pin_line  = pin.get("line")
            pin_over  = pin.get("over")
            pin_under = pin.get("under")
            follower_data = {bk: book_data.get(bk, {}) for bk in FOLLOWER_BOOKS}

            all_lines   = [pin_line] + [follower_data[bk].get("line") for bk in FOLLOWER_BOOKS]
            all_lines   = [l for l in all_lines if l is not None]
            lines_split = len(set(all_lines)) > 1

            # Determine actionable play
            ap_bk = ap_bet = ap_line = ap_price = ap_why = ap_priority = None
            if lines_split and pin_line is not None:
                non_pin = [
                    (bk, follower_data[bk].get("line"),
                     follower_data[bk].get("over"),
                     follower_data[bk].get("under"))
                    for bk in FOLLOWER_BOOKS
                    if follower_data[bk].get("line") is not None
                ]
                if non_pin:
                    sig_types  = {s["type"] for s in signals}
                    max_entry  = max(non_pin, key=lambda x: x[1])
                    min_entry  = min(non_pin, key=lambda x: x[1])
                    is_seq     = "FULL SEQUENCE" in sig_types or "SEQUENCE BUILDING" in sig_types
                    if pin_line < max_entry[1] and max_entry[3] is not None:
                        ap_bk, ap_line, ap_bet   = max_entry[0], max_entry[1], "UNDER"
                        ap_price   = max_entry[3]
                        ap_why     = f"LINE SPLIT — Pinnacle dropped to {pin_line}"
                        ap_priority = 1 if is_seq else 2
                    elif pin_line > min_entry[1] and min_entry[2] is not None:
                        ap_bk, ap_line, ap_bet   = min_entry[0], min_entry[1], "OVER"
                        ap_price   = min_entry[2]
                        ap_why     = f"LINE SPLIT — Pinnacle raised to {pin_line}"
                        ap_priority = 1 if is_seq else 2

            if ap_bk and ap_price is not None and ap_why:
                all_plays.append({
                    "game":          game_name,
                    "time":          game_time,
                    "commence_time": ct,
                    "player":        player,
                    "market":        market,
                    "bet":           ap_bet,
                    "line":          ap_line,
                    "book":          ap_bk,
                    "price":         ap_price,
                    "why":           ap_why,
                    "priority":      ap_priority,
                    "streak":        (s_dir, s_len),
                    "pin_line":      pin_line,
                })

            if signals:
                all_alerts.append((game_name, player, market, game_time, signals))

            player_rows.append({
                "player":        player,
                "market":        market,
                "pin_line":      pin_line,
                "pin_over":      pin_over,
                "pin_under":     pin_under,
                "follower_data": follower_data,
                "lines_split":   lines_split,
                "signals":       signals,
                "streak_dir":    s_dir,
                "streak_len":    s_len,
                "ap_bk":         ap_bk,
                "ap_bet":        ap_bet,
            })

    game_data.append({
        "game":        game_name,
        "away":        g.get("away", ""),
        "home":        g.get("home", ""),
        "time":        game_time,
        "started":     False,
        "player_rows": player_rows,
    })

# ── ACTIONABLE PLAYS ──────────────────────────────────────────────────────────
st.markdown("### 🎯 Actionable Plays")
all_plays.sort(key=lambda x: (x["commence_time"], x["player"], x["market"]))

if not all_plays:
    st.info("No actionable plays right now. Plays appear when Pinnacle's line splits from follower books.")
else:
    for play in all_plays:
        bet_color  = "#15803d" if play["bet"] == "OVER" else "#1d4ed8"
        bet_bg     = "#f0fdf4" if play["bet"] == "OVER" else "#eff6ff"
        bet_border = "#86efac" if play["bet"] == "OVER" else "#93c5fd"
        seq_tag    = "🚨 " if play.get("priority") == 1 else ""
        mkt_icon   = MARKET_ICON.get(play["market"], "")
        mkt_name   = MARKET_DISPLAY.get(play["market"], play["market"])
        book_name  = BOOK_DISPLAY.get(play["book"], play["book"])
        price_str  = _price_str(play["price"])

        streak_dir, streak_len = play["streak"]
        streak_html = ""
        if streak_dir and streak_len >= 3:
            arrow   = "▼" if streak_dir == "down" else "▲"
            s_color = "#dc2626" if streak_dir == "down" else "#16a34a"
            streak_html = (f' <span style="background:{s_color};color:#fff;font-size:10px;'
                           f'font-weight:600;padding:1px 5px;border-radius:3px;">'
                           f'{arrow}×{streak_len}</span>')

        st.markdown(
            f'<div style="background:{bet_bg};border:1.5px solid {bet_border};'
            f'border-radius:8px;padding:10px 14px;margin-bottom:6px;">'
            # Row 1: player + game + time
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">'
            f'<span style="font-size:14px;font-weight:700;color:#111827;">'
            f'{seq_tag}{play["player"]}{streak_html}'
            f'<span style="font-size:11px;color:#6b7280;font-weight:400;"> — {play["game"]}</span>'
            f'</span>'
            f'<span style="font-size:11px;color:#9ca3af;">{play["time"]} ET</span>'
            f'</div>'
            # Row 2: market + bet + line + book + price
            f'<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:2px;">'
            f'<span style="font-size:11px;color:#6b7280;">{mkt_icon} {mkt_name}</span>'
            f'<span style="font-size:18px;font-weight:800;color:{bet_color};">{play["bet"]} {play["line"]}</span>'
            f'<span style="font-size:13px;color:#374151;">@ <strong>{book_name}</strong> {price_str}</span>'
            f'</div>'
            # Row 3: reason
            f'<div style="font-size:11px;color:#6b7280;">{play["why"]}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

st.markdown("---")

# ── Signal Legend ─────────────────────────────────────────────────────────────
st.markdown("#### 🗺️ Signal Color Guide")
st.markdown(
    '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;">'
    '<div style="background:#eff6ff;color:#1e3a5f;border:1px solid #93c5fd;border-radius:6px;padding:6px 12px;font-size:13px;">'
    '🔵 <strong>LINE SPLIT</strong> — Books disagree on the line</div>'
    '<div style="background:#fefce8;color:#713f12;border:1px solid #fde047;border-radius:6px;padding:6px 12px;font-size:13px;">'
    '🟡 <strong>PINNACLE DROP</strong> — Pinnacle over price fell (sharp OVER action)</div>'
    '<div style="background:#fff7ed;color:#7c2d12;border:1px solid #fdba74;border-radius:6px;padding:6px 12px;font-size:13px;">'
    '🟠 <strong>FOLLOWER / LINE DROP</strong> — Follower dropped or line moved</div>'
    '<div style="background:#fef2f2;color:#7f1d1d;border:1px solid #fca5a5;border-radius:6px;padding:6px 12px;font-size:13px;">'
    '🔴 <strong>SEQUENCE BUILDING</strong> — Steps 1–2 confirmed</div>'
    '<div style="background:#7f1d1d;color:#fecaca;border:1px solid #ef4444;border-radius:6px;padding:6px 12px;font-size:13px;">'
    '🚨 <strong>FULL SEQUENCE</strong> — All 4 steps hit. <strong>BET NOW.</strong></div>'
    '</div>',
    unsafe_allow_html=True,
)

st.markdown("---")

# ── ALERTS ────────────────────────────────────────────────────────────────────
if all_alerts:
    def _sev(a):
        lvl = {"alarm": 0, "red": 1, "orange": 2, "yellow": 3, "blue": 4}
        return min(lvl.get(s["level"], 9) for s in a[4])
    all_alerts.sort(key=_sev)

    st.markdown(f"### 🚨 {len(all_alerts)} Active Signals")
    for game_name, player, market, game_time, signals in all_alerts:
        top = min({"alarm":0,"red":1,"orange":2,"yellow":3,"blue":4}.get(s["level"],9) for s in signals)
        top_name = ["alarm","red","orange","yellow","blue"][top]
        css  = LEVEL_CSS[top_name]
        icon = LEVEL_ICON[top_name]
        mkt_name = MARKET_DISPLAY.get(market, market)
        mkt_icon = MARKET_ICON.get(market, "")
        st.markdown(
            f'<div style="padding:10px 14px;border-radius:8px;margin-bottom:8px;{css}">'
            f'<strong>{icon} {player}</strong> — {mkt_icon} {mkt_name}'
            f'<span style="font-size:12px;font-weight:400;"> &nbsp;{game_name} ({game_time} ET)</span><br>'
            + "".join(
                f'<span style="margin-right:14px;font-size:12px;">'
                f'{LEVEL_ICON[s["level"]]} <strong>{s["type"]}</strong>: {s["detail"]}</span>'
                for s in signals
            )
            + "</div>",
            unsafe_allow_html=True,
        )
else:
    st.info("No signals yet. Signals appear as Pinnacle's lines move.")

st.markdown("---")

# ── Full Player Grid ──────────────────────────────────────────────────────────
st.markdown("### 📊 All Games — Player Props Grid")

fcol1, fcol2 = st.columns([2, 3])
with fcol1:
    selected_markets = st.multiselect(
        "Markets",
        options=PLAYER_MARKETS,
        default=PLAYER_MARKETS,
        format_func=lambda x: f"{MARKET_ICON.get(x,'')} {MARKET_DISPLAY.get(x,x)}",
    )
with fcol2:
    search_player = st.text_input("🔍 Search player", placeholder="e.g. LeBron James")

for gd in game_data:
    if gd.get("started"):
        with st.expander(f"⏸ {gd['game']} — STARTED", expanded=False):
            st.caption("Game in progress.")
        continue

    rows = gd["player_rows"]
    if selected_markets:
        rows = [r for r in rows if r["market"] in selected_markets]
    if search_player.strip():
        rows = [r for r in rows if search_player.lower() in r["player"].lower()]
    if not rows:
        continue

    n_splits  = sum(1 for r in rows if r["lines_split"])
    n_signals = sum(1 for r in rows if r["signals"])
    badge = ""
    if n_splits:
        badge += f' <span style="background:#eff6ff;color:#1e3a5f;border:1px solid #93c5fd;font-size:11px;padding:1px 7px;border-radius:4px;">{n_splits} splits</span>'
    if n_signals:
        badge += f' <span style="background:#fef2f2;color:#7f1d1d;border:1px solid #fca5a5;font-size:11px;padding:1px 7px;border-radius:4px;">{n_signals} signals</span>'

    with st.expander(f"**{gd['game']}** — {gd['time']} ET", expanded=bool(n_splits or n_signals)):

        by_player = defaultdict(list)
        for r in rows:
            by_player[r["player"]].append(r)

        for player_name, p_rows in sorted(by_player.items()):
            any_streak = next((r for r in p_rows if r["streak_len"] >= 3), None)
            streak_html = ""
            if any_streak:
                arrow   = "▼" if any_streak["streak_dir"] == "down" else "▲"
                s_color = "#dc2626" if any_streak["streak_dir"] == "down" else "#16a34a"
                streak_html = (f' <span style="background:{s_color};color:#fff;font-size:10px;'
                               f'font-weight:600;padding:1px 5px;border-radius:3px;">'
                               f'{arrow}×{any_streak["streak_len"]}</span>')

            has_action = any(r["ap_bk"] for r in p_rows)
            name_bg    = "background:#f0fdf4;" if has_action else ""

            st.markdown(
                f'<div style="font-size:13px;font-weight:700;color:#111827;'
                f'margin:10px 0 4px;padding:4px 8px;border-radius:4px;{name_bg}">'
                f'{player_name}{streak_html}</div>',
                unsafe_allow_html=True,
            )

            for r in p_rows:
                mkt_name = MARKET_DISPLAY.get(r["market"], r["market"])
                mkt_icon = MARKET_ICON.get(r["market"], "")

                sig_types   = {s["type"] for s in r["signals"]}
                border_color = (
                    "#ef4444" if "FULL SEQUENCE" in sig_types else
                    "#fca5a5" if ("SEQUENCE BUILDING" in sig_types or "CROSSED ZERO" in sig_types) else
                    "#fdba74" if ("LINE DROP" in sig_types or "FOLLOWER" in sig_types) else
                    "#93c5fd" if "LINE SPLIT" in sig_types else
                    "#e5e7eb"
                )

                split_badge = (
                    ' <span style="background:#fef3c7;color:#92400e;font-size:10px;'
                    'font-weight:600;padding:1px 6px;border-radius:3px;">LINE SPLIT</span>'
                    if r["lines_split"] else ""
                )

                books_data = [("pinnacle", r["pin_line"], r["pin_over"], r["pin_under"])]
                for bk in FOLLOWER_BOOKS:
                    fd = r["follower_data"].get(bk, {})
                    books_data.append((bk, fd.get("line"), fd.get("over"), fd.get("under")))

                header_html = (
                    '<tr style="border-bottom:1px solid #e5e7eb;">'
                    '<th style="padding:2px 8px 2px 6px;font-size:11px;color:#9ca3af;font-weight:500;text-align:left;">Book</th>'
                    '<th style="padding:2px 8px;font-size:11px;color:#9ca3af;font-weight:500;">Line</th>'
                    '<th style="padding:2px 6px;font-size:11px;color:#9ca3af;font-weight:500;">Over</th>'
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
                    line_str = str(ln) if ln is not None else "—"
                    o_str    = _price_str(ov)
                    u_str    = _price_str(un)
                    pin_bold = "font-weight:700;" if bk == "pinnacle" else ""

                    if is_play and play_bet == "OVER":
                        row_style = "background:#f0fdf4;"
                        o_html = f'<strong style="color:#15803d;">{o_str} ✓</strong>'
                        u_html = f'<span style="color:#9ca3af;">{u_str}</span>'
                    elif is_play and play_bet == "UNDER":
                        row_style = "background:#eff6ff;"
                        o_html = f'<span style="color:#9ca3af;">{o_str}</span>'
                        u_html = f'<strong style="color:#1d4ed8;">{u_str} ✓</strong>'
                    else:
                        row_style = "background:#fafafa;" if bk == "pinnacle" else ""
                        o_html = f'<span>{o_str}</span>'
                        u_html = f'<span>{u_str}</span>'

                    rows_html += (
                        f'<tr style="{row_style}">'
                        f'<td style="padding:3px 8px 3px 6px;font-size:12px;color:#6b7280;{pin_bold}white-space:nowrap;">{label}</td>'
                        f'<td style="padding:3px 8px;font-size:12px;color:#374151;">{line_str}</td>'
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
                    f'<div style="border:1px solid {border_color};border-radius:6px;'
                    f'padding:8px 10px;margin-bottom:6px;">'
                    f'<div style="font-size:12px;font-weight:600;color:#374151;margin-bottom:4px;">'
                    f'{mkt_icon} {mkt_name}{split_badge}</div>'
                    f'<table style="width:100%;border-collapse:collapse;">'
                    f'{header_html}{rows_html}</table>'
                    f'{signals_html}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
