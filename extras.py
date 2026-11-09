"""Everything besides the scoreboard, fetched in the background:

  * the favorite teams' next game and last result (from their ESPN schedule),
    so MY TEAMS and the NEXT UP countdown work all week, not only within
    the scoreboard's 3-day window
  * standings: each favorite's NFL division / college conference
  * the AP Top 25 (when college football is on)
  * headlines: one per favorite team plus the top story per sport

snapshot() gives (favorite games, filler cards); main.py rebuilds the cards
when `version` changes. The filler cards are shown only while nothing is
live (the weekday filler).
"""

import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import cards as C
import espn
from espn import SESSION, SPORT_PATHS, ascii_text, fav_matches

REFRESH_S = 30 * 60
RESULT_DAYS = 8                 # a favorite's last result is shown this long
NEWS_PER_SPORT = 2
SITE = "https://site.api.espn.com/apis/site/v2/sports/"

_lock = threading.Lock()
_fav_games = []
_cards = []
_teams, _favs = [], []
_wake = threading.Event()
_force = False
version = 0
updated_at = 0.0                # last refresh that finished (health)
last_error = ""


def configure(teams, favs):
    """New teams/favorites from the settings: refetch everything."""
    global _teams, _favs, _force
    _teams, _favs = list(teams), list(favs)
    _force = True
    _wake.set()


def snapshot():
    with _lock:
        return list(_fav_games), list(_cards)


# ---------------------------------------------------------------------------
def _json(url, **params):
    r = SESSION.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _football():
    return [s for s in dict.fromkeys(s for s, _q in _teams) if s in (1, 5)]


def _fav_teams(sport):
    """[(team dict)] for the favorites in this sport's team list."""
    if not _favs:
        return []
    d = _json(SITE + SPORT_PATHS[sport] + "/teams", limit=1000)   # all ~760 college teams
    teams = [t["team"] for t in d["sports"][0]["leagues"][0]["teams"]]
    return [t for t in teams if any(fav_matches(t, f) for f in _favs)]


def _ymd_et(iso):
    """ESPN's scoreboard days are US Eastern."""
    t = datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    return t.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")


def _game_on(sport, event_id, iso):
    """The scoreboard Game for one event (by its date)."""
    for g in espn.fetch(sport, [(31, "")], _favs, _ymd_et(iso), exact=True):
        if g.event_id == event_id:
            return g
    return None


def _schedule_games(sport, team):
    """(last result, next game) of a favorite as Games, either may be None."""
    events = _json(SITE + SPORT_PATHS[sport] + f"/teams/{team['id']}/schedule").get("events") or []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    done = [e for e in events if ((e.get("competitions") or [{}])[0].get("status") or {})
            .get("type", {}).get("state") == "post"]
    ahead = [e for e in events if (e.get("date") or "") > now and e not in done]
    last = nxt = None
    if done:
        e = done[-1]
        age = time.time() - C.iso_to_time(e["date"])
        if age < RESULT_DAYS * 86400:
            last = _game_on(sport, str(e["id"]), e["date"])
    if ahead:
        e = ahead[0]
        nxt = _game_on(sport, str(e["id"]), e["date"])
    return last, nxt


def _standings(sport, team, seen):
    """A STANDINGS card for the team's division/conference (once each)."""
    if sport == 1:
        d = _json("https://site.api.espn.com/apis/v2/sports/football/nfl/standings", level=3)
        groups = [div for conf in d.get("children") or [] for div in conf.get("children") or []]
    else:
        info = _json(SITE + SPORT_PATHS[sport] + f"/teams/{team['id']}").get("team") or {}
        gid = (info.get("groups") or {}).get("id")
        if not gid:
            return None
        d = _json("https://site.api.espn.com/apis/v2/sports/football/college-football/standings", group=gid)
        groups = [d]
    for grp in groups:
        entries = (grp.get("standings") or {}).get("entries") or []
        if not any(str(e["team"].get("id")) == str(team["id"]) for e in entries):
            continue
        name = grp.get("abbreviation") if sport == 1 else grp.get("shortName") or grp.get("name")
        title = f"{grp.get('name', '')}" if sport == 1 else f"{name}"
        if title in seen:
            return None
        seen.add(title)
        rows = []
        for e in entries:
            t = e["team"]
            stats = {s.get("type") or s.get("name"): s.get("displayValue") or "" for s in e.get("stats") or []}
            overall = stats.get("total") or stats.get("overall") or ""
            if sport == 1:
                side = stats.get("vsdiv") or stats.get("divisionrecord") or ""
            else:
                side = stats.get("vsconf") or ""
            rows.append((ascii_text(t.get("abbreviation") or "")[:5], overall, side,
                         any(fav_matches(t, f) for f in _favs)))
        side_label = "DIV" if sport == 1 else "CONF"
        return C.Card(C.STANDINGS, sport, (ascii_text(title).upper(), side_label, rows))
    return None


def _rankings():
    """RANKS cards: the College Football Playoff committee's top 25 (from
    early November) first, then the AP poll."""
    d = _json(SITE + "football/college-football/rankings")
    polls = d.get("rankings") or []
    out = []
    for title, match in (("CFP TOP 25", lambda n: "playoff" in n or "cfp" in n), ("AP TOP 25", lambda n: n.startswith("ap"))):
        poll = next((r for r in polls if match((r.get("name") or r.get("shortName") or "").lower())), None)
        if not poll:
            continue
        rows = []
        for r in poll.get("ranks") or []:
            t = r.get("team") or {}
            rows.append((r.get("current"), ascii_text(t.get("abbreviation") or "")[:5], r.get("recordSummary") or "",
                         any(fav_matches(t, f) for f in _favs)))
        out.append(C.Card(C.RANKS, 5, (title, rows[:25])))
    return out


def _news(sport, fav_teams):
    out, seen = [], set()
    for t in fav_teams:
        arts = _json(SITE + SPORT_PATHS[sport] + "/news", team=t["id"], limit=1).get("articles") or []
        for a in arts[:1]:
            h = ascii_text(a.get("headline") or "")
            if h and h not in seen:
                seen.add(h)
                out.append(C.Card(C.NEWS, sport, (ascii_text(t.get("shortDisplayName") or t.get("name") or ""), h)))
    arts = _json(SITE + SPORT_PATHS[sport] + "/news", limit=NEWS_PER_SPORT + 2).get("articles") or []
    n = 0
    for a in arts:
        h = ascii_text(a.get("headline") or "")
        if h and h not in seen and n < NEWS_PER_SPORT:
            seen.add(h)
            out.append(C.Card(C.NEWS, sport, ("", h)))
            n += 1
    return out


def _refresh():
    global _fav_games, _cards, version
    fav_games, ranks, standings, news = [], [], [], []
    seen_groups = set()
    for sport in _football():
        try:
            favs = _fav_teams(sport)
        except Exception as e:  # noqa: BLE001
            print(f"[Extras] teams {e}", flush=True)
            favs = []
        for t in favs:
            for what, fn in (("schedule", lambda: fav_games.extend(g for g in _schedule_games(sport, t) if g)),
                             ("standings", lambda: standings.append(_standings(sport, t, seen_groups)))):
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    print(f"[Extras] {what} {t.get('abbreviation')}: {e}", flush=True)
        if sport == 5:
            try:
                ranks += _rankings()
            except Exception as e:  # noqa: BLE001
                print(f"[Extras] rankings {e}", flush=True)
        try:
            news += _news(sport, favs)
        except Exception as e:  # noqa: BLE001
            print(f"[Extras] news {e}", flush=True)
    filler = [c for c in ranks + standings if c] + news + [C.Card(C.REMOTE, 0, None)]
    keys = set()
    fav_games = [g for g in fav_games if not (g.key in keys or keys.add(g.key))]
    with _lock:
        _fav_games, _cards = fav_games, filler
        version += 1
    global updated_at
    updated_at = time.time()
    print(f"[Extras] {len(fav_games)} favorite games, {len(filler)} filler cards", flush=True)


def _loop():
    global _force
    last = 0.0
    while True:
        _wake.wait(timeout=5)
        _wake.clear()
        if _force or time.time() - last >= REFRESH_S:
            _force = False
            last = time.time()
            try:
                _refresh()
            except Exception as e:  # noqa: BLE001
                global last_error
                last_error = str(e)[:120]
                print(f"[Extras] {e}", flush=True)


def start(teams, favs):
    configure(teams, favs)
    threading.Thread(target=_loop, daemon=True).start()
