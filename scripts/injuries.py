"""
Injury lists and starting lineups from ESPN (NBA, NFL, NHL, college football, MLB).

`fetch_injuries` builds {team name: [{name, status, note, return, pos}]} (worst status first).
`fetch_lineups` reads the day's games and, where ESPN has posted them, each team's starters
(usually about 30 minutes before the game; before that the lineup is simply missing).
`find` looks a player up in a game's two teams, tolerating spelling differences with the odds feed.
"""
from scripts import best_plays as bp

PATHS = {"NBA": "basketball/nba", "NFL": "football/nfl", "NHL": "hockey/nhl",
         "CFB": "football/college-football", "MLB": "baseball/mlb"}
LABELS = {"NBA": "NBA 🏀", "NFL": "NFL 🏈", "NHL": "NHL 🏒", "CFB": "CFB 🎓", "MLB": "MLB ⚾"}
SEVERITY = {"out": 3, "doubtful": 2, "questionable": 1, "day-to-day": 1, "probable": 0}
STAYS_OFF_ALERTS = 2          # severity at or above this (Out / Doubtful) blocks a Telegram alert


def severity(status):
    s = (status or "").lower()
    if "out" in s or "injured reserve" in s or "suspend" in s:
        return 3
    for k, v in SEVERITY.items():
        if k in s:
            return v
    return 1


def parse_injuries(data):
    """ESPN injuries JSON -> {team displayName: [entries]} sorted worst first."""
    out = {}
    for t in (data or {}).get("injuries", []) or []:
        team = t.get("displayName") or (t.get("team") or {}).get("displayName") or ""
        rows = []
        for i in t.get("injuries", []) or []:
            ath = i.get("athlete") or {}
            name = ath.get("displayName") or ath.get("fullName")
            if not name:
                continue
            status = i.get("status") or (i.get("type") or {}).get("description") or ""
            rows.append({
                "name": name, "status": status,
                "pos": (ath.get("position") or {}).get("abbreviation", ""),
                "note": (i.get("shortComment") or i.get("longComment") or "")[:240],
                "return": ((i.get("details") or {}).get("returnDate") or ""),
            })
        rows.sort(key=lambda r: -severity(r["status"]))
        if team:
            out[team] = rows
    return out


def fetch_injuries(sport, fetch=bp._get_plain):
    return parse_injuries(fetch(f"{bp.ESPN_API}/{PATHS[sport]}/injuries"))


def _starters(summary):
    """{team displayName: [starter names]} from a game summary (empty until ESPN posts the lineup)."""
    out = {}
    for t in (summary.get("boxscore") or {}).get("players", []) or []:
        team = (t.get("team") or {}).get("displayName", "")
        names = []
        for grp in t.get("statistics", []) or []:
            for a in grp.get("athletes", []) or []:
                if a.get("starter"):
                    n = (a.get("athlete") or {}).get("displayName")
                    if n and n not in names:
                        names.append(n)
        if names and team:
            out[team] = names
    return out


def fetch_lineups(sport, date_str, fetch=bp._get_plain):
    """[{id, away, home, start, status, starters: {team: [names]}, probables: {team: name}}] for one day."""
    base = f"{bp.ESPN_API}/{PATHS[sport]}"
    data = fetch(f"{base}/scoreboard?dates={date_str.replace('-', '')}&limit=300")
    games = []
    for ev in data.get("events", []) or []:
        comp = (ev.get("competitions") or [{}])[0]
        g = {"id": ev.get("id"), "start": ev.get("date", ""),
             "status": ((ev.get("status") or {}).get("type") or {}).get("description", ""),
             "starters": {}, "probables": {}}
        for c in comp.get("competitors", []) or []:
            side = c.get("homeAway")
            name = (c.get("team") or {}).get("displayName", "")
            g[side] = name
            for pr in c.get("probables", []) or []:
                n = (pr.get("athlete") or {}).get("displayName")
                if n:
                    g["probables"][name] = n
        if "home" not in g or "away" not in g:
            continue
        try:
            g["starters"] = _starters(fetch(f"{base}/summary?event={g['id']}"))
        except Exception:  # noqa: BLE001
            pass
        games.append(g)
    return games


def find(teams, away, home, who):
    """The injury entry for a player in either team of a game, or None."""
    nm = bp.norm_name(who)
    ck, toks = nm.replace(" ", ""), nm.split()
    if not toks:
        return None
    for team in (away, home):
        for e in (teams or {}).get(team, []):
            en = bp.norm_name(e["name"])
            if en == nm or en.replace(" ", "") == ck:
                return e
    if len(toks) >= 2:
        for team in (away, home):
            for e in (teams or {}).get(team, []):
                t = bp.norm_name(e["name"]).split()
                if len(t) >= 2 and t[-1] == toks[-1] and min(len(t[0]), len(toks[0])) >= 3 \
                        and (t[0].startswith(toks[0]) or toks[0].startswith(t[0])):
                    return e
    return None


def play_injury(inj, p):
    """Injury entry for a play's player (props only), from {sport: {'teams': ...}}."""
    if not inj or p.get("kind") != "prop":
        return None
    away, _, home = p["game"].partition(" @ ")
    return find((inj.get(p["sport"]) or {}).get("teams"), away, home, p["who"])


COLORS = {3: "#b91c1c", 2: "#c2410c", 1: "#b45309", 0: "#6b7280"}


def badge(entry):
    """Small coloured status label, '' when there is no entry."""
    if not entry:
        return ""
    sev = severity(entry["status"])
    tip = (entry.get("note") or "").replace('"', "'")
    return (f' <span title="{tip}" style="background:{COLORS[sev]};color:#fff;font-size:10px;font-weight:700;'
            f'padding:1px 6px;border-radius:3px;">⚠️ {entry["status"]}</span>')
