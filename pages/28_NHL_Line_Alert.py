"""
🚨 NHL Line Movement Alert

Monitors team totals every 5 min and fires when the steam sequence triggers:
  Step 1 — Pinnacle over price drops
  Step 2 — William Hill or another book follows
  Step 3 — Over price approaches or crosses zero
  Step 4 — Line drops a half-point

Books used: Pinnacle (sharp), William Hill (Caesars), Fanatics
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

EASTERN     = ZoneInfo("America/New_York")
CACHE_DIR   = os.path.join(ROOT, "nhl_line_alert_cache")
TRACKED_BOOKS = {"pinnacle", "williamhill_us", "fanatics"}
MIN_REFRESH_SEC = 300  # 5 minutes

os.makedirs(CACHE_DIR, exist_ok=True)

st.set_page_config(page_title="NHL Line Alert", page_icon="🚨", layout="wide")
st.title("🚨 NHL Line Movement Alert")
st.caption(
    "Fires when the steam sequence triggers: **Pinnacle over drops → book follows → "
    "price crosses zero → line drops**. Refreshes every 5 min. Uses Pinnacle + "
    "William Hill + Fanatics (the only books posting NHL team totals)."
)

# ── Secrets ──────────────────────────────────────────────────────────────────

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

# ── Fetch ─────────────────────────────────────────────────────────────────────

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _get(url, timeout=15):
    resp = urllib.request.urlopen(url, timeout=timeout, context=_CTX)
    return json.loads(resp.read()), resp.headers.get("x-requests-remaining", "?")


def fetch_snapshot():
    now_et = datetime.now(tz=EASTERN)
    events, quota = _get(
        f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/events"
        f"?apiKey={ODDS_KEY}"
    )

    games = []
    for ev in events:
        eid  = ev["id"]
        home = ev["home_team"]
        away = ev["away_team"]
        ct   = ev["commence_time"]

        try:
            data, _ = _get(
                f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/events/{eid}/odds"
                f"?apiKey={ODDS_KEY}&regions=us,eu,uk&markets=team_totals&oddsFormat=american"
            )
        except Exception:
            continue

        sides = defaultdict(dict)
        for bm in data.get("bookmakers", []):
            bk = bm["key"]
            if bk not in TRACKED_BOOKS:
                continue
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "team_totals":
                    continue
                for out in mkt.get("outcomes", []):
                    team  = out.get("description", "")
                    name  = out.get("name", "")
                    price = out.get("price")
                    point = out.get("point")
                    if name == "Over":
                        sides[team][bk] = {"line": point, "over": price}
                    elif name == "Under":
                        if bk not in sides[team]:
                            sides[team][bk] = {}
                        sides[team][bk]["under"] = price

        if sides:
            games.append({
                "game":         f"{away} @ {home}",
                "away":         away,
                "home":         home,
                "commence_time": ct,
                "sides":        {k: v for k, v in sides.items()},
            })

    snap = {
        "captured_at": now_et.isoformat(),
        "quota":        quota,
        "games":        games,
    }
    # Persist to today's cache file
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
    """Return the last n snapshots from today's cache (excluding current)."""
    today = datetime.now(tz=EASTERN).strftime("%Y-%m-%d")
    path  = os.path.join(CACHE_DIR, f"{today}.json")
    if not os.path.exists(path):
        return []
    try:
        snaps = json.load(open(path))
        return snaps[-n:]
    except Exception:
        return []


# ── Sequence detection ────────────────────────────────────────────────────────

def _side_prices(snap, game_name, team):
    """Return {book: {line, over, under}} for a game-side in a snapshot."""
    for g in snap.get("games", []):
        if g["game"] == game_name:
            return g.get("sides", {}).get(team, {})
    return {}


def detect_signals(game_name, team, history, current):
    """Return list of signal dicts for a specific game-side."""
    cur = _side_prices(current, game_name, team)
    if not cur:
        return []

    signals = []
    history_sides = [_side_prices(s, game_name, team) for s in history]
    # Build timeline oldest→newest
    timeline = [h for h in history_sides if h] + [cur]

    if len(timeline) < 2:
        # Single snapshot — check static signals only
        lines = [v.get("line") for v in cur.values() if v.get("line") is not None]
        unique = set(l for l in lines if l != 0.5)
        if len(unique) > 1:
            signals.append({"type": "LINE_SPLIT", "level": "blue",
                             "detail": f"Lines split: {sorted(unique)}"})
        return signals

    prev = timeline[-2]

    # ── Signal 1: Pinnacle over price dropped ────────────────────────────────
    pin_prev = prev.get("pinnacle", {}).get("over")
    pin_curr = cur.get("pinnacle", {}).get("over")
    pinnacle_dropped = False
    if pin_prev is not None and pin_curr is not None and pin_curr < pin_prev:
        pinnacle_dropped = True
        signals.append({
            "type": "PINNACLE DROP",
            "level": "yellow",
            "detail": f"Pinnacle over: {pin_prev:+d} → {pin_curr:+d}  ({pin_curr - pin_prev:+d})",
        })

    # ── Signal 2: Another book follows Pinnacle ──────────────────────────────
    followers = []
    for bk in ("williamhill_us", "fanatics"):
        bk_prev = prev.get(bk, {}).get("over")
        bk_curr = cur.get(bk, {}).get("over")
        if bk_prev is not None and bk_curr is not None and bk_curr < bk_prev:
            followers.append(bk)
            signals.append({
                "type": "FOLLOWER",
                "level": "orange",
                "detail": f"{bk}: {bk_prev:+d} → {bk_curr:+d}  ({bk_curr - bk_prev:+d})",
            })

    # ── Signal 3: Over price crosses zero ────────────────────────────────────
    for bk, v_cur in cur.items():
        v_prev = prev.get(bk, {})
        o_p = v_prev.get("over")
        o_c = v_cur.get("over")
        if o_p is not None and o_c is not None:
            if (o_p > 0 and o_c < 0) or (o_p < 0 and o_c > 0):
                signals.append({
                    "type": "CROSSED ZERO",
                    "level": "red",
                    "detail": f"{bk}: {o_p:+d} → {o_c:+d}",
                })

    # ── Signal 4: Line dropped ────────────────────────────────────────────────
    prev_lines = [v.get("line") for v in prev.values() if v.get("line") is not None]
    cur_lines  = [v.get("line") for v in cur.values() if v.get("line") is not None]
    if prev_lines and cur_lines:
        prev_med = sorted(prev_lines)[len(prev_lines) // 2]
        cur_med  = sorted(cur_lines)[len(cur_lines) // 2]
        if cur_med < prev_med:
            signals.append({
                "type": "LINE DROP",
                "level": "red",
                "detail": f"Line: {prev_med} → {cur_med}",
            })

    # ── Composite: FULL SEQUENCE ──────────────────────────────────────────────
    types = {s["type"] for s in signals}
    if "PINNACLE DROP" in types and followers and (
            "CROSSED ZERO" in types or "LINE DROP" in types):
        signals.insert(0, {
            "type": "FULL SEQUENCE",
            "level": "alarm",
            "detail": "All 4 steps confirmed — Pinnacle → follower → cross/drop",
        })
    elif "PINNACLE DROP" in types and followers:
        signals.insert(0, {
            "type": "SEQUENCE BUILDING",
            "level": "red",
            "detail": "Steps 1–2 confirmed. Watching for price crossing zero.",
        })

    # ── Static: line split (no history needed) ───────────────────────────────
    unique_lines = set(
        v.get("line") for v in cur.values()
        if v.get("line") is not None and v.get("line") != 0.5
    )
    if len(unique_lines) > 1:
        signals.append({
            "type": "LINE SPLIT",
            "level": "blue",
            "detail": f"Books disagree on line: {sorted(unique_lines)}",
        })

    return signals


# ── Consecutive streak ────────────────────────────────────────────────────────

def streak_info(game_name, team, history, current):
    """Return (direction, length) of current price streak for Pinnacle over."""
    all_snaps = [s for s in history if _side_prices(s, game_name, team)] + [current]
    prices = []
    for s in all_snaps:
        p = _side_prices(s, game_name, team).get("pinnacle", {}).get("over")
        if p is not None:
            prices.append(p)
    if len(prices) < 2:
        return None, 0
    direction = None
    length = 1
    for i in range(len(prices) - 1, 0, -1):
        d = prices[i] - prices[i - 1]
        if d == 0:
            continue
        cur_d = "down" if d < 0 else "up"
        if direction is None:
            direction = cur_d
            length = 1
        elif cur_d == direction:
            length += 1
        else:
            break
    return direction, length


# ── Controls ──────────────────────────────────────────────────────────────────

col_r, col_t, col_q = st.columns([1, 2, 2])

# Auto-refresh from session state
if "last_fetch_time" not in st.session_state:
    st.session_state.last_fetch_time = None
if "last_snap" not in st.session_state:
    st.session_state.last_snap = None
if "last_quota" not in st.session_state:
    st.session_state.last_quota = "?"

with col_r:
    force_refresh = st.button("🔄 Refresh Now", type="primary", use_container_width=True)

now = datetime.now(tz=EASTERN)
needs_refresh = (
    force_refresh
    or st.session_state.last_snap is None
    or (
        st.session_state.last_fetch_time is not None
        and (now - st.session_state.last_fetch_time).total_seconds() >= MIN_REFRESH_SEC
    )
)

if needs_refresh:
    with st.spinner("Fetching team totals from 16 games…"):
        try:
            snap, quota = fetch_snapshot()
            st.session_state.last_snap     = snap
            st.session_state.last_quota    = quota
            st.session_state.last_fetch_time = now
        except Exception as e:
            st.error(f"Fetch failed: {e}")
            st.stop()

snap  = st.session_state.last_snap
quota = st.session_state.last_quota
fetch_time = st.session_state.last_fetch_time

with col_t:
    if fetch_time:
        st.caption(f"Last updated: **{fetch_time.strftime('%I:%M:%S %p ET')}**  •  auto-refreshes every 5 min")
with col_q:
    st.caption(f"API quota remaining: **{quota}**  •  ~{int(quota)//17 if str(quota).isdigit() else '?'} refreshes left" if str(quota).isdigit() else f"Quota: {quota}")

history = load_history(n=15)

# ── Scan all games for signals ────────────────────────────────────────────────

all_alerts = []   # (game, team, signals, game_time)
game_rows  = []   # for full grid

for g in snap.get("games", []):
    game_name = g["game"]
    ct = g["commence_time"]
    try:
        gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
        game_time = gt.strftime("%I:%M %p")
    except Exception:
        game_time = "?"

    row = {"game": game_name, "time": game_time, "sides": []}

    for team in [g["away"], g["home"]]:
        cur_side = g["sides"].get(team, {})
        if not cur_side:
            continue

        signals = detect_signals(game_name, team, history, snap)
        s_dir, s_len = streak_info(game_name, team, history, snap)

        pin  = cur_side.get("pinnacle", {})
        wil  = cur_side.get("williamhill_us", {})
        fan  = cur_side.get("fanatics", {})

        pin_line  = pin.get("line")
        wil_line  = wil.get("line")
        fan_line  = fan.get("line")
        pin_over  = pin.get("over")
        wil_over  = wil.get("over")
        fan_over  = fan.get("over")
        pin_under = pin.get("under")

        all_lines = [l for l in [pin_line, wil_line, fan_line] if l is not None]
        lines_split = len(set(l for l in all_lines if l != 0.5)) > 1

        # When lines differ, find the book with the highest (easiest) line — that's the play
        book_entries = [
            ("pinnacle",       pin_line, pin_over),
            ("williamhill_us", wil_line, wil_over),
            ("fanatics",       fan_line, fan_over),
        ]
        valid = [(bk, ln, ov) for bk, ln, ov in book_entries
                 if ln is not None and ln != 0.5 and ov is not None]
        best_line_book = max(valid, key=lambda x: x[1]) if valid else None

        # When lines are same, find best (highest) over price
        same_line_books = [(bk, ln, ov) for bk, ln, ov in valid
                           if pin_line and ln == pin_line]
        best_price_book = max(same_line_books, key=lambda x: x[2]) if same_line_books else None

        row["sides"].append({
            "team":           team,
            "pin_line":       pin_line,
            "wil_line":       wil_line,
            "fan_line":       fan_line,
            "lines_split":    lines_split,
            "pin_over":       pin_over,
            "wil_over":       wil_over,
            "fan_over":       fan_over,
            "pin_under":      pin_under,
            "best_line_book": best_line_book,   # (book, line, price) — play when lines differ
            "best_price_book":best_price_book,  # (book, line, price) — play when lines same
            "streak_dir":     s_dir,
            "streak_len":     s_len,
            "signals":        signals,
        })

        if signals:
            all_alerts.append((game_name, team, game_time, signals))

    game_rows.append(row)

# ── ALERTS section ────────────────────────────────────────────────────────────

LEVEL_CSS = {
    "alarm":  "background:#7f1d1d;color:#fecaca;border:1px solid #ef4444;",
    "red":    "background:#fef2f2;color:#7f1d1d;border:1px solid #fca5a5;",
    "orange": "background:#fff7ed;color:#7c2d12;border:1px solid #fdba74;",
    "yellow": "background:#fefce8;color:#713f12;border:1px solid #fde047;",
    "blue":   "background:#eff6ff;color:#1e3a5f;border:1px solid #93c5fd;",
}
LEVEL_ICON = {
    "alarm":  "🚨",
    "red":    "🔴",
    "orange": "🟠",
    "yellow": "🟡",
    "blue":   "🔵",
}

if all_alerts:
    # Sort by severity
    def _severity(a):
        levels = {"alarm": 0, "red": 1, "orange": 2, "yellow": 3, "blue": 4}
        top = min(levels.get(s["level"], 9) for s in a[3])
        return top
    all_alerts.sort(key=_severity)

    st.markdown(f"### 🚨 {len(all_alerts)} Active Signals")
    for game_name, team, game_time, signals in all_alerts:
        top_level = min(
            {"alarm": 0, "red": 1, "orange": 2, "yellow": 3, "blue": 4}.get(s["level"], 9)
            for s in signals
        )
        top_name = ["alarm", "red", "orange", "yellow", "blue"][top_level]
        css = LEVEL_CSS[top_name]
        icon = LEVEL_ICON[top_name]
        with st.container():
            st.markdown(
                f'<div style="padding:12px 16px;border-radius:8px;margin-bottom:10px;{css}">'
                f'<strong>{icon} {game_name} — {team}</strong> &nbsp;({game_time} ET)<br>'
                + "".join(
                    f'<span style="margin-right:16px;font-size:13px;">'
                    f'{LEVEL_ICON[s["level"]]} <strong>{s["type"]}</strong>: {s["detail"]}</span>'
                    for s in signals
                )
                + "</div>",
                unsafe_allow_html=True,
            )
else:
    st.info("No signals yet. Signals appear here when the sequence triggers.")

# ── Full game grid ────────────────────────────────────────────────────────────

st.markdown("---")
st.markdown("### 📊 All Games — Current Team Totals")
st.caption(
    "Each book shows **line @ over price**. "
    "When lines differ, the play is the OVER at the book with the **highest line** (easiest to cover). "
    "When lines match, the play is the OVER at the **best price**. "
    "Streak = consecutive Pinnacle over moves same direction."
)

def _price_str(p):
    return f"{p:+d}" if p is not None else "—"

def _streak_badge(direction, length):
    if direction is None or length < 2:
        return ""
    arrow = "▼" if direction == "down" else "▲"
    color = "#dc2626" if direction == "down" else "#16a34a"
    return (f'<span style="background:{color};color:#fff;font-size:11px;'
            f'font-weight:600;padding:2px 6px;border-radius:4px;margin-left:6px;">'
            f'{arrow}×{length}</span>')

BOOK_LABELS = {
    "pinnacle":       "Pinnacle",
    "williamhill_us": "Wm Hill",
    "fanatics":       "Fanatics",
}

def _book_row(bk_key, line, over, is_play, play_reason):
    label = BOOK_LABELS.get(bk_key, bk_key)
    line_str  = str(line) if line is not None else "—"
    price_str = _price_str(over)
    if is_play:
        row_style = "background:#f0fdf4;"
        price_html = f'<strong style="color:#15803d;font-size:15px;">{price_str}</strong>'
        play_tag   = (f'<span style="background:#15803d;color:#fff;font-size:10px;'
                      f'font-weight:700;padding:1px 6px;border-radius:3px;margin-left:6px;">'
                      f'BET HERE — {play_reason}</span>')
    else:
        row_style  = ""
        price_html = f'<span style="font-size:14px;">{price_str}</span>'
        play_tag   = ""
    return (f'<tr style="{row_style}">'
            f'<td style="padding:5px 10px 5px 6px;color:#6b7280;font-size:13px;white-space:nowrap;">{label}</td>'
            f'<td style="padding:5px 8px;font-size:13px;color:#374151;">{line_str}</td>'
            f'<td style="padding:5px 0;">{price_html}{play_tag}</td>'
            f'</tr>')

for row in game_rows:
    with st.expander(f"**{row['game']}** — {row['time']} ET", expanded=True):
        cols = st.columns(len(row["sides"]) or 1)
        for i, side in enumerate(row["sides"]):
            with cols[i]:
                sig_types = {s["type"] for s in side["signals"]}
                has_alarm = "FULL SEQUENCE" in sig_types
                has_seq   = "SEQUENCE BUILDING" in sig_types
                has_cross = "CROSSED ZERO" in sig_types
                has_drop  = "LINE DROP" in sig_types

                label = side["team"]
                if has_alarm:              label = "🚨 " + label
                elif has_seq or has_cross: label = "🔴 " + label
                elif has_drop:             label = "🟠 " + label

                streak_html = _streak_badge(side["streak_dir"], side["streak_len"])

                # Determine the play
                lines_split    = side["lines_split"]
                best_line_book = side["best_line_book"]   # (bk, line, price)
                best_price_book= side["best_price_book"]  # (bk, line, price)

                if lines_split and best_line_book:
                    play_bk     = best_line_book[0]
                    play_reason = f"highest line {best_line_book[1]}"
                    split_badge = (f'<span style="background:#fef3c7;color:#92400e;font-size:11px;'
                                   f'font-weight:600;padding:2px 7px;border-radius:4px;margin-left:6px;">'
                                   f'LINE SPLIT</span>')
                elif best_price_book:
                    play_bk     = best_price_book[0]
                    play_reason = f"best price {_price_str(best_price_book[2])}"
                    split_badge = ""
                else:
                    play_bk     = None
                    play_reason = ""
                    split_badge = ""

                st.markdown(
                    f'<div style="margin-bottom:6px;font-size:15px;font-weight:600;">'
                    f'{label}{streak_html}{split_badge}</div>',
                    unsafe_allow_html=True,
                )

                # Book rows: line @ price, with BET HERE on the play book
                books_data = [
                    ("pinnacle",       side["pin_line"], side["pin_over"]),
                    ("williamhill_us", side["wil_line"], side["wil_over"]),
                    ("fanatics",       side["fan_line"], side["fan_over"]),
                ]
                header = ('<tr style="border-bottom:1px solid #e5e7eb;">'
                          '<th style="padding:3px 10px 3px 6px;font-size:11px;color:#9ca3af;font-weight:500;text-align:left;">Book</th>'
                          '<th style="padding:3px 8px;font-size:11px;color:#9ca3af;font-weight:500;text-align:left;">Line</th>'
                          '<th style="padding:3px 0;font-size:11px;color:#9ca3af;font-weight:500;text-align:left;">Over</th>'
                          '</tr>')
                rows_html = header + "".join(
                    _book_row(bk, ln, ov, bk == play_bk, play_reason)
                    for bk, ln, ov in books_data
                    if ln is not None or ov is not None
                )
                st.markdown(
                    f'<table style="width:100%;border-collapse:collapse;margin-bottom:6px;">{rows_html}</table>',
                    unsafe_allow_html=True,
                )

                # Signal badges
                if side["signals"]:
                    for s in side["signals"]:
                        icon = LEVEL_ICON.get(s["level"], "•")
                        css  = LEVEL_CSS.get(s["level"], "")
                        st.markdown(
                            f'<div style="font-size:12px;padding:4px 8px;border-radius:4px;'
                            f'margin-top:3px;{css}">'
                            f'{icon} <strong>{s["type"]}</strong>: {s["detail"]}</div>',
                            unsafe_allow_html=True,
                        )

# ── Auto-rerun every 5 min ────────────────────────────────────────────────────

st.markdown("---")
st.caption(
    f"Quota cost: ~17 credits/refresh  •  Min interval: 5 min  •  "
    f"Books: Pinnacle + William Hill + Fanatics  •  "
    f"Sequence: PIN drop → follower → cross zero → line drop"
)

# Schedule rerun
if fetch_time:
    elapsed = (datetime.now(tz=EASTERN) - fetch_time).total_seconds()
    wait_ms  = max(0, int((MIN_REFRESH_SEC - elapsed) * 1000))
    if wait_ms > 0:
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=wait_ms + 5000, key="line_alert_refresh")
        except ImportError:
            pass
