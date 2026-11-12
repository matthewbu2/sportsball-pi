"""ESPN hidden scoreboard API -> Game records (port of the ESP32 ESPN.h).

site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard is
undocumented but stable. Its ?dates=A-B range intermittently returns 400
"Failed to get events endpoint" for long stretches; on that we fall back to
no dates and skip ranges for 30 minutes. Akamai can blacklist a User-Agent
that is hammered in a burst; changing the UA string unblocks it.
"""

import time
import unicodedata
from dataclasses import dataclass, field

import requests

SPORT_PATHS = ["", "football/nfl", "basketball/nba", "baseball/mlb", "hockey/nhl",
               "football/college-football", "basketball/mens-college-basketball"]
SPORT_LABELS = ["None", "NFL", "NBA", "MLB", "NHL", "CFB", "CBB"]
SPORT_SUBLABELS = ["", "PRO FOOTBALL", "PRO BASKETBALL", "BASEBALL", "HOCKEY",
                   "COLLEGE FOOTBALL", "COLLEGE HOOPS"]
SPORT_LOGO_URLS = [
    "",
    "https://a.espncdn.com/i/teamlogos/leagues/500/nfl.png",
    "https://a.espncdn.com/i/teamlogos/leagues/500/nba.png",
    "https://a.espncdn.com/i/teamlogos/leagues/500/mlb.png",
    "https://a.espncdn.com/i/teamlogos/leagues/500/nhl.png",
    "https://a.espncdn.com/redesign/assets/img/icons/ESPN-icon-football-college.png",
    "https://a.espncdn.com/redesign/assets/img/icons/ESPN-icon-basketball.png",
]
NUM_SPORTS = 7

PRE, IN, POST = 0, 1, 2
PLAY_OTHER, PLAY_HOMER, PLAY_DUNK, PLAY_THREE, PLAY_INT, PLAY_FUMBLE = range(6)

USER_AGENT = "sportsball-pi/1.0"
SESSION = requests.Session()
SESSION.headers["User-Agent"] = USER_AGENT


@dataclass
class Game:
    away_abbr: str = ""
    home_abbr: str = ""
    away_score: str = ""
    home_score: str = ""
    away_record: str = ""
    home_record: str = ""
    away_rank: int = 0
    home_rank: int = 0
    away_color: tuple = (0, 0, 0)    # lightened for text on black
    home_color: tuple = (0, 0, 0)
    away_fill: tuple = (0, 0, 0)     # true team color for fills
    home_fill: tuple = (0, 0, 0)
    away_logo: str = ""
    home_logo: str = ""
    sport_idx: int = 0
    slot_mask: int = 0               # bit n = matches configured team slot n
    date: str = ""                   # ISO start, sortable: "2026-10-11T17:00Z"
    state: int = PRE
    status_text: str = ""            # "Q3 7:03", "OCT 11 1P", "FINAL"
    network: str = ""
    possession: int = 0              # 0 none, 1 away, 2 home
    is_red_zone: bool = False
    yards_to_goal: int = -1
    down_dist: str = ""              # "1ST & 10"
    poss_text: str = ""              # "ND 42"
    balls: int = 0
    strikes: int = 0
    outs: int = 0
    bases: int = 0                   # bit0 1st, bit1 2nd, bit2 3rd
    last_play: int = PLAY_OTHER
    play_id: str = ""
    play_side: int = 0               # team with the ball after the play
    period: int = 0
    clock_sec: int = 0
    fav_mask: int = 0                # bit0 away, bit1 home
    line_first: int = 1
    away_line: list = field(default_factory=list)   # None = not played yet
    home_line: list = field(default_factory=list)
    away_hits: int = 0
    home_hits: int = 0
    away_errors: int = 0
    home_errors: int = 0
    away_to: int = -1
    home_to: int = -1
    distance: int = -1
    play_text: str = ""
    play_type: str = ""              # ESPN's play type ("Safety", "Pass Reception")
    away_name: str = ""              # nickname for speech: "Colts"
    home_name: str = ""
    away_long: str = ""              # "Indianapolis Colts"
    home_long: str = ""
    # Stat leaders: (side, category, player, display) -- side 0 game-wide
    # (football), 1 away, 2 home.
    leaders: list = field(default_factory=list)
    # Every leader ESPN lists, with photo: (side, stat name, player, line, headshot) -- the HD recap card
    stars: list = field(default_factory=list)
    event_id: str = ""
    away_id: str = ""
    home_id: str = ""
    odds_line: str = ""              # "MIA -19.5" (pregame)
    over_under: str = ""             # "48.5"
    away_goals_max: int = 0          # hockey: most goals by one away player
    home_goals_max: int = 0

    @property
    def key(self):
        """Identity across refreshes."""
        return f"{self.date}{self.away_abbr}@{self.home_abbr}"


# ---------------------------------------------------------------------------
# Team colors
# ---------------------------------------------------------------------------
def _hex(h):
    try:
        v = int(h, 16) if h and len(h) >= 6 else 0x444444
    except ValueError:
        v = 0x444444
    return ((v >> 16) & 255, (v >> 8) & 255, v & 255)


def _too_dark(c):
    return max(c) < 48


def pick_team_fill(primary, alt):
    c = _hex(primary or "333333")
    if _too_dark(c):
        a = _hex(alt or "333333")
        if not _too_dark(a):
            c = a
    return c


def lighten_for_panel(c):
    r, g, b = (float(v) for v in c)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if lum < 110:
        t = (110 - lum) / (255 - lum)   # blend toward white
        r, g, b = r + (255 - r) * t, g + (255 - g) * t, b + (255 - b) * t
    return (int(r), int(g), int(b))


def color_is_light(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2] > 150


# ---------------------------------------------------------------------------
# Team matching
# ---------------------------------------------------------------------------
def team_matches(team, query):
    q = (query or "").strip()
    if not q:
        return True
    ql = q.lower()
    for k in ("abbreviation", "location", "name", "shortDisplayName", "displayName"):
        if (team.get(k) or "").lower() == ql:
            return True
    if len(q) >= 2:
        for k in ("name", "abbreviation"):
            if (team.get(k) or "").lower().startswith(ql):
                return True
    return False


def fav_matches(team, fav):
    if not fav:
        return False
    if team_matches(team, fav):
        return True
    words = fav.split()
    if len(words) < 2:
        return False
    vals = {(team.get(k) or "").lower() for k in ("abbreviation", "location", "name", "shortDisplayName")}
    return all(w.lower() in vals for w in words)


def parse_favs(s):
    return [t.strip() for t in (s or "").split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def ascii_text(s):
    """Accented letters to plain ASCII (the LED font is ASCII)."""
    s = unicodedata.normalize("NFKD", s or "")
    return s.encode("ascii", "ignore").decode()


def short_network(src):
    t = (src or "").upper().split("/")[0]
    if "BIG TEN" in t:
        return "BTN"
    for suffix in (" NETWORK", " NET"):
        if suffix in t:
            return (t[:t.index(suffix)] + "N")[:5]
    return t.split(" ")[0][:5]


MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def format_kickoff(short_detail):
    """'10/25 - 1:00 PM EDT' -> 'OCT 25 1P'."""
    try:
        md, tm = short_detail.split(" - ")
        mo, d = (int(v) for v in md.split("/"))
        hm, ap = tm.split()[:2]
        hh, mm = (int(v) for v in hm.split(":"))
        if mm == 0:
            return f"{MONTHS[mo - 1]} {d} {hh}{ap[0]}"
        return f"{MONTHS[mo - 1]} {d} {hh}:{mm:02d}{ap[0]}"
    except (ValueError, IndexError):
        return short_detail.upper()[:15]


def format_live_status(sport, status_name, period, clock, short_detail):
    if sport == 3 or status_name != "STATUS_IN_PROGRESS":
        return short_detail.upper()[:15]
    reg = 2 if sport == 6 else 3 if sport == 4 else 4
    if period > reg:
        ot = period - reg
        return f"OT {clock}" if ot == 1 else f"{ot}OT {clock}"
    if sport == 6:
        return f"{period}H {clock}"
    if sport == 4:
        return f"P{period} {clock}"
    return f"Q{period} {clock}"


def dark_logo_url(url):
    """ESPN's '500-dark' variant (Iowa's default logo is black)."""
    return url.replace("/500/", "/500-dark/", 1) if "/500/" in url else url


def team_logo_url(g, home):
    u = g.home_logo if home else g.away_logo
    return dark_logo_url(u) if u else ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _num(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


_LEADER_LABELS = {"passingYards": "PASS", "rushingYards": "RUSH", "receivingYards": "REC", "points": "PTS",
                  "rebounds": "REB", "assists": "AST", "goals": "G", "pointsLeader": "PTS"}


def _add_leader(g, side, L):
    cat = _LEADER_LABELS.get(L.get("name"))
    top = (L.get("leaders") or [None])[0]
    if top:
        a = top.get("athlete") or {}
        head = a.get("headshot")
        head = head.get("href", "") if isinstance(head, dict) else (head or "")
        sside = side
        if not sside:
            tid = str((top.get("team") or a.get("team") or {}).get("id", ""))
            sside = 1 if tid and tid == g.away_id else 2 if tid and tid == g.home_id else 0
        name = ascii_text(a.get("shortName") or a.get("displayName") or "")
        if name:
            g.stars.append((sside, L.get("name") or "", name, ascii_text(top.get("displayValue") or ""), head))
    if not cat or not top or (cat == "AST" and g.sport_idx == 4):
        return
    name = ascii_text(((top.get("athlete") or {}).get("shortName")) or "")
    if name:
        g.leaders.append((side, cat, name, ascii_text(top.get("displayValue") or "")))


def parse_event(ev, sport, queries, favs):
    """queries: [(slot, text)]. Returns a Game, or None if no slot matches."""
    comp = (ev.get("competitions") or [{}])[0]
    comps = comp.get("competitors") or []
    if len(comps) < 2:
        return None
    away = next((c for c in comps if c.get("homeAway") == "away"), None)
    home = next((c for c in comps if c.get("homeAway") != "away"), None)
    if not away or not home:
        return None
    at, ht = away.get("team") or {}, home.get("team") or {}

    slot_mask = 0
    for slot, q in queries:
        if team_matches(at, q) or team_matches(ht, q):
            slot_mask |= 1 << slot
    if not slot_mask:
        return None

    g = Game(slot_mask=slot_mask, sport_idx=sport)
    g.fav_mask = (1 if any(fav_matches(at, f) for f in favs) else 0) | \
                 (2 if any(fav_matches(ht, f) for f in favs) else 0)
    g.event_id = str(ev.get("id") or "")
    g.away_id, g.home_id = str(at.get("id") or ""), str(ht.get("id") or "")
    odds = (comp.get("odds") or [{}])[0] or {}
    g.odds_line = ascii_text(odds.get("details") or "")[:12]
    ou = odds.get("overUnder")
    g.over_under = f"{ou:g}" if isinstance(ou, (int, float)) else ""
    g.away_abbr = (at.get("abbreviation") or "???")[:5]
    g.home_abbr = (ht.get("abbreviation") or "???")[:5]
    g.away_score = str(away.get("score") or "")
    g.home_score = str(home.get("score") or "")
    g.away_record = ((away.get("records") or [{}])[0].get("summary") or "")[:9]
    g.home_record = ((home.get("records") or [{}])[0].get("summary") or "")[:9]
    ar = _num((away.get("curatedRank") or {}).get("current"))
    hr = _num((home.get("curatedRank") or {}).get("current"))
    g.away_rank = ar if 1 <= ar <= 25 else 0
    g.home_rank = hr if 1 <= hr <= 25 else 0
    g.away_fill = pick_team_fill(at.get("color"), at.get("alternateColor"))
    g.home_fill = pick_team_fill(ht.get("color"), ht.get("alternateColor"))
    g.away_color = lighten_for_panel(g.away_fill)
    g.home_color = lighten_for_panel(g.home_fill)
    g.away_name = ascii_text(at.get("name") or at.get("shortDisplayName") or g.away_abbr)
    g.home_name = ascii_text(ht.get("name") or ht.get("shortDisplayName") or g.home_abbr)
    g.away_long = ascii_text(at.get("displayName") or g.away_name)
    g.home_long = ascii_text(ht.get("displayName") or g.home_name)
    for L in comp.get("leaders") or []:
        _add_leader(g, 0, L)
    for side, c in ((1, away), (2, home)):
        for L in c.get("leaders") or []:
            _add_leader(g, side, L)
            if L.get("name") == "goals" and L.get("leaders"):
                v = _num(L["leaders"][0].get("value"))
                if side == 1:
                    g.away_goals_max = v
                else:
                    g.home_goals_max = v
    g.away_logo = at.get("logo") or ""
    g.home_logo = ht.get("logo") or ""
    g.date = ev.get("date") or ""
    g.network = short_network(comp.get("broadcast") or "")

    st = ev.get("status") or {}
    stype = st.get("type") or {}
    state = stype.get("state") or "pre"
    status_name = stype.get("name") or "STATUS_SCHEDULED"
    short_detail = stype.get("shortDetail") or "TBD"
    g.state = IN if state == "in" else POST if state == "post" else PRE
    if g.state == PRE:
        g.status_text = format_kickoff(short_detail)
    elif g.state == IN:
        g.status_text = format_live_status(sport, status_name, _num(st.get("period")),
                                           st.get("displayClock") or "", short_detail)
        g.period = _num(st.get("period"))
        clk = st.get("displayClock") or ""
        if ":" in clk:
            m, s = clk.split(":", 1)
            g.clock_sec = _num(m) * 60 + _num(s)
        else:
            g.clock_sec = _num(clk)
    else:
        g.status_text = short_detail.upper()[:15]

    if g.state != PRE:
        al, hl = away.get("linescores") or [], home.get("linescores") or []
        n = max(len(al), len(hl))
        first = max(0, n - 12)
        g.line_first = first + 1
        g.away_line = [min(254, max(0, _num(al[k].get("value")))) if k < len(al) else None for k in range(first, n)]
        g.home_line = [min(254, max(0, _num(hl[k].get("value")))) if k < len(hl) else None for k in range(first, n)]
        g.away_hits, g.home_hits = _num(away.get("hits")), _num(home.get("hits"))
        g.away_errors, g.home_errors = _num(away.get("errors")), _num(home.get("errors"))

    sit = comp.get("situation") or {}
    if g.state == IN and sit:
        lp = sit.get("lastPlay") or {}
        g.play_id = str(lp.get("id") or "")
        end_team = str(((lp.get("end") or {}).get("team") or {}).get("id") or "")
        if end_team:
            g.play_side = 1 if end_team == str(at.get("id")) else 2 if end_team == str(ht.get("id")) else 0
        g.play_text = ascii_text(lp.get("text") or "")
        g.play_type = ((lp.get("type") or {}).get("text") or "")
        g.away_to = _num(sit.get("awayTimeouts"), -1)
        g.home_to = _num(sit.get("homeTimeouts"), -1)
        g.distance = _num(sit.get("distance"), -1)
        play = (lp.get("text") or "").upper()
        ptype = ((lp.get("type") or {}).get("text") or "")

        if sport == 3:
            g.balls, g.strikes, g.outs = _num(sit.get("balls")), _num(sit.get("strikes")), _num(sit.get("outs"))
            g.bases = (1 if sit.get("onFirst") else 0) | (2 if sit.get("onSecond") else 0) | \
                      (4 if sit.get("onThird") else 0)
            sd = short_detail.lower()
            g.possession = 1 if sd.startswith("top") else 2 if sd.startswith("bot") else 0
            if "HOMERED" in play or "HOME RUN" in play or "HOME RUN" in ptype.upper():
                g.last_play = PLAY_HOMER
        elif sport in (2, 6):
            if "MAKES" in play:
                if "DUNK" in play:
                    g.last_play = PLAY_DUNK
                elif "THREE POINT" in play or "3-PT" in play:
                    g.last_play = PLAY_THREE
        elif sport in (1, 5):
            poss = str(sit.get("possession") or "")
            if poss:
                g.possession = 1 if poss == str(at.get("id")) else 2 if poss == str(ht.get("id")) else 0
            g.is_red_zone = bool(sit.get("isRedZone"))
            if "Interception" in ptype:
                g.last_play = PLAY_INT
            elif any(k in ptype for k in ("Fumble Recovery (Opponent)", "Opp Fumble Recovery", "Fumble Return")):
                g.last_play = PLAY_FUMBLE
            dd = (sit.get("shortDownDistanceText") or "").upper()
            if "GOAL" in dd:
                dd = dd[:dd.index("GOAL") + 1]      # "1ST & GOAL" -> "1ST & G"
            g.down_dist = dd[:11]
            g.poss_text = (sit.get("possessionText") or "").upper()[:9]
            parts = g.poss_text.rsplit(" ", 1)
            yl = _num(parts[-1])
            if yl > 0 and g.possession:
                poss_abbr = g.away_abbr if g.possession == 1 else g.home_abbr
                own = len(parts) == 2 and parts[0] == poss_abbr.upper()
                g.yards_to_goal = 100 - yl if own else yl
    return g


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
_range_failed_at = 0.0
RANGE_RETRY_S = 30 * 60


class FetchError(Exception):
    pass


def _get(sport, dates, **extra):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{SPORT_PATHS[sport]}/scoreboard"
    params = {"dates": dates} if dates else {}
    params.update(extra)
    r = SESSION.get(url, params=params, timeout=15)
    return r


def _fav_extra_events(sport, dates, favs, have_ids):
    """College football's default scoreboard is only the featured/ranked
    games; a favorite's game against an unranked team is only in the full
    FBS list (groups=80). Those events, favorites only."""
    if sport != 5 or not favs:
        return []
    try:
        r = _get(sport, dates, groups=80, limit=400)
        if r.status_code != 200:
            return []
        evs = r.json().get("events") or []
    except (requests.RequestException, ValueError):
        return []
    out = []
    for ev in evs:
        if str(ev.get("id")) in have_ids:
            continue
        comps = ((ev.get("competitions") or [{}])[0].get("competitors") or [])
        if any(fav_matches(c.get("team") or {}, f) for c in comps for f in favs):
            out.append(ev)
    return out


def fetch(sport, queries, favs, dates="", cutoff="", exact=False):
    """Games of one sport matching the configured team queries.

    dates: "YYYYMMDD" or "YYYYMMDD-YYYYMMDD"; cutoff: drop games starting at or
    after this ISO time (ESPN's no-dates fallback returns football's whole week).
    exact: always ask for these dates (one day; no fallback to the current week).
    """
    global _range_failed_at
    use_range = bool(dates) and (exact or not _range_failed_at or time.time() - _range_failed_at > RANGE_RETRY_S)
    r = _get(sport, dates if use_range else "")
    if r.status_code == 400 and use_range and not exact:
        print(f"[ESPN] {SPORT_LABELS[sport]} date range rejected, retrying without it", flush=True)
        _range_failed_at = time.time()
        r = _get(sport, "")
    elif r.status_code == 200 and use_range and not exact:
        _range_failed_at = 0
    if r.status_code != 200:
        raise FetchError(f"{SPORT_LABELS[sport]} HTTP {r.status_code}")
    games = []
    events = r.json().get("events") or []
    events += _fav_extra_events(sport, dates if use_range else "", favs, {str(e.get("id")) for e in events})
    for ev in events:
        if cutoff and (ev.get("date") or "") >= cutoff:
            continue
        g = parse_event(ev, sport, queries, favs)
        if g:
            games.append(g)
    return games
