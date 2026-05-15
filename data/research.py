"""
External research sources for sanity-checking model picks.

Three free sources verified today:
  1. MLB Stats API BvP (statsapi.mlb.com vsPlayer endpoint)
  2. Reddit r/sportsbook daily MLB Props thread (community picks + upvote sentiment)
  3. Action Network public picks page (experts' picks in embedded SSR JSON)

All free, no auth. Twitter/X intentionally excluded (paywalled and noisy).
"""

import urllib.request
import ssl
# System cert chain is broken on this Windows install — use unverified
# context for these public-data APIs. No auth/PII flows through these endpoints.
_UNVERIFIED_SSL = ssl._create_unverified_context()
import urllib.error
import urllib.parse
import json
import re
from datetime import datetime, date

USER_AGENT = "mlb-research/1.0 (+research only)"
MLB_BASE = "https://statsapi.mlb.com/api/v1"


# ---------------- MLB Batter-vs-Pitcher (BvP) ----------------

def get_bvp(batter_id: int, pitcher_id: int, season: int) -> dict:
    """
    Returns career batter-vs-pitcher line via the MLB vsPlayer endpoint.
    Aggregated across all years.

    Returns dict with: ab, hits, hr, bb, k, ops, avg, slg.
    Empty dict if no encounters or API failure.
    """
    if not batter_id or not pitcher_id:
        return {}
    url = (
        f"{MLB_BASE}/people/{batter_id}/stats"
        f"?stats=vsPlayerTotal&opposingPlayerId={pitcher_id}"
        f"&group=hitting&season={season}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        data = json.loads(urllib.request.urlopen(req, timeout=10, context=_UNVERIFIED_SSL).read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {}

    splits = data.get("stats", [{}])[0].get("splits", [])
    if not splits:
        return {}
    s = splits[0].get("stat", {})
    ab = s.get("atBats", 0) or 0
    if ab == 0:
        return {}
    return {
        "ab":   ab,
        "pa":   s.get("plateAppearances", 0) or 0,
        "hits": s.get("hits", 0) or 0,
        "hr":   s.get("homeRuns", 0) or 0,
        "bb":   s.get("baseOnBalls", 0) or 0,
        "k":    s.get("strikeOuts", 0) or 0,
        "rbi":  s.get("rbi", 0) or 0,
        "avg":  s.get("avg", "—"),
        "obp":  s.get("obp", "—"),
        "slg":  s.get("slg", "—"),
        "ops":  s.get("ops", "—"),
    }


# ---------------- Reddit r/sportsbook scraper ----------------

REDDIT_HEADERS = {"User-Agent": USER_AGENT}


def find_daily_mlb_thread(target_date: date | None = None) -> dict | None:
    """
    Locates today's "MLB Props and Home Run Picks - <date>" thread in
    r/sportsbook. Returns dict {id, title, num_comments, permalink, score}
    or None if not posted yet.
    """
    target_date = target_date or date.today()
    # The mods post threads with format: "MLB Props and Home Run Picks - M/D/YY (Day)"
    # also "MLB Betting and Picks - M/D/YY (Day)"
    date_str = f"{target_date.month}/{target_date.day}"

    url = "https://www.reddit.com/r/sportsbook/hot.json?limit=30"
    try:
        req = urllib.request.Request(url, headers=REDDIT_HEADERS)
        data = json.loads(urllib.request.urlopen(req, timeout=10, context=_UNVERIFIED_SSL).read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    found = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        title = d.get("title", "")
        if "MLB" in title and ("Props" in title or "Picks" in title) and date_str in title:
            found.append({
                "id":           d.get("id"),
                "title":        title,
                "num_comments": d.get("num_comments", 0),
                "permalink":    d.get("permalink"),
                "score":        d.get("score", 0),
                "url":          f"https://www.reddit.com{d.get('permalink', '')}",
            })
    if not found:
        return None
    # Prefer "Props" (HR-focused) over generic "Betting"
    found.sort(key=lambda t: (0 if "Props" in t["title"] else 1, -t["num_comments"]))
    return found[0]


def get_reddit_top_comments(thread_permalink: str, limit: int = 15) -> list:
    """
    Pulls top-voted comments from a Reddit thread. Filters out [deleted],
    [removed], and bot/auto-mod comments.

    Returns list of {score, body, author, age_h}, sorted by score desc.
    """
    if not thread_permalink:
        return []
    url = f"https://www.reddit.com{thread_permalink}.json?sort=top&limit={limit}"
    try:
        req = urllib.request.Request(url, headers=REDDIT_HEADERS)
        body = urllib.request.urlopen(req, timeout=12, context=_UNVERIFIED_SSL).read()
        data = json.loads(body)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    if len(data) < 2:
        return []
    comments = data[1].get("data", {}).get("children", [])
    out = []
    for c in comments:
        cd = c.get("data", {})
        text = cd.get("body", "").strip()
        score = cd.get("score", 0)
        author = cd.get("author", "—")
        ts = cd.get("created_utc", 0)
        if not text or text in ("[deleted]", "[removed]") or score < 1:
            continue
        if author in ("AutoModerator", "automoderator"):
            continue
        from datetime import datetime, timezone
        age_h = (datetime.now(tz=timezone.utc).timestamp() - ts) / 3600 if ts else 0
        out.append({
            "score":  score,
            "body":   text,
            "author": author,
            "age_h":  age_h,
        })
    out.sort(key=lambda c: -c["score"])
    return out


def extract_player_mentions(comments: list, player_names: list) -> dict:
    """
    Counts how many times each player is mentioned across the comments
    weighted by upvote score. Returns {player_name: weighted_score}.
    Useful for "what does the community think of my picks?" overlay.
    """
    counts = {p: 0 for p in player_names}
    for c in comments:
        body = c.get("body", "").lower()
        weight = max(1, c.get("score", 1))
        for p in player_names:
            # Match last name or last+first chunk; case-insensitive
            last_name = p.split(",")[0].strip().split()[-1].lower()
            if len(last_name) >= 4 and last_name in body:
                counts[p] += weight
    return counts


# ---------------- Action Network expert picks ----------------

def get_action_network_picks() -> list:
    """
    Scrapes the Action Network public picks page for MLB HR-related expert
    picks. Their site uses Next.js with __NEXT_DATA__ embedded JSON.

    Returns list of {expert, pick, league, body, posted_at}. Best-effort —
    returns empty list if the page structure changes.
    """
    url = "https://www.actionnetwork.com/mlb/props"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130",
            "Accept": "text/html,*/*",
        })
        body = urllib.request.urlopen(req, timeout=15, context=_UNVERIFIED_SSL).read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError):
        return []

    # Find the embedded __NEXT_DATA__ JSON
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.DOTALL)
    if not m:
        return []
    try:
        next_data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    # Walk the structure to find picks. AN's structure changes; we hunt for
    # any list of dicts with 'pick' / 'play' keys near 'mlb' or 'home_run'.
    picks = []
    def walk(obj, path=""):
        if isinstance(obj, dict):
            # Heuristic: if a dict has a 'pick' or 'play' field with a baseball context
            if (("pick" in obj or "play" in obj) and
                any(k in str(obj).lower() for k in ("home_run", "homerun", "mlb", "baseball"))):
                picks.append(obj)
            for v in obj.values():
                walk(v, path)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, path)

    walk(next_data)
    # Dedupe and trim
    seen = set()
    out = []
    for p in picks:
        sig = json.dumps(p, sort_keys=True, default=str)[:200]
        if sig in seen:
            continue
        seen.add(sig)
        out.append(p)
    return out[:30]


# ---------------- Convenience wrapper ----------------

def get_research_for_date(target_date: date | None = None) -> dict:
    """
    Pulls all research sources at once for a given date.
    Returns dict with 'reddit' (thread + comments) and 'action_network'.
    """
    target_date = target_date or date.today()
    thread = find_daily_mlb_thread(target_date)
    comments = []
    if thread:
        comments = get_reddit_top_comments(thread["permalink"], limit=15)
    return {
        "date":            target_date.isoformat(),
        "reddit_thread":   thread,
        "reddit_comments": comments,
        "action_network":  get_action_network_picks(),
    }


if __name__ == "__main__":
    import sys
    print("=== BvP test (Judge vs Skenes) ===")
    bvp = get_bvp(592450, 694973, 2026)
    print(json.dumps(bvp, indent=2, default=str))

    print("\n=== Reddit thread today ===")
    thread = find_daily_mlb_thread()
    if thread:
        print(f"  '{thread['title']}' — {thread['num_comments']} comments")
        comments = get_reddit_top_comments(thread["permalink"], limit=5)
        print(f"  top {len(comments)} comments:")
        for c in comments[:5]:
            preview = c["body"].replace("\n", " | ")[:200]
            print(f"    [{c['score']:>3}↑ /u/{c['author']}] {preview}")
    else:
        print("  No thread found for today.")

    print("\n=== Action Network ===")
    an = get_action_network_picks()
    print(f"  found {len(an)} pick objects")
    if an:
        print(f"  sample keys: {list(an[0].keys())[:8]}")
