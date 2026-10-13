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
HISTORY_DIR = os.path.join(ROOT, "nhl_line_alert_history")
TRACKED_BOOKS = {"pinnacle", "williamhill_us", "fanatics"}
MIN_REFRESH_SEC = 300  # 5 minutes

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)

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


def fetch_scores():
    """Fetch current + recent game scores from the Odds API (1 credit)."""
    data, _ = _get(
        f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/scores/"
        f"?apiKey={ODDS_KEY}&daysFrom=3"
    )
    lookup = {}
    for ev in data:
        away = ev.get("away_team", "")
        home = ev.get("home_team", "")
        key  = f"{away} @ {home}"
        completed  = ev.get("completed", False)
        raw_scores = ev.get("scores") or []
        team_scores = {}
        for s in raw_scores:
            try:
                team_scores[s["name"]] = int(s["score"])
            except Exception:
                pass
        lookup[key] = {
            "completed":   completed,
            "team_scores": team_scores,
            "last_update": ev.get("last_update"),
        }
    return lookup


def save_play_record(plays, snap_time):
    """Append detected plays to today's history file (for GitHub Actions to commit)."""
    if not plays:
        return
    today = snap_time.strftime("%Y-%m-%d")
    path  = os.path.join(HISTORY_DIR, f"{today}.json")
    existing = {}
    if os.path.exists(path):
        try:
            existing = json.load(open(path, encoding="utf-8"))
        except Exception:
            existing = {}
    snap_key = snap_time.strftime("%H:%M")
    existing[snap_key] = {
        "captured_at": snap_time.isoformat(),
        "plays":       plays,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass


def load_play_history(days=7):
    """Load play records from the last N days (committed history files)."""
    records = []
    today_et = datetime.now(tz=EASTERN)
    for i in range(days):
        d = (today_et - timedelta(days=i)).strftime("%Y-%m-%d")
        path = os.path.join(HISTORY_DIR, f"{d}.json")
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
            for snap_time, snap_data in data.items():
                for play in snap_data.get("plays", []):
                    records.append({**play, "date": d, "snap_time": snap_time})
        except Exception:
            pass
    return records


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

col_r, col_s, col_t, col_q = st.columns([1, 1, 2, 2])

# Auto-refresh from session state
if "last_fetch_time" not in st.session_state:
    st.session_state.last_fetch_time = None
if "last_snap" not in st.session_state:
    st.session_state.last_snap = None
if "last_quota" not in st.session_state:
    st.session_state.last_quota = "?"
if "scores_lookup" not in st.session_state:
    st.session_state.scores_lookup = {}

with col_r:
    force_refresh = st.button("🔄 Refresh Now", type="primary", use_container_width=True)

with col_s:
    snap_now = st.button("📸 Snapshot Now", use_container_width=True,
                         help="Manually save today's plays to the history record.")

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
    with st.spinner("Fetching team totals and scores…"):
        try:
            snap, quota = fetch_snapshot()
            st.session_state.last_snap       = snap
            st.session_state.last_quota      = quota
            st.session_state.last_fetch_time = now
        except Exception as e:
            st.error(f"Fetch failed: {e}")
            st.stop()
        try:
            st.session_state.scores_lookup = fetch_scores()
        except Exception:
            pass

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
all_plays  = []   # actionable plays
game_rows  = []   # for full grid

sorted_games = sorted(
    snap.get("games", []),
    key=lambda g: g.get("commence_time", "")
)

for g in sorted_games:
    game_name = g["game"]
    ct = g["commence_time"]
    try:
        gt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(EASTERN)
        game_time = gt.strftime("%I:%M %p")
        game_dt   = gt
    except Exception:
        game_time = "?"
        game_dt   = None

    row = {"game": game_name, "away": g["away"], "home": g["home"],
           "time": game_time, "game_dt": game_dt, "sides": []}

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
        wil_under = wil.get("under")
        fan_under = fan.get("under")

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
            "wil_under":      wil_under,
            "fan_under":      fan_under,
            "streak_dir":     s_dir,
            "streak_len":     s_len,
            "signals":        signals,
        })

        if signals:
            all_alerts.append((game_name, team, game_time, signals))

        # ── Collect actionable plays ──────────────────────────────────────────
        sig_types = {s["type"] for s in signals}

        # Determine play for this side (mirrors display logic below)
        ap_bk = ap_bet = ap_line = ap_price = ap_why = ap_priority = None

        if lines_split and pin_line:
            non_pin_lines = [
                (bk, ln, ov, un) for bk, ln, ov, un in [
                    ("williamhill_us", wil_line, wil_over, wil_under),
                    ("fanatics",       fan_line, fan_over, fan_under),
                ] if ln is not None and ln != 0.5
            ]
            if non_pin_lines:
                max_entry = max(non_pin_lines, key=lambda x: x[1])
                min_entry = min(non_pin_lines, key=lambda x: x[1])
                if pin_line < max_entry[1]:
                    ap_bk, ap_line = max_entry[0], max_entry[1]
                    ap_bet   = "UNDER"
                    ap_price = max_entry[3]  # under price
                    ap_why   = f"LINE SPLIT — Pinnacle dropped to {pin_line}"
                    ap_priority = 1 if ("FULL SEQUENCE" in sig_types or "SEQUENCE BUILDING" in sig_types) else 2
                elif pin_line > min_entry[1]:
                    ap_bk, ap_line = min_entry[0], min_entry[1]
                    ap_bet   = "OVER"
                    ap_price = min_entry[2]  # over price
                    ap_why   = f"LINE SPLIT — Pinnacle raised to {pin_line}"
                    ap_priority = 1 if ("FULL SEQUENCE" in sig_types or "SEQUENCE BUILDING" in sig_types) else 2
        elif best_price_book and best_price_book[0] != "pinnacle":
            ap_bk    = best_price_book[0]
            ap_line  = best_price_book[1]
            ap_bet   = "OVER"
            ap_price = best_price_book[2]
            # Only surface as an actionable play if there's a meaningful signal
            if sig_types - {"LINE SPLIT"}:
                ap_why = " + ".join(s["type"] for s in signals if s["type"] not in ("LINE SPLIT",))
                ap_priority = 1 if "FULL SEQUENCE" in sig_types else 3

        if ap_bk and ap_price and ap_why:
            all_plays.append({
                "game":     game_name,
                "time":     game_time,
                "team":     team,
                "bet":      ap_bet,
                "line":     ap_line,
                "book":     ap_bk,
                "price":    ap_price,
                "why":      ap_why,
                "priority": ap_priority,
                "streak":   (s_dir, s_len),
            })

    game_rows.append(row)

# Save play record to history on refresh or manual snapshot
if (needs_refresh or snap_now) and fetch_time and all_plays:
    save_play_record(all_plays, fetch_time)
    if snap_now:
        st.toast(f"📸 Snapshot saved — {len(all_plays)} play(s) recorded at {fetch_time.strftime('%I:%M %p ET')}",
                 icon="✅")

# ── ACTIONABLE PLAYS ─────────────────────────────────────────────────────────

BOOK_DISPLAY = {
    "pinnacle":       "Pinnacle",
    "williamhill_us": "William Hill",
    "fanatics":       "Fanatics",
}

def _price_str(p):
    return f"{p:+d}" if p is not None else "—"

st.markdown("### 🎯 Actionable Plays")

all_plays.sort(key=lambda x: (x["priority"], x["game"]))

if not all_plays:
    st.info("No actionable plays right now. Plays appear when a line split or steam signal is detected.")
else:
    cols = st.columns(min(len(all_plays), 3))
    for idx, play in enumerate(all_plays):
        col = cols[idx % 3]
        with col:
            bet_color  = "#15803d" if play["bet"] == "OVER" else "#1d4ed8"
            bet_bg     = "#f0fdf4" if play["bet"] == "OVER" else "#eff6ff"
            bet_border = "#86efac" if play["bet"] == "OVER" else "#93c5fd"
            priority_tag = ""
            if play["priority"] == 1:
                priority_tag = '<span style="background:#dc2626;color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px;">🚨 SEQUENCE</span> '

            streak_dir, streak_len = play["streak"]
            streak_html = ""
            if streak_dir and streak_len >= 3:
                arrow = "▼" if streak_dir == "down" else "▲"
                s_color = "#dc2626" if streak_dir == "down" else "#16a34a"
                streak_html = (f'<span style="background:{s_color};color:#fff;font-size:10px;'
                               f'font-weight:600;padding:2px 5px;border-radius:3px;margin-left:4px;">'
                               f'{arrow}×{streak_len}</span>')

            price_str = _price_str(play["price"])
            book_name = BOOK_DISPLAY.get(play["book"], play["book"])

            st.markdown(
                f'<div style="background:{bet_bg};border:1.5px solid {bet_border};'
                f'border-radius:10px;padding:14px 16px;margin-bottom:8px;">'
                f'<div style="font-size:12px;color:#6b7280;margin-bottom:4px;">'
                f'{priority_tag}{play["game"]}</div>'
                f'<div style="font-size:13px;font-weight:600;color:#111827;margin-bottom:6px;">'
                f'{play["team"]}{streak_html}</div>'
                f'<div style="font-size:22px;font-weight:700;color:{bet_color};margin-bottom:2px;">'
                f'{play["bet"]} {play["line"]}</div>'
                f'<div style="font-size:14px;color:#374151;margin-bottom:6px;">'
                f'@ <strong>{book_name}</strong> &nbsp; {price_str}</div>'
                f'<div style="font-size:11px;color:#6b7280;border-top:1px solid {bet_border};'
                f'padding-top:6px;margin-top:2px;">{play["why"]}</div>'
                f'<div style="font-size:11px;color:#9ca3af;margin-top:2px;">{play["time"]} ET</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

st.markdown("---")

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
    "Each book shows its own **line, over price, and under price**. "
    "**LINE SPLIT — Pinnacle lower** (e.g. PIN=2.5, WH=3.5) = sharp UNDER steam → "
    "**BET UNDER at the book with the higher line** before they drop it. "
    "**LINE SPLIT — Pinnacle higher** = sharp OVER steam → "
    "**BET OVER at the book with the lower line**. "
    "Same line = bet the best price. "
    "Streak = consecutive same-direction Pinnacle moves."
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

def _book_row(bk_key, line, over, under, is_play, play_bet, play_reason):
    label    = BOOK_LABELS.get(bk_key, bk_key)
    line_str = str(line) if line is not None else "—"
    o_str    = _price_str(over)
    u_str    = _price_str(under)

    if is_play and play_bet == "OVER":
        row_style  = "background:#f0fdf4;"
        o_html     = f'<strong style="color:#15803d;font-size:14px;">{o_str} ✓</strong>'
        u_html     = f'<span style="font-size:13px;color:#9ca3af;">{u_str}</span>'
        tag        = (f'<span style="background:#15803d;color:#fff;font-size:10px;font-weight:700;'
                      f'padding:1px 6px;border-radius:3px;margin-left:6px;">BET OVER — {play_reason}</span>')
    elif is_play and play_bet == "UNDER":
        row_style  = "background:#eff6ff;"
        o_html     = f'<span style="font-size:13px;color:#9ca3af;">{o_str}</span>'
        u_html     = f'<strong style="color:#1d4ed8;font-size:14px;">{u_str} ✓</strong>'
        tag        = (f'<span style="background:#1d4ed8;color:#fff;font-size:10px;font-weight:700;'
                      f'padding:1px 6px;border-radius:3px;margin-left:6px;">BET UNDER — {play_reason}</span>')
    else:
        row_style  = ""
        o_html     = f'<span style="font-size:13px;">{o_str}</span>'
        u_html     = f'<span style="font-size:13px;">{u_str}</span>'
        tag        = ""

    return (f'<tr style="{row_style}">'
            f'<td style="padding:5px 10px 5px 6px;color:#6b7280;font-size:13px;white-space:nowrap;">{label}</td>'
            f'<td style="padding:5px 8px;font-size:13px;color:#374151;">{line_str}</td>'
            f'<td style="padding:5px 6px;">{o_html}</td>'
            f'<td style="padding:5px 0;">{u_html}{tag}</td>'
            f'</tr>')

scores_lookup = st.session_state.scores_lookup

for row in game_rows:
    sc = scores_lookup.get(row["game"], {})

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

                # Per-team score badge
                ts = sc.get("team_scores", {})
                team_sc = ts.get(side["team"])
                if team_sc is not None and sc.get("completed"):
                    score_html = (f'<span style="background:#1f2937;color:#f9fafb;font-size:12px;'
                                  f'font-weight:700;padding:2px 8px;border-radius:4px;margin-left:8px;">'
                                  f'✅ {team_sc} goals</span>')
                elif team_sc is not None:
                    score_html = (f'<span style="background:#065f46;color:#d1fae5;font-size:12px;'
                                  f'font-weight:700;padding:2px 8px;border-radius:4px;margin-left:8px;">'
                                  f'🟢 {team_sc}</span>')
                else:
                    score_html = ""

                # Determine the play
                lines_split    = side["lines_split"]
                best_line_book = side["best_line_book"]   # (bk, line, price) — highest line
                best_price_book= side["best_price_book"]  # (bk, line, price) — same line, best price
                pin_line       = side["pin_line"]

                play_bk        = None
                play_reason    = ""
                play_bet       = "OVER"   # default
                split_badge    = ""

                if lines_split and best_line_book:
                    # Pinnacle lower than another book → UNDER steam → bet UNDER at highest line
                    # Pinnacle higher than another book → OVER steam → bet OVER at lowest line
                    non_pin_lines = [
                        (bk, ln, ov) for bk, ln, ov in [
                            ("williamhill_us", side["wil_line"], side["wil_over"]),
                            ("fanatics",       side["fan_line"], side["fan_over"]),
                        ] if ln is not None and ln != 0.5
                    ]
                    if non_pin_lines and pin_line:
                        max_non_pin = max(non_pin_lines, key=lambda x: x[1])
                        min_non_pin = min(non_pin_lines, key=lambda x: x[1])
                        if pin_line < max_non_pin[1]:
                            # PIN lower → UNDER steam → play UNDER at highest non-pin line
                            play_bk     = max_non_pin[0]
                            play_bet    = "UNDER"
                            play_reason = f"UNDER {max_non_pin[1]} — PIN dropped to {pin_line}, bet before they follow"
                        elif pin_line > min_non_pin[1]:
                            # PIN higher → OVER steam → play OVER at lowest non-pin line
                            play_bk     = min_non_pin[0]
                            play_bet    = "OVER"
                            play_reason = f"OVER {min_non_pin[1]} — PIN raised to {pin_line}, bet before they follow"
                    split_badge = (f'<span style="background:#fef3c7;color:#92400e;font-size:11px;'
                                   f'font-weight:600;padding:2px 7px;border-radius:4px;margin-left:6px;">'
                                   f'LINE SPLIT</span>')
                elif best_price_book:
                    play_bk     = best_price_book[0]
                    play_bet    = "OVER"
                    play_reason = f"OVER {best_price_book[1]} — best price {_price_str(best_price_book[2])}"

                st.markdown(
                    f'<div style="margin-bottom:6px;font-size:15px;font-weight:600;">'
                    f'{label}{streak_html}{split_badge}{score_html}</div>',
                    unsafe_allow_html=True,
                )

                # Book rows: line, over, under — BET HERE on the play book
                books_data = [
                    ("pinnacle",       side["pin_line"], side["pin_over"],
                     side["pin_under"]),
                    ("williamhill_us", side["wil_line"], side["wil_over"],
                     side.get("wil_under")),
                    ("fanatics",       side["fan_line"], side["fan_over"],
                     side.get("fan_under")),
                ]
                header = ('<tr style="border-bottom:1px solid #e5e7eb;">'
                          '<th style="padding:3px 10px 3px 6px;font-size:11px;color:#9ca3af;font-weight:500;text-align:left;">Book</th>'
                          '<th style="padding:3px 8px;font-size:11px;color:#9ca3af;font-weight:500;text-align:left;">Line</th>'
                          '<th style="padding:3px 6px;font-size:11px;color:#9ca3af;font-weight:500;text-align:left;">Over</th>'
                          '<th style="padding:3px 0;font-size:11px;color:#9ca3af;font-weight:500;text-align:left;">Under</th>'
                          '</tr>')
                rows_html = header + "".join(
                    _book_row(bk, ln, ov, un, bk == play_bk, play_bet, play_reason)
                    for bk, ln, ov, un in books_data
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

# ── Past Plays & Results ──────────────────────────────────────────────────────

st.markdown("---")
st.markdown("### 📋 Past Plays & Results")
st.caption("Plays recorded by the daily snapshot cron (noon, 4 PM, 6 PM ET). "
           "Result shown once game is complete. Scores only available for last 3 days via API.")

import pandas as pd

# Find all dates that have history files
_avail_dates = []
for _i in range(30):
    _d = (datetime.now(tz=EASTERN) - timedelta(days=_i)).date()
    if os.path.exists(os.path.join(HISTORY_DIR, f"{_d}.strftime('%Y-%m-%d')}"
                                   .replace(".strftime('%Y-%m-%d')", "")
                                   + _d.strftime("%Y-%m-%d") + ".json"
                                   ).replace(HISTORY_DIR + os.sep + _d.strftime("%Y-%m-%d") + ".json",
                                             os.path.join(HISTORY_DIR, _d.strftime("%Y-%m-%d") + ".json"))):
        _avail_dates.append(_d)

# Simpler: just scan the directory
_avail_dates = []
if os.path.isdir(HISTORY_DIR):
    for _fname in sorted(os.listdir(HISTORY_DIR), reverse=True):
        if _fname.endswith(".json"):
            try:
                _avail_dates.append(datetime.strptime(_fname[:10], "%Y-%m-%d").date())
            except Exception:
                pass

_today = datetime.now(tz=EASTERN).date()

hcol1, hcol2 = st.columns([1, 3])
with hcol1:
    if _avail_dates:
        selected_date = st.date_input(
            "📅 Select date",
            value=_avail_dates[0],
            min_value=_avail_dates[-1] if _avail_dates else _today - timedelta(days=30),
            max_value=_today,
        )
    else:
        selected_date = st.date_input("📅 Select date", value=_today)

with hcol2:
    if _avail_dates:
        st.caption(f"History available for: {', '.join(d.strftime('%b %d') for d in _avail_dates[:10])}"
                   + (" …" if len(_avail_dates) > 10 else ""))
    else:
        st.caption("No history files yet — records appear after the first snapshot runs.")

# Load the selected date's history file
_sel_str  = selected_date.strftime("%Y-%m-%d")
_sel_path = os.path.join(HISTORY_DIR, f"{_sel_str}.json")

if not os.path.exists(_sel_path):
    st.info(f"No records for {selected_date.strftime('%B %d, %Y')}. "
            "Hit **📸 Snapshot Now** to save today's plays, or wait for the cron.")
else:
    try:
        _day_data = json.load(open(_sel_path, encoding="utf-8"))
    except Exception:
        _day_data = {}

    # Build flat play list from all snapshots in that day, deduped by (game, team, bet, line)
    _seen = set()
    _plays_for_day = []
    for _snap_time in sorted(_day_data.keys()):
        for _p in _day_data[_snap_time].get("plays", []):
            _key = (_p.get("game"), _p.get("team"), _p.get("bet"), _p.get("line"))
            if _key not in _seen:
                _seen.add(_key)
                _plays_for_day.append({**_p, "snap_time": _snap_time})

    if not _plays_for_day:
        st.info(f"No plays recorded for {selected_date.strftime('%B %d, %Y')}.")
    else:
        st.caption(f"**{len(_plays_for_day)} unique plays** detected on {selected_date.strftime('%B %d, %Y')} "
                   f"across {len(_day_data)} snapshot(s) at: {', '.join(sorted(_day_data.keys()))}")

        rows_out = []
        for rec in _plays_for_day:
            game = rec.get("game", "")
            team = rec.get("team", "")
            sc   = scores_lookup.get(game, {})
            ts   = sc.get("team_scores", {})
            team_score = ts.get(team)
            completed  = sc.get("completed", False)

            if completed and team_score is not None:
                line = float(rec.get("line", 0))
                bet  = rec.get("bet", "OVER")
                if bet == "OVER":
                    result = "✅ WIN" if team_score > line else ("🟡 PUSH" if team_score == line else "❌ LOSS")
                else:
                    result = "✅ WIN" if team_score < line else ("🟡 PUSH" if team_score == line else "❌ LOSS")
                score_str = str(team_score)
            elif ts and team_score is not None:
                result = "🟢 Live"
                score_str = str(team_score)
            else:
                result = "⏳ Pending"
                score_str = "—"

            rows_out.append({
                "Snap":   rec.get("snap_time", ""),
                "Game":   game,
                "Team":   team,
                "Bet":    f"{rec.get('bet','')} {rec.get('line','')}",
                "Book":   BOOK_DISPLAY.get(rec.get("book", ""), rec.get("book", "")),
                "Price":  _price_str(rec.get("price")),
                "Score":  score_str,
                "Result": result,
                "Why":    rec.get("why", ""),
            })

        df = pd.DataFrame(rows_out)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Summary win/loss for that day
        wins   = sum(1 for r in rows_out if r["Result"] == "✅ WIN")
        losses = sum(1 for r in rows_out if r["Result"] == "❌ LOSS")
        pushes = sum(1 for r in rows_out if r["Result"] == "🟡 PUSH")
        settled = wins + losses + pushes
        if settled:
            st.markdown(
                f'<div style="margin-top:8px;font-size:13px;color:#374151;">'
                f'<strong>Day record:</strong> &nbsp;'
                f'<span style="color:#15803d;font-weight:700;">{wins}W</span> – '
                f'<span style="color:#dc2626;font-weight:700;">{losses}L</span>'
                + (f' – <span style="color:#d97706;font-weight:700;">{pushes}P</span>' if pushes else "")
                + f'&nbsp; ({settled} settled)'
                + f'</div>',
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
