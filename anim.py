"""Game-moment animations (port of the ESP32 Anim.h).

After each refresh, every game that was live at the previous refresh is
compared with it, and each moment gets a full-panel scene:
  score        a cutscene (touchdown, field goal, home run, dunk, three,
               goal) or a flashing call, then the scorer's logo and new
               score (basketball: dunks and threes only)
  lead change  the lead flipping sides -- a wipe from the old leader's
               color to the new one (after the score that caused it;
               basketball: final period only, standalone)
  turnover     football interception / lost fumble, in the colors of the
               team that took the ball
  final        FINAL, then the winner's logo and score

On the Pi every game gets the full scenes (the ESP32 gave non-favorite
games only a short flash). Favorites still change the story: the opponent
scoring on a favorite gets the "sad" scene, a favorite winning gets
fireworks and losing a muted final. When a burst of moments piles up
(BACKLOG), non-favorite ones fall back to the short flash so the queue
can't run minutes behind.

Scenes were drawn for the CYD's 106-LED-wide panel. Each feature keeps its
place relative to the edge or center it belongs to: a plain x stays put
relative to the left edge, CX(x) the center, RX(x) the right edge, and
SPREAD(x) scales across the width (fireworks, stars).
"""

import math
import time
from collections import namedtuple
from dataclasses import replace

import numpy as np

import panel as P
import sprites as S
from cards import draw_logo
from espn import (IN, POST, PRE, PLAY_DUNK, PLAY_FUMBLE, PLAY_HOMER, PLAY_INT, PLAY_THREE,
                  color_is_light, team_logo_url)
from logos import LOGO_SIZE
from panel import PANEL_H, PANEL_W, text_w

LAYOUT_W = 106


def CX(x):
    return x + (PANEL_W - LAYOUT_W) // 2


def RX(x):
    return x + (PANEL_W - LAYOUT_W)


def SPREAD(x):
    return x * PANEL_W // LAYOUT_W


K = P.BLACKISH            # near-black text (pure 0 is an unlit LED)

ANIM_FLASH_MS = 1800
ANIM_SCORE_MS = 2600
ANIM_FLASH_EVERY = 200
STEP_HZ = 25              # "step" (shimmer, marquee, flicker) rate, as on the ESP32
BACKLOG = 5               # queued events beyond this: non-favorite ones play short

EV_SCORE, EV_TURNOVER, EV_FINAL, EV_LEAD, EV_START, EV_UPSET, EV_HATTRICK, EV_BREAK = range(8)
ST_AUTO, ST_FULL, ST_SHORT, ST_SAD, ST_VICTORY, ST_LOSS = range(6)
KIND_NAMES = ["scored", "turnover", "final", "lead change", "start", "upset alert", "hat trick", "break"]
STYLE_NAMES = ["auto", "full", "short", "sad", "victory", "loss"]

# home: the team it's about (scored / took the ball / won / took the lead).
# tag refines a score or final: safety, two (two-point try), grandslam,
# walkoff, buzzer (basketball, last-second winner), upset (final).
Event = namedtuple("Event", "game home delta demo kind lead_change style tag", defaults=("",))


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _sign(v):
    return (v > 0) - (v < 0)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
BB_COOLDOWN_S = 3 * 60


class Detector:
    def __init__(self):
        self.snap = None          # key -> snapshot tuple, see _snap()
        self.hoops_last = {}      # key -> time of the last basketball celebration
        self.upset_alerted = set()

    def _hoops_ok(self, key):
        now = time.monotonic()
        if now - self.hoops_last.get(key, -1e9) < BB_COOLDOWN_S:
            return False
        self.hoops_last[key] = now
        return True

    @staticmethod
    def _snap(g):
        return (_int(g.away_score), _int(g.home_score), g.state, g.play_id, g.status_text,
                g.away_goals_max, g.home_goals_max)

    def detect(self, games):
        """Events since the previous call. The first call only records a baseline."""
        out = []
        if self.snap is not None:
            for g in games:
                sp = self.snap.get(g.key)
                if sp:
                    out += self._game(g, sp)
        self.snap = {g.key: self._snap(g) for g in games}
        return out

    def _game(self, g, sp):
        out = []
        old_away, old_home, old_state, old_play, old_status, old_ag, old_hg = sp
        if g.state == PRE:
            return out
        # Game start: favorites get a kickoff/tipoff/first pitch/puck drop intro.
        if old_state == PRE:
            if g.state == IN and g.fav_mask:
                out.append(Event(g, bool(g.fav_mask & 2), 0, False, EV_START, False, ST_FULL))
            return out
        if old_state != IN:
            return out

        na, nh = _int(g.away_score), _int(g.home_score)
        da, dh = na - old_away, nh - old_home
        old_lead, new_lead = _sign(old_home - old_away), _sign(nh - na)
        flip = old_lead != 0 and new_lead == -old_lead
        football = g.sport_idx in (1, 5)
        hoops = g.sport_idx in (2, 6)
        final_period = g.period >= {2: 4, 6: 2}.get(g.sport_idx, 99)

        # Turnover first, so a pick-six reads INTERCEPTION, then TOUCHDOWN.
        if football and g.last_play in (PLAY_INT, PLAY_FUMBLE) and g.play_id and g.play_id != old_play:
            home = g.play_side == 2 if g.play_side else g.possession == 2
            out.append(Event(g, home, 0, False, EV_TURNOVER, False, ST_AUTO))

        if hoops:
            # Basketball scores every possession, so only dunks, threes and
            # last-second winners celebrate (each game at most once per
            # BB_COOLDOWN_S), and lead changes count only in the final period.
            buzzer = final_period and (g.state == POST or g.clock_sec <= 2) and \
                (flip or old_lead == 0) and new_lead != 0 and (dh if new_lead > 0 else da) > 0
            big = g.last_play in (PLAY_DUNK, PLAY_THREE) and (dh > 0 or da > 0)
            late_flip = flip and final_period
            if buzzer:
                out.append(Event(g, new_lead > 0, dh if new_lead > 0 else da, False, EV_SCORE, False, ST_AUTO,
                                 "buzzer"))
            elif big and self._hoops_ok(g.key):
                out.append(Event(g, dh > 0, 2 if g.last_play == PLAY_DUNK else 3, False, EV_SCORE, late_flip,
                                 ST_AUTO))
            elif late_flip and self._hoops_ok(g.key + "^lead"):
                out.append(Event(g, new_lead > 0, 0, False, EV_LEAD, False, ST_AUTO))
        else:
            # The lead-change scene rides on the score of the team now leading.
            for home, d in ((False, da), (True, dh)):
                if d <= 0:
                    continue
                tag = ""
                if football and d == 2:
                    tag = "safety" if "safety" in g.play_type.lower() else "two"
                elif football and d == 8:
                    tag = "two"
                elif g.sport_idx == 3:
                    if home and g.state == POST and nh > na and g.period >= 9:
                        tag = "walkoff"
                    elif g.last_play == PLAY_HOMER and d >= 4:
                        tag = "grandslam"
                lead = flip and (new_lead > 0 if home else new_lead < 0)
                out.append(Event(g, home, d, False, EV_SCORE, lead, ST_AUTO, tag))

        # Hockey hat trick: a player's third goal.
        if g.sport_idx == 4:
            for home, old, new in ((False, old_ag, g.away_goals_max), (True, old_hg, g.home_goals_max)):
                if old < 3 <= new:
                    out.append(Event(g, home, 0, False, EV_HATTRICK, False, ST_FULL))

        # Upset alert: a ranked college team trailing late to an unranked or
        # lower-ranked one (once per game).
        if g.state == IN and g.sport_idx in (5, 6) and g.key not in self.upset_alerted:
            late = g.period >= (3 if g.sport_idx == 5 else 2)
            fav_side = _upset_favorite(g)
            if late and fav_side and (na > nh if fav_side == 2 else nh > na):
                self.upset_alerted.add(g.key)
                out.append(Event(g, fav_side == 1, 0, False, EV_UPSET, False, ST_FULL))

        # Halftime / intermission roundup, favorites only.
        if g.state == IN and g.fav_mask and is_break(g.status_text) and not is_break(old_status):
            out.append(Event(g, bool(g.fav_mask & 2), 0, False, EV_BREAK, False, ST_FULL))

        if g.state == POST:
            winner_home = nh > na
            upset = g.sport_idx in (5, 6) and _upset_favorite(g) == (1 if winner_home else 2)
            out.append(Event(g, winner_home, 0, False, EV_FINAL, False, ST_AUTO, "upset" if upset else ""))
        return out


def _upset_favorite(g):
    """The ranked side that would be upset by losing: 1 away, 2 home, 0 none."""
    a, h = g.away_rank or 99, g.home_rank or 99
    if a == h:
        return 0
    return 1 if a < h else 2


def is_break(status):
    s = status.upper()
    return "HALF" in s or s.startswith("END OF") or "INTERMISSION" in s


def event_style(ev, have_favs, backlog=0):
    if ev.style != ST_AUTO:
        return ev.style
    g = ev.game
    me = bool(g.fav_mask & (2 if ev.home else 1))
    them = bool(g.fav_mask & (1 if ev.home else 2))
    if ev.demo or not have_favs:
        return ST_FULL
    full = ST_FULL if backlog <= BACKLOG or me or them else ST_SHORT
    if ev.kind == EV_SCORE:
        if ev.tag in ("buzzer", "walkoff", "grandslam"):
            return ST_SAD if them and not me else ST_FULL
        return ST_FULL if me else ST_SAD if them else full
    if ev.kind == EV_TURNOVER:
        return ST_FULL if me or them else full
    if ev.kind == EV_FINAL:
        if g.away_score == g.home_score:
            return full
        return ST_VICTORY if me else ST_LOSS if them else full
    return ST_FULL


# ---------------------------------------------------------------------------
# Shared scenery
# ---------------------------------------------------------------------------
def anim_hash(a, b, c):
    h = (a * 73856093 ^ b * 19349663 ^ c * 83492791) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0x5bd1e995) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def shade(c):
    return S.shade(c)


def team_pal(g, home):
    """Jersey in the team's true color; helmet in the lightened text color so
    a dark-uniformed player still stands out on grass or ice."""
    return S.Pal(g.home_fill if home else g.away_fill, g.home_color if home else g.away_color)


def fill_of(g, home):
    return g.home_fill if home else g.away_fill


def color_of(g, home):
    return g.home_color if home else g.away_color


def abbr_of(g, home):
    return g.home_abbr if home else g.away_abbr


def score_of(g, home):
    return g.home_score if home else g.away_score


_FANS = [P.RED, P.WHITE, P.YELLOW, P.c565(0x041F), P.ORANGE, P.LTGREY]


# Fan seats: odd rows 1-11, every other column, staggered by row pair.
_SEAT_Y, _SEAT_X = np.array([(y, x) for y in range(1, 12, 2) for x in range((y // 2) % 2, PANEL_W, 2)],
                            dtype=np.uint32).T
_FAN_RGB = np.array(_FANS, dtype=np.uint8)


def _hash_np(a, b, c):
    """anim_hash() over uint32 arrays."""
    h = a * np.uint32(73856093) ^ b * np.uint32(19349663) ^ c * np.uint32(83492791)
    h ^= h >> np.uint32(13)
    h *= np.uint32(0x5bd1e995)
    h ^= h >> np.uint32(15)
    return h


_crowd_cache = {}


def _crowd_rows(phase, team):
    """The 13 stand rows for one shimmer phase, as an RGB array (cached)."""
    key = (phase, team)
    rows = _crowd_cache.get(key)
    if rows is None:
        if len(_crowd_cache) > 64:
            _crowd_cache.clear()
        rows = np.empty((13, PANEL_W, 3), dtype=np.uint8)
        rows[:] = (40, 30, 50)
        with np.errstate(over="ignore"):
            h = _hash_np(_SEAT_X, _SEAT_Y, np.uint32(phase) + (_SEAT_X * 7 + _SEAT_Y) % 3)
        lit = h % 4 != 0
        col = _FAN_RGB[(h >> 4) % 6]
        col[(h >> 8) % 5 == 0] = team
        rows[_SEAT_Y[lit], _SEAT_X[lit]] = col[lit]
        _crowd_cache[key] = rows
    return rows


def draw_crowd(cv, step, team, y0=0, wall=None):
    """Stands full of fans (13 rows from y0) that shimmer, plus a 2-row wall."""
    cv.bitmap(0, y0, _crowd_rows(step // 3, tuple(team)))
    cv.fill(0, y0 + 13, PANEL_W, 2, wall or (20, 40, 120))


# The panel edge clockwise from the top-left, as index arrays.
_EDGE = [(x, 0) for x in range(PANEL_W)] + [(PANEL_W - 1, y) for y in range(1, PANEL_H)] + \
        [(x, PANEL_H - 1) for x in range(PANEL_W - 2, -1, -1)] + [(0, y) for y in range(PANEL_H - 2, 0, -1)]
_EDGE_X = np.array([p[0] for p in _EDGE])
_EDGE_Y = np.array([p[1] for p in _EDGE])


def draw_marquee(cv, step, c):
    """Chasing marquee lights around the panel edge: every 3rd LED lit."""
    k = (3 - step % 3) % 3
    cv.fb[_EDGE_Y[k::3], _EDGE_X[k::3]] = c


def shadow_text(cv, cx, y, s, c, size):
    cv.text_center(cx + 1, y + 1, s, K, size)
    cv.text_center(cx, y, s, c, size)


def blink(t):
    return P.RED if (t // 150) % 2 else P.YELLOW


def draw_firework(cv, cx, cy, age, color):
    """A ring of sparks expanding from (cx, cy) and fading out."""
    if age > 900:
        return
    r = age // 50 if age < 500 else 10
    c = color if age < 600 else shade(color)
    for dx, dy in ((10, 0), (9, 5), (5, 9), (0, 10), (-5, 9), (-9, 5),
                   (-10, 0), (-9, -5), (-5, -9), (0, -10), (5, -9), (9, -5)):
        cv.pixel(cx + int(dx * r / 10), cy + int(dy * r / 10), c)
        if r > 3:
            cv.pixel(cx + int(dx * (r - 2) / 10), cy + int(dy * (r - 2) / 10), shade(c))
    if age < 120:
        cv.fill(cx - 1, cy - 1, 3, 3, P.WHITE)


# ---------------------------------------------------------------------------
# Touchdown: runner breaks away down a striped field with a defender in
# pursuit, crosses into the scoring team's end zone, raises the ball, spikes
# it -- with a blinking TOUCHDOWN banner.
# ---------------------------------------------------------------------------
TD_RUN_MS = 2100
TD_SCENE_MS = 3700
TD_ENDZONE = 250


def touchdown_scene(cv, g, home, t, step):
    me, them = team_pal(g, home), team_pal(g, not home)
    ez = fill_of(g, home)
    abbr = abbr_of(g, home)
    rt = min(t, TD_RUN_MS)
    runner_x = 40 + rt * 11 // 100
    cam = min(runner_x - 40, TD_ENDZONE - 36) - RX(0)

    draw_crowd(cv, step, ez)
    for x in range(PANEL_W):
        wx = x + cam
        if wx >= TD_ENDZONE:
            c = ez
        else:
            c = (30, 120, 40) if int(wx / 10) % 2 else (40, 140, 50)
        cv.fill(x, 15, 1, PANEL_H - 15, c)
        if wx == TD_ENDZONE or (wx < TD_ENDZONE and wx % 20 == 0):
            cv.fill(x, 15, 1, PANEL_H - 15, P.WHITE)
        elif wx < TD_ENDZONE and wx % 4 == 0:
            cv.pixel(x, 32, P.WHITE)
            cv.pixel(x, 66, P.WHITE)
    cv.fill(0, 15, PANEL_W, 1, P.WHITE)
    ez_screen = TD_ENDZONE - cam
    size = 1 if len(abbr) > 3 else 2
    if ez_screen < PANEL_W:
        cv.text_center(ez_screen + 52, 44 if size == 2 else 48, abbr, K if color_is_light(ez) else P.WHITE, size)

    rx, ry = runner_x - cam, 48
    if t < TD_RUN_MS:
        f = (t // 110) % 2
        S.draw(cv, rx - 20 - t // 120, ry + 6, S.FB_RUN2 if f else S.FB_RUN1, them)
        S.draw(cv, rx, ry, S.FB_RUN1 if f else S.FB_RUN2, me)
    else:
        ct = t - TD_RUN_MS
        S.draw(cv, rx - 20 - TD_RUN_MS // 120, ry + 6, S.FB_RUN2, them)
        if ct < 500:
            S.draw(cv, rx, ry, S.FB_CELEBRATE, me)
        else:
            S.draw(cv, rx, ry, S.FB_SPIKE, me)
            bt = ct - 500
            bx, floor_y = rx + 9 + bt // 40, ry + 12
            if bt < 150:
                by = ry + 2 + bt * (floor_y - ry - 2) // 150
            elif bt < 450:
                u = bt - 150
                by = floor_y - u * (300 - u) // 900
            elif bt < 650:
                u = bt - 450
                by = floor_y - u * (200 - u) // 2000
            else:
                by = floor_y
            S.draw(cv, bx, by, S.FB_BALL, me)
        cv.text(CX(1), 19, "TOUCHDOWN", K, 2)
        cv.text(CX(0), 18, "TOUCHDOWN", blink(ct), 2)


# ---------------------------------------------------------------------------
# Field goal / extra point, from behind the kicker.
# ---------------------------------------------------------------------------
FG_KICK_MS = 700
FG_FLIGHT_MS = 1200
FG_SCENE_MS = 3400
_FG_LINES = [79, 68, 59, 52, 46, 41, 37, 34, 31]


def field_goal_scene(cv, g, home, t, step):
    me = team_pal(g, home)
    ez = fill_of(g, home)
    draw_crowd(cv, step, ez)
    for i in range(8):
        c = (30, 120, 40) if i % 2 else (40, 140, 50)
        cv.fill(0, _FG_LINES[i + 1], PANEL_W, _FG_LINES[i] - _FG_LINES[i + 1], c)
        cv.fill(0, _FG_LINES[i + 1], PANEL_W, 1, P.WHITE)
    cv.fill(0, 15, PANEL_W, 16, ez)
    cv.fill(0, 30, PANEL_W, 1, P.WHITE)
    for y in range(31, PANEL_H):
        inset = (PANEL_H - y) // 3
        cv.pixel(inset, y, P.WHITE)
        cv.pixel(PANEL_W - 1 - inset, y, P.WHITE)
    gold = (250, 210, 40)
    cv.fill(CX(52), 22, 2, 9, gold)
    cv.fill(CX(40), 22, 26, 2, gold)
    cv.fill(CX(40), 3, 2, 19, gold)
    cv.fill(CX(64), 3, 2, 19, gold)

    kicked = t >= FG_KICK_MS
    ft = t - FG_KICK_MS if kicked else 0
    good = kicked and ft >= FG_FLIGHT_MS
    S.draw(cv, CX(53), 60, S.KB_HOLDER, me)
    if not kicked:
        S.draw(cv, CX(26) + t * 16 // FG_KICK_MS, 58, S.KB_WALK2 if (t // 140) % 2 else S.KB_WALK1, me)
        cv.fill(CX(55), 58, 2, 3, (150, 75, 25))
    else:
        S.draw(cv, CX(42), 58, S.KB_KICK, me)
    if kicked and (not good or ft < FG_FLIGHT_MS + 500):
        u = 1.0 if good else ft / FG_FLIGHT_MS
        bx, by = CX(55) + int(u * 3), 57 - int(u * 46)
        size = 5 if u < 0.3 else 4 if u < 0.65 else 3
        cv.fill(bx - size // 2 - 1, by - 1, size + 2, size + 2, K)
        cv.fill(bx - size // 2, by, size, size, (170, 85, 30))
        if step % 2:
            cv.fill(bx - size // 2 + 1, by + size // 2, max(size - 2, 1), 1, P.WHITE)
    if good:
        gt = ft - FG_FLIGHT_MS
        S.draw(cv, CX(74), 18, S.REF_GOOD, me)
        shadow_text(cv, PANEL_W // 2, 40, "IT'S", blink(gt), 2)
        shadow_text(cv, PANEL_W // 2, 58, "GOOD!", blink(gt), 2)


# ---------------------------------------------------------------------------
# Home run, at a night ballpark.
# ---------------------------------------------------------------------------
HR_PITCH_MS = 300
HR_SWING_MS = 750
HR_OUT_MS = 1500
HR_SCENE_MS = 4000


def home_run_scene(cv, g, home, t, step, call=("HOME", "RUN!")):
    me, them = team_pal(g, home), team_pal(g, not home)
    cv.fill(0, 0, PANEL_W, 18, (10, 12, 40))
    for i in range(SPREAD(14)):
        cv.pixel((i * 37 + 11) % PANEL_W, (i * 13 + 3) % 16, (200, 200, 160))
    draw_crowd(cv, step, fill_of(g, home), 18, (20, 90, 40))
    cv.fill(0, 31, PANEL_W, 1, (240, 200, 40))
    for y in range(32, 58):
        cv.fill(0, y, PANEL_W, 1, (30, 120, 40) if (y // 4) % 2 else (40, 140, 50))
    cv.fill(0, 58, PANEL_W, PANEL_H - 58, (150, 100, 60))
    cv.fill(18, 72, 7, 2, P.WHITE)
    cv.fill(19, 74, 5, 1, P.WHITE)
    cv.fill(21, 75, 1, 1, P.WHITE)
    cv.fill(CX(70), 60, 16, 3, (170, 120, 75))
    S.draw(cv, CX(73), 47, S.PITCHER, them, flip=True)
    bat = S.BAT_STANCE if t < HR_SWING_MS else S.BAT_SWING if t < HR_OUT_MS + 300 else S.BAT_TROT
    S.draw(cv, 8, 58, bat, me)
    if HR_PITCH_MS <= t < HR_SWING_MS:
        u = (t - HR_PITCH_MS) / (HR_SWING_MS - HR_PITCH_MS)
        cv.fill(CX(74) - int(u * (CX(74) - 22)), 54 + int(u * 11), 2, 2, P.WHITE)
    elif HR_SWING_MS <= t < HR_OUT_MS:
        u = (t - HR_SWING_MS) / (HR_OUT_MS - HR_SWING_MS)
        bx = 24 + int(u * (RX(94) - 24))
        by = 64 - int(u * 58) - int(u * (1 - u) * 30)
        sz = 3 if u < 0.4 else 2 if u < 0.75 else 1
        cv.fill(bx, by, sz, sz, P.WHITE)
        if u < 0.2:
            cv.text(18, 50, "*", P.YELLOW)
    if t >= HR_OUT_MS:
        ft = t - HR_OUT_MS
        draw_firework(cv, SPREAD(22), 9, ft, color_of(g, home))
        for dl, x, y, c in ((350, 78, 7, P.YELLOW), (700, 50, 11, P.RED), (1100, 90, 12, P.CYAN),
                            (1400, 12, 6, P.WHITE)):
            if ft > dl:
                draw_firework(cv, SPREAD(x), y, ft - dl, c)
        shadow_text(cv, PANEL_W // 2, 34, call[0], blink(ft), 2)
        shadow_text(cv, PANEL_W // 2, 52, call[1], blink(ft), 2)


# ---------------------------------------------------------------------------
# Basketball: slam dunk, or a three from deep.
# ---------------------------------------------------------------------------
DK_RUN_MS = 1000
DK_AIR_MS = 500
DK_SCENE_MS = 3600
TH_RELEASE_MS = 900
TH_FLIGHT_MS = 1000
TH_SCENE_MS = 3600


def draw_ball(cv, x, y):
    cv.fill(x, y, 4, 4, (235, 110, 20))
    for px, py in ((x, y), (x + 3, y), (x, y + 3), (x + 3, y + 3)):
        cv.pixel(px, py, (0, 0, 0))
    cv.fill(x + 1, y + 1, 2, 1, (90, 40, 10))


def draw_court(cv, g, home, step, shake, ripple):
    team = fill_of(g, home)
    draw_crowd(cv, step, team)
    cv.fill(0, 15, PANEL_W, PANEL_H - 15, (200, 150, 90))
    for y in range(17, PANEL_H, 4):
        cv.fill(0, y, PANEL_W, 1, (170, 120, 70))
        for x in range(((y // 4) % 2) * 9, PANEL_W, 18):
            cv.pixel(x, y + 2, (170, 120, 70))
    cv.fill(RX(76), 44, 20, PANEL_H - 44, team)
    cv.fill(RX(75), 44, 1, PANEL_H - 44, P.WHITE)
    cv.fill(RX(75), 44, 21, 1, P.WHITE)
    for y in range(27, PANEL_H):
        dy = y - 52
        if abs(dy) <= 25:
            cv.pixel(RX(90) - int(2 * math.sqrt(26 * 26 - dy * dy)), y, P.WHITE)
    sx = (1 if step % 2 else -1) if shake else 0
    cv.fill(RX(100), 30, 2, 28, (120, 120, 120))
    cv.fill(RX(96) + sx, 16, 3, 20, P.WHITE)
    cv.fill(RX(97) + sx, 24, 1, 6, P.RED)
    cv.fill(RX(86), 32, 10, 1, (240, 90, 20))
    for y in range(33, 40):
        inset = (y - 33) // 2
        wob = (y + ripple) % 2 if ripple > 0 else 0
        for x in range(RX(86) + inset + wob, RX(96) - inset, 2):
            cv.pixel(x, y, P.WHITE)


def hoops_scene(cv, g, home, dunk, t, step):
    me = team_pal(g, home)
    floor_y = 64
    if dunk:
        scored = t >= DK_RUN_MS + DK_AIR_MS
        st = t - DK_RUN_MS - DK_AIR_MS if scored else 0
        draw_court(cv, g, home, step, scored and st < 600, st // 60 if scored and st < 900 else 0)
        if t < DK_RUN_MS:
            px = -6 + t * (RX(54) + 6) // DK_RUN_MS
            S.draw(cv, px, floor_y - 15, S.BB_RUN2 if (t // 120) % 2 else S.BB_RUN1, me)
            b = (t // 60) % 4
            draw_ball(cv, px + 11, floor_y - 8 + (b * 3 if b < 2 else (4 - b) * 3))
        elif not scored:
            u = (t - DK_RUN_MS) / DK_AIR_MS
            px, py = RX(54) + int(u * 26), floor_y - 15 - int(u * 34)
            S.draw(cv, px, py, S.BB_JUMP, me)
            draw_ball(cv, px + 8, py - 3)
        else:
            py = floor_y - 49 if st < 1100 else floor_y - 15
            S.draw(cv, RX(80), py, S.BB_JUMP, me)
            if st < 500:
                draw_ball(cv, RX(89), 33 + st // 50)
            else:
                draw_ball(cv, RX(89) + (st - 500) // 120, floor_y - 4)
            tx = RX(70) // 2
            shadow_text(cv, tx, 20, "SLAM", blink(st), 2)
            shadow_text(cv, tx, 38, "DUNK!", blink(st), 2)
    else:
        land = TH_RELEASE_MS + TH_FLIGHT_MS
        swish = t >= land
        st = t - land if swish else 0
        draw_court(cv, g, home, step, False, st // 60 if swish and st < 900 else 0)
        air = TH_RELEASE_MS - 300 <= t < TH_RELEASE_MS + 250
        py = floor_y - 15 - (6 if air else 0)
        if t < TH_RELEASE_MS - 300:
            spr = S.BB_RUN2 if (t // 150) % 2 else S.BB_RUN1
        else:
            spr = S.BB_JUMP
        S.draw(cv, 8, py, spr, me)
        if t < TH_RELEASE_MS - 300:
            b = (t // 75) % 4
            draw_ball(cv, 19, floor_y - 8 + (b * 3 if b < 2 else (4 - b) * 3))
        elif t < TH_RELEASE_MS:
            draw_ball(cv, 16, py - 3)
        elif not swish:
            u = (t - TH_RELEASE_MS) / TH_FLIGHT_MS
            bx = 16 + int(u * (RX(89) - 16))
            by = (py - 3) + int(u * (29 - (py - 3))) - int(u * (1 - u) * 120)
            draw_ball(cv, bx, by)
        elif st < 400:
            draw_ball(cv, RX(89), 30 + st // 40)
        if swish:
            shadow_text(cv, PANEL_W // 2, 20, "FROM", blink(st), 2)
            shadow_text(cv, PANEL_W // 2, 38, "DOWNTOWN!", blink(st), 2)


# ---------------------------------------------------------------------------
# Hockey goal.
# ---------------------------------------------------------------------------
GL_SHOT_MS = 1400
GL_PUCK_MS = 250
GL_SCENE_MS = 3500


def goal_scene(cv, g, home, t, step):
    me, them = team_pal(g, home), team_pal(g, not home)
    draw_crowd(cv, step, fill_of(g, home))
    cv.fill(0, 15, PANEL_W, 1, P.WHITE)
    cv.fill(0, 16, PANEL_W, PANEL_H - 16, (205, 220, 235))
    cv.fill(18, 16, 3, PANEL_H - 16, (30, 60, 200))
    cv.fill(RX(92), 16, 1, PANEL_H - 16, (220, 30, 30))
    for y in range(40, 61):
        half = 8 - abs(y - 50) * 8 // 10
        if half > 0:
            cv.fill(RX(92) - half, y, half, 1, (140, 190, 240))
    cv.fill(RX(93), 42, 9, 1, P.RED)
    cv.fill(RX(93), 58, 9, 1, P.RED)
    cv.fill(RX(101), 42, 1, 17, P.RED)
    for y in range(43, 58):
        for x in range(RX(94), RX(101)):
            if (x + y) % 2 == 0:
                cv.pixel(x, y, (120, 120, 120))

    scored = t >= GL_SHOT_MS + GL_PUCK_MS
    light = scored and (t // 120) % 2 == 0
    cv.fill(RX(95), 17, 5, 4, P.RED if light else (90, 20, 20))
    if light:
        for k in range(3):
            cv.pixel(RX(93) - k, 16 + k, P.YELLOW)
            cv.pixel(RX(101) + k, 16 + k, P.YELLOW)
    S.draw(cv, RX(78), 42, S.HK_SKATE2 if scored else S.HK_SKATE1, them, flip=True)
    sy = 44
    if t < GL_SHOT_MS:
        sx = -14 + t * (RX(46) + 14) // GL_SHOT_MS
        S.draw(cv, sx, sy, S.HK_SKATE2 if (t // 150) % 2 else S.HK_SKATE1, me)
        cv.fill(sx + 13, sy + 13, 2, 1, K)
    else:
        sx = min(RX(46) + (t - GL_SHOT_MS) // 80, RX(60))
        S.draw(cv, sx, sy, S.HK_CELEBRATE if scored else S.HK_SKATE1, me)
        pt = t - GL_SHOT_MS
        px0, py0 = RX(46) + 13, sy + 13
        px = RX(97) if scored else px0 + (RX(97) - px0) * pt // GL_PUCK_MS
        py = 50 if scored else py0 + int((50 - py0) * pt / GL_PUCK_MS)
        cv.fill(px, py, 2, 1, K)
    if scored:
        c = blink(t - GL_SHOT_MS)
        cv.text(CX(11), 19, "GOAL!", K, 3)
        cv.text(CX(10), 18, "GOAL!", c, 3)


# ---------------------------------------------------------------------------
# Turnover, final, lead change, sad, victory, loss
# ---------------------------------------------------------------------------
TO_CALL_MS = 1800
_ROWS, _COLS = np.mgrid[0:PANEL_H, 0:PANEL_W]
TO_SCENE_MS = 3600


def turnover_scene(cv, g, home, interception, t, step):
    fill, color, lost = fill_of(g, home), color_of(g, home), fill_of(g, not home)
    abbr = abbr_of(g, home)
    if t < TO_CALL_MS:
        # Diagonal stripes of both teams' colors sliding across.
        cv.fill(0, 0, PANEL_W, PANEL_H, lost)
        cv.fb[:PANEL_H][(_COLS - _ROWS // 2 - (t // 25) % 6 + 2) % 6 < 3] = fill
        tc = P.YELLOW if (t // 150) % 2 else P.WHITE
        if interception:
            shadow_text(cv, PANEL_W // 2, 18, "PICKED", tc, 2)
            shadow_text(cv, PANEL_W // 2, 40, "OFF!", tc, 2)
        else:
            shadow_text(cv, PANEL_W // 2, 18, "FUMBLE!", tc, 2)
            shadow_text(cv, PANEL_W // 2, 42, "RECOVERED", tc, 1)
        cv.fill(0, 64, PANEL_W, 11, K)
        shadow_text(cv, PANEL_W // 2, 66, "TURNOVER", P.RED, 1)
    else:
        draw_logo(cv, CX(3), 8, team_logo_url(g, home), 2)
        cv.text(CX(56), 14, abbr, color, 2 if text_w(abbr, 2) <= 48 else 1)
        cv.text(CX(56), 36, "BALL!", P.YELLOW if (t // 300) % 2 else P.WHITE, 2)
        cv.text_center(PANEL_W // 2, 66, "INTERCEPTION" if interception else "FUMBLE RECOVERY", P.LTGREY)
    draw_marquee(cv, step, color)


FN_CALL_MS = 1400
FN_SCENE_MS = 4400


def final_scene(cv, g, home, t, step):
    tie = g.away_score == g.home_score
    fill = P.GREY if tie else fill_of(g, home)
    color = P.WHITE if tie else color_of(g, home)
    if t < FN_CALL_MS:
        on = (t // 200) % 2 == 0
        if on:
            cv.fill(0, 0, PANEL_W, PANEL_H, fill)
        shadow_text(cv, PANEL_W // 2, 30, "FINAL", P.WHITE if on else P.YELLOW, 3)
    elif tie:
        draw_logo(cv, 4, 6, team_logo_url(g, False))
        draw_logo(cv, PANEL_W - 4 - LOGO_SIZE, 6, team_logo_url(g, True))
        cv.text_center(PANEL_W // 2, 12, "TIE", P.YELLOW, 2)
        cv.text_center(PANEL_W // 2, 50, f"{g.away_abbr} {g.away_score}-{g.home_score} {g.home_abbr}", P.WHITE)
    else:
        abbr = abbr_of(g, home)
        draw_logo(cv, CX(3), 4, team_logo_url(g, home), 2)
        cv.text(CX(56), 8, abbr, color, 2 if text_w(abbr, 2) <= 48 else 1)
        cv.text(CX(56), 28, "WINS!", P.YELLOW if (t // 300) % 2 else P.WHITE, 2)
        cv.text_center(PANEL_W // 2, 58, f"{score_of(g, home)}-{score_of(g, not home)}", P.WHITE, 2)
    draw_marquee(cv, step, color)


LD_WIPE_MS = 700
LD_SCENE_MS = 2600


def lead_scene(cv, g, home, t, step):
    wipe = PANEL_W if t >= LD_WIPE_MS else t * PANEL_W // LD_WIPE_MS
    cv.fill(0, 0, PANEL_W, PANEL_H, fill_of(g, not home))
    cv.fill(0, 0, wipe, PANEL_H, fill_of(g, home))
    if wipe < PANEL_W:
        cv.fill(wipe, 0, 2, PANEL_H, P.WHITE)
    tc = P.YELLOW if (t // 150) % 2 else P.WHITE
    shadow_text(cv, PANEL_W // 2, 10, "LEAD", tc, 2)
    shadow_text(cv, PANEL_W // 2, 30, "CHANGE!", tc, 2)
    if t >= LD_WIPE_MS:
        cv.fill(0, 56, PANEL_W, 13, K)
        cv.text_center(PANEL_W // 2, 59, f"{abbr_of(g, home)} LEADS {score_of(g, home)}-{score_of(g, not home)}",
                       P.WHITE)
    draw_marquee(cv, step, color_of(g, home))


SD_SCENE_MS = 3400


def sad_scene(cv, g, scorer_home, t, step):
    fav_home = not scorer_home
    cv.fill(0, 0, PANEL_W, PANEL_H, (12, 14, 28))
    for x in range(2, PANEL_W, 5):
        h = anim_hash(x, 7, 3)
        y = (t // 12 + h % PANEL_H) % (PANEL_H + 6) - 6
        cv.fill(x, y, 1, 4, (70, 80, 110))
    fav_color = color_of(g, fav_home)
    word = "UGH."
    x = PANEL_W // 2 - text_w(word, 3) // 2
    for i, ch in enumerate(word):
        lt = t - 300 - i * 250
        sag = 0 if lt <= 0 else min(14, lt // 60)
        cv.text(x + 1, 9 + sag + 1, ch, K, 3)
        cv.text(x, 9 + sag, ch, fav_color, 3)
        x += 18
    cv.text_center(PANEL_W // 2, 52, f"{abbr_of(g, scorer_home)} SCORES", P.LTGREY)
    cv.text_center(PANEL_W // 2, 66, f"{g.away_abbr} {g.away_score}  {g.home_abbr} {g.home_score}", P.GREY)


VC_SCENE_MS = 5600
LO_SCENE_MS = 3200
_BURSTS = [(20, 14, 0), (84, 10, 250), (52, 6, 500), (12, 40, 800), (94, 44, 1000), (60, 30, 1300),
           (30, 8, 1600), (78, 26, 1850), (46, 46, 2100), (16, 22, 2400), (88, 12, 2700), (54, 18, 3000)]
_OTHERS = [P.YELLOW, P.WHITE, P.CYAN, P.ORANGE]


def victory_scene(cv, g, home, t, step):
    if t >= FN_CALL_MS:
        ft = t - FN_CALL_MS
        for k, (x, y, dl) in enumerate(_BURSTS):
            if ft >= dl:
                draw_firework(cv, SPREAD(x), y, (ft - dl) % 1600,
                              _OTHERS[k // 2 % 4] if k % 2 else color_of(g, home))
    final_scene(cv, g, home, t, step)


def loss_scene(cv, g, winner_home, t, step):
    fav_home = not winner_home
    draw_logo(cv, CX(3), 4, team_logo_url(g, fav_home), 2)
    cv.text(CX(56), 8, "FINAL", P.GREY)
    cv.text(CX(56), 22, "LOSS", P.LTGREY, 2)
    cv.text_center(PANEL_W // 2, 58, f"{score_of(g, fav_home)}-{score_of(g, not fav_home)}", P.GREY, 2)


# ---------------------------------------------------------------------------
# Building a moment's scene sequence
# ---------------------------------------------------------------------------
def scoring_call(sport, delta):
    if sport in (1, 5):
        if delta >= 6:
            return "TOUCH", "DOWN!"
        if delta == 3:
            return "FIELD", "GOAL!"
        if delta == 2:
            return "TWO", "POINTS"
        return "SCORE!", ""
    if sport == 4:
        return "GOAL!", ""
    if sport == 3:
        return ("RUN", "SCORES!") if delta == 1 else (f"{delta} RUNS", "SCORE!")
    return "SCORE!", ""


def flash_scene(g, home, delta, marquee, lines=None):
    fill, color = fill_of(g, home), color_of(g, home)
    on_fill = K if color_is_light(fill) else P.WHITE
    l1, l2 = lines or scoring_call(g.sport_idx, delta)
    abbr = abbr_of(g, home)

    def draw(cv, t, step):
        on = (t // ANIM_FLASH_EVERY) % 2 == 0
        if on:
            cv.fill(0, 0, PANEL_W, PANEL_H, fill)
        tc = on_fill if on else color
        y1 = 14 if l2 else 24
        cv.text_center(PANEL_W // 2, y1, l1, tc, 2)
        if l2:
            cv.text_center(PANEL_W // 2, y1 + 20, l2, tc, 2)
        cv.text_center(PANEL_W // 2, 64, abbr, on_fill if on else P.YELLOW)
        if marquee:
            draw_marquee(cv, step, P.YELLOW)
    return draw


def score_card_scene(g, home):
    """Big logo, new score blinking, opponent and clock below."""
    def draw(cv, t, step):
        draw_logo(cv, CX(3), 8, team_logo_url(g, home), 2)
        cv.text(CX(55), 12, abbr_of(g, home), color_of(g, home), 2)
        cv.text(CX(55), 32, score_of(g, home), P.YELLOW if (t // 300) % 2 == 0 else P.WHITE, 3)
        cv.text(6, 70, f"{abbr_of(g, not home)} {score_of(g, not home)}", P.GREY)
        cv.text_right(PANEL_W - 6, 70, g.status_text, P.GREEN)
        draw_marquee(cv, step, P.YELLOW)
    return draw


START_SCENE_MS = 3600
_START_CALL = ["", "KICKOFF!", "TIPOFF!", "PLAY BALL!", "PUCK DROP!", "KICKOFF!", "TIPOFF!"]


def start_scene(cv, g, home, t, step):
    """Game start: both logos slide in to meet at VS, then the sport's call."""
    slide = min(t, 900) / 900
    lx = int(-30 + slide * (PANEL_W // 2 - 44 + 30))
    rx = int(PANEL_W + slide * (PANEL_W // 2 + 44 - LOGO_SIZE * 2 - PANEL_W))
    cv.fill(0, 0, PANEL_W // 2, 50, shade(shade(g.away_fill)))
    cv.fill(PANEL_W // 2, 0, PANEL_W - PANEL_W // 2, 50, shade(shade(g.home_fill)))
    draw_logo(cv, lx - LOGO_SIZE, 2, team_logo_url(g, False), 2)
    draw_logo(cv, rx, 2, team_logo_url(g, True), 2)
    if t >= 900:
        shadow_text(cv, PANEL_W // 2, 19, "VS", P.WHITE if (t // 200) % 2 else P.YELLOW, 1)
        if t < 1100:
            cv.fill(PANEL_W // 2 - 1, 0, 2, 50, P.WHITE)   # the clash
    if t >= 1300:
        shadow_text(cv, PANEL_W // 2, 56, _START_CALL[g.sport_idx], blink(t - 1300), 2)
    draw_marquee(cv, step, P.YELLOW)


UPSET_SCENE_MS = 3800
_HAZARD = (250, 150, 0)


def upset_scene(cv, g, fav_home, t, step):
    """Upset alert: hazard stripes, UPSET ALERT, who's trailing whom."""
    cv.fb[:PANEL_H][(_COLS + _ROWS - (t // 30)) % 12 < 6] = _HAZARD
    cv.fill(4, 8, PANEL_W - 8, 44, K)
    on = (t // 250) % 2 == 0
    cv.text_center(PANEL_W // 2, 11, "UPSET", P.RED if on else P.ORANGE, 2)
    cv.text_center(PANEL_W // 2, 30, "ALERT!", P.RED if on else P.ORANGE, 2)
    cv.fill(4, 56, PANEL_W - 8, 18, K)
    rank = g.home_rank if fav_home else g.away_rank
    line = f"#{rank} {abbr_of(g, fav_home)} {score_of(g, fav_home)}  {abbr_of(g, not fav_home)} " \
           f"{score_of(g, not fav_home)}"
    cv.text_center(PANEL_W // 2, 58, line, P.WHITE)
    cv.tiny(PANEL_W // 2 - P.tiny_w(g.status_text) // 2, 67, g.status_text, P.LTGREY)


HT_SCENE_MS = 4200
_HAT = ["..KKK..", ".KHHHK.", ".KHHHK.", "KKKKKKK"]
_HAT_COLORS = [(20, 20, 20), (200, 30, 30), (30, 60, 200), (230, 230, 230), (40, 140, 60), (230, 180, 30)]


def hattrick_scene(cv, g, home, t, step):
    """Hat trick: hats rain onto the ice."""
    draw_crowd(cv, step, fill_of(g, home))
    cv.fill(0, 15, PANEL_W, 1, P.WHITE)
    cv.fill(0, 16, PANEL_W, PANEL_H - 16, (205, 220, 235))
    cv.fill(PANEL_W // 2 - 1, 16, 2, PANEL_H - 16, (220, 30, 30))
    for i in range(40):
        h = anim_hash(i, 11, 5)
        delay = (h % 2400)
        if t < delay:
            continue
        x = int(h >> 8) % (PANEL_W - 6)
        land = 30 + (h >> 16) % (PANEL_H - 36)
        y = min(land, 2 + (t - delay) // 18)
        col = _HAT_COLORS[(h >> 4) % len(_HAT_COLORS)]
        S.draw(cv, x, y, _HAT, S.Pal(col, col))
    if t > 600:
        shadow_text(cv, PANEL_W // 2, 18, "HAT", blink(t), 2)
        shadow_text(cv, PANEL_W // 2, 36, "TRICK!", blink(t), 2)
        cv.fill(0, 66, PANEL_W, 11, K)
        cv.text_center(PANEL_W // 2, 68, f"{abbr_of(g, home)} {score_of(g, home)}  "
                       f"{abbr_of(g, not home)} {score_of(g, not home)}", P.WHITE)


BZ_SCENE_MS = 3600


def buzzer_scene(cv, g, home, t, step):
    """Buzzer beater: dark arena, the clock frozen at 0.0 blinking red."""
    draw_crowd(cv, step, fill_of(g, home))
    cv.fill(0, 15, PANEL_W, PANEL_H - 15, (20, 16, 24))
    on = (t // 160) % 2 == 0
    box = (PANEL_W // 2 - 34, 18, 68, 30)
    cv.fill(*box, (60, 60, 60))
    cv.fill(box[0] + 2, box[1] + 2, box[2] - 4, box[3] - 4, K)
    cv.text_center(PANEL_W // 2, 22, "0.0", P.RED if on else (90, 0, 0), 3)
    if t < 1500:   # horn: light bars pulsing from the clock
        for k in range(4):
            r = (t // 60 + k * 6) % 24
            cv.fill(box[0] - 4 - r, 26 + k * 4, 3, 2, P.YELLOW)
            cv.fill(box[0] + box[2] + 1 + r, 26 + k * 4, 3, 2, P.YELLOW)
    shadow_text(cv, PANEL_W // 2, 54, "BUZZER BEATER!", blink(t), 1)
    cv.text_center(PANEL_W // 2, 68, f"{abbr_of(g, home)} {score_of(g, home)}  "
                   f"{abbr_of(g, not home)} {score_of(g, not home)}", P.WHITE)


# ---------------------------------------------------------------------------
# Halftime / intermission roundup: the break, this game's leaders, then
# around the league.
# ---------------------------------------------------------------------------
BREAK_TITLE_MS = 2600
BREAK_LEADERS_MS = 5000
BREAK_PAGE_MS = 4000


def break_title_scene(cv, g, t, step):
    title = "HALFTIME" if "HALF" in g.status_text.upper() else "INTERMISSION" if g.sport_idx == 4 else "BREAK"
    cv.fill(0, 0, PANEL_W // 2, PANEL_H, shade(g.away_fill))
    cv.fill(PANEL_W // 2, 0, PANEL_W - PANEL_W // 2, PANEL_H, shade(g.home_fill))
    shadow_text(cv, PANEL_W // 2, 6, title, P.YELLOW, 2 if text_w(title, 2) < PANEL_W - 4 else 1)
    draw_logo(cv, 14, 28, team_logo_url(g, False))
    draw_logo(cv, PANEL_W - 14 - LOGO_SIZE, 28, team_logo_url(g, True))
    shadow_text(cv, PANEL_W // 2, 32, f"{g.away_score}-{g.home_score}", P.WHITE, 2)
    shadow_text(cv, PANEL_W // 2, 62, f"{g.away_abbr}  {g.status_text}  {g.home_abbr}", P.LTGREY, 1)
    draw_marquee(cv, step, P.YELLOW)


def _break_leaders(g):
    if any(side == 0 for side, *_ in g.leaders):
        return [L for L in g.leaders if L[0] == 0][:4]
    rows = []
    for cat in ("PTS", "G", "REB", "AST"):
        rows += [L for L in g.leaders if L[1] == cat]
    return rows[:4]


def break_leaders_scene(cv, g, t, step):
    cv.text(2, 1, "LEADERS", P.ORANGE)
    cv.text_right(PANEL_W - 2, 1, f"{g.away_abbr} {g.away_score}-{g.home_score} {g.home_abbr}", P.WHITE)
    cv.fill(0, 9, PANEL_W, 1, (0, 70, 110))
    for i, (side, cat, name, value) in enumerate(_break_leaders(g)):
        y = 13 + i * 17
        if i * 400 > t:
            break
        col = P.WHITE if side == 0 else color_of(g, side == 2)
        cv.text(2, y, cat, P.ORANGE)
        x = 32
        if side:
            cv.text(x, y, abbr_of(g, side == 2), col)
            x += text_w(abbr_of(g, side == 2)) + 6
        cv.text(x, y, name.upper()[:(PANEL_W - x) // 6], P.WHITE)
        cv.tiny(32, y + 9, value.upper().replace(", ", " "), P.LTGREY)


def break_page_scene(games, page):
    rows = games[page * 4:page * 4 + 4]

    def draw(cv, t, step):
        cv.text(2, 1, "AROUND THE LEAGUE", P.ORANGE)
        cv.fill(0, 9, PANEL_W, 1, (0, 70, 110))
        for i, o in enumerate(rows):
            y = 14 + i * 17
            post = o.state == POST
            aw, hw = _int(o.away_score) > _int(o.home_score), _int(o.home_score) > _int(o.away_score)
            cv.text(2, y, o.away_abbr, o.away_color)
            cv.text_right(46, y, o.away_score, P.GREY if post and hw else P.YELLOW if not post else P.WHITE)
            cv.text(54, y, o.home_abbr, o.home_color)
            cv.text_right(98, y, o.home_score, P.GREY if post and aw else P.YELLOW if not post else P.WHITE)
            st = o.status_text[:9]
            cv.text_right(PANEL_W - 2, y, st, P.ORANGE if post else P.GREEN)
            cv.tiny(2, y + 9, o.away_name.upper()[:12] + " AT " + o.home_name.upper()[:12], (90, 90, 100))
    return draw


def break_sequence(g, games):
    seq = [(BREAK_TITLE_MS, lambda cv, t, s: break_title_scene(cv, g, t, s), "break")]
    if _break_leaders(g):
        seq.append((BREAK_LEADERS_MS, lambda cv, t, s: break_leaders_scene(cv, g, t, s), ""))
    others = [o for o in games if o.key != g.key and o.state == IN] + \
             sorted((o for o in games if o.key != g.key and o.state == POST), key=lambda o: o.date, reverse=True)
    others = others[:12]
    for page in range((len(others) + 3) // 4):
        seq.append((BREAK_PAGE_MS, break_page_scene(others, page), ""))
    return seq


# ---------------------------------------------------------------------------
# Building a moment's scene sequence: [(duration_ms, draw(cv, t_ms, step), sound)]
# ---------------------------------------------------------------------------
def sequence(ev, style, games=()):
    g, home = ev.game, ev.home
    lead = (LD_SCENE_MS, lambda cv, t, s: lead_scene(cv, g, home, t, s), "lead")
    if ev.kind == EV_START:
        return [(START_SCENE_MS, lambda cv, t, s: start_scene(cv, g, home, t, s), "start")]
    if ev.kind == EV_UPSET:
        return [(UPSET_SCENE_MS, lambda cv, t, s: upset_scene(cv, g, home, t, s), "upset")]
    if ev.kind == EV_HATTRICK:
        return [(HT_SCENE_MS, lambda cv, t, s: hattrick_scene(cv, g, home, t, s), "hattrick")]
    if ev.kind == EV_BREAK:
        return break_sequence(g, games)
    if ev.kind == EV_TURNOVER:
        pick = ev.delta == 1 if ev.demo else g.last_play == PLAY_INT
        return [(TO_CALL_MS if style == ST_SHORT else TO_SCENE_MS,
                 lambda cv, t, s: turnover_scene(cv, g, home, pick, t, s), "turnover")]
    if ev.kind == EV_FINAL:
        upset = [(ANIM_FLASH_MS, flash_scene(g, home, 0, True, ("UPSET!", "")), "upset")] if ev.tag == "upset" else []
        if style == ST_VICTORY:
            return [(VC_SCENE_MS, lambda cv, t, s: victory_scene(cv, g, home, t, s), "victory")] + upset
        if style == ST_LOSS:
            return [(LO_SCENE_MS, lambda cv, t, s: loss_scene(cv, g, home, t, s), "loss")]
        return [(FN_CALL_MS if style == ST_SHORT else FN_SCENE_MS,
                 lambda cv, t, s: final_scene(cv, g, home, t, s), "final")] + upset
    if ev.kind == EV_LEAD:
        return [lead]
    if style == ST_SAD:
        seq = [(SD_SCENE_MS, lambda cv, t, s: sad_scene(cv, g, home, t, s), "sad")]
        return seq + [lead] if ev.lead_change else seq
    if style == ST_SHORT:
        return [(ANIM_FLASH_MS, flash_scene(g, home, ev.delta, False), "flash")]

    football = g.sport_idx in (1, 5)
    hr = g.sport_idx == 3 and (g.last_play == PLAY_HOMER or ev.demo)
    extra = []
    if ev.tag == "safety":
        seq = [(ANIM_FLASH_MS, flash_scene(g, home, 2, True, ("SAFETY!", "")), "safety")]
    elif ev.tag == "two" and ev.delta == 2:
        seq = [(ANIM_FLASH_MS, flash_scene(g, home, 2, True, ("TWO-POINT", "GOOD!")), "flash")]
    elif football and ev.delta >= 6:
        seq = [(TD_SCENE_MS, lambda cv, t, s: touchdown_scene(cv, g, home, t, s), "td")]
        if ev.tag == "two":
            extra = [(ANIM_FLASH_MS, flash_scene(g, home, 2, True, ("TWO-POINT", "GOOD!")), "flash")]
    elif football and ev.delta in (1, 3):
        seq = [(FG_SCENE_MS, lambda cv, t, s: field_goal_scene(cv, g, home, t, s), "fg")]
    elif ev.tag == "walkoff":
        # A walk-off homer plays in full; any other walk-off joins at the fireworks.
        skip = 0 if hr and not ev.demo else HR_OUT_MS
        seq = [(HR_SCENE_MS - skip,
                lambda cv, t, s: home_run_scene(cv, g, home, t + skip, s, ("WALK", "OFF!")), "walkoff")]
    elif ev.tag == "grandslam":
        seq = [(HR_SCENE_MS, lambda cv, t, s: home_run_scene(cv, g, home, t, s, ("GRAND", "SLAM!")), "grandslam")]
    elif hr:
        seq = [(HR_SCENE_MS, lambda cv, t, s: home_run_scene(cv, g, home, t, s), "hr")]
    elif ev.tag == "buzzer":
        seq = [(BZ_SCENE_MS, lambda cv, t, s: buzzer_scene(cv, g, home, t, s), "buzzer")]
    elif g.sport_idx in (2, 6):
        dunk = ev.delta == 2
        seq = [(DK_SCENE_MS if dunk else TH_SCENE_MS,
                lambda cv, t, s: hoops_scene(cv, g, home, dunk, t, s), "dunk" if dunk else "three")]
    elif g.sport_idx == 4:
        seq = [(GL_SCENE_MS, lambda cv, t, s: goal_scene(cv, g, home, t, s), "goal")]
    else:
        seq = [(ANIM_FLASH_MS, flash_scene(g, home, ev.delta, True), "flash")]
    seq += extra
    seq.append((ANIM_SCORE_MS, score_card_scene(g, home), "scorecard"))
    if ev.lead_change:
        seq.append(lead)
    return seq


class Player:
    """Plays queued moments one after another inside the frame loop.

    hooks: callables (sound, event, segment_index) run as each segment
    starts -- sound effects and the announcer."""

    def __init__(self):
        self.queue = []
        self.seq = []
        self.seg = 0
        self.start = 0.0
        self.current = None
        self.games = []
        self.hooks = []

    @property
    def active(self):
        return bool(self.seq) or bool(self.queue)

    def add(self, events):
        self.queue += events

    def _begin_segment(self, ev):
        snd = self.seq[0][2]
        for h in self.hooks:
            try:
                h(snd, ev, self.seg)
            except Exception as e:  # noqa: BLE001 - sound trouble must not stop the show
                print(f"[Anim] hook: {e}", flush=True)

    def frame(self, cv, have_favs):
        """Draws the current scene; returns the game of an event that just
        finished (so the crawl can park on its card), else None."""
        now = time.monotonic()
        done = None
        while True:
            if not self.seq:
                if not self.queue:
                    return done
                ev = self.queue.pop(0)
                style = event_style(ev, have_favs, len(self.queue))
                g = ev.game
                print(f"[Anim] {abbr_of(g, ev.home)} {KIND_NAMES[ev.kind]} {ev.delta} {ev.tag} "
                      f"({STYLE_NAMES[style]})", flush=True)
                self.current = ev
                self.seq = sequence(ev, style, self.games)
                self.seg = 0
                self.start = now
                self._begin_segment(ev)
            dur, draw, _snd = self.seq[0]
            t = int((now - self.start) * 1000)
            if t < dur:
                cv.clear()
                draw(cv, t, int((now - self.start) * STEP_HZ))
                return done
            self.seq.pop(0)
            self.start += dur / 1000
            if self.seq:
                self.seg += 1
                self._begin_segment(self.current)
            else:
                done = self.current.game
                self.current = None


DEMOS = {   # name: (sport it belongs to or 0 = any, kind, delta, style, tag)
    "td": (1, EV_SCORE, 7, ST_AUTO, ""), "fg": (1, EV_SCORE, 3, ST_AUTO, ""), "hr": (3, EV_SCORE, 1, ST_AUTO, ""),
    "goal": (4, EV_SCORE, 1, ST_AUTO, ""), "dunk": (2, EV_SCORE, 2, ST_AUTO, ""),
    "three": (2, EV_SCORE, 3, ST_AUTO, ""), "int": (1, EV_TURNOVER, 1, ST_AUTO, ""),
    "fumble": (1, EV_TURNOVER, 0, ST_AUTO, ""), "lead": (0, EV_LEAD, 0, ST_AUTO, ""),
    "final": (0, EV_FINAL, 0, ST_AUTO, ""), "sad": (1, EV_SCORE, 7, ST_SAD, ""),
    "short": (1, EV_SCORE, 7, ST_SHORT, ""), "victory": (0, EV_FINAL, 0, ST_VICTORY, ""),
    "loss": (0, EV_FINAL, 0, ST_LOSS, ""),
    "safety": (1, EV_SCORE, 2, ST_AUTO, "safety"), "two": (1, EV_SCORE, 2, ST_AUTO, "two"),
    "grandslam": (3, EV_SCORE, 4, ST_AUTO, "grandslam"), "walkoff": (3, EV_SCORE, 1, ST_AUTO, "walkoff"),
    "buzzer": (2, EV_SCORE, 3, ST_AUTO, "buzzer"), "hattrick": (4, EV_HATTRICK, 0, ST_FULL, ""),
    "upset": (5, EV_UPSET, 0, ST_FULL, ""), "start": (0, EV_START, 0, ST_FULL, ""),
    "break": (0, EV_BREAK, 0, ST_FULL, ""),
}
SPORT_FAMILY = {1: (1, 5), 2: (2, 6), 3: (3,), 4: (4,), 5: (5, 6), 0: (1, 2, 3, 4, 5, 6)}


def demo_event(name, games):
    """A demo of `name` on a game of the right sport (favorites first), or None."""
    spec = DEMOS.get(name)
    if not spec or not games:
        return None
    sport, kind, delta, style, tag = spec
    fam = SPORT_FAMILY[sport]
    pick = sorted((g for g in games if g.sport_idx in fam), key=lambda g: (not g.fav_mask, g.state != IN))
    if not pick:
        return None
    g = pick[0]
    if g.state == PRE or g.away_score == g.home_score:
        # A demo on a game that hasn't started: give it a plausible score.
        lo, hi = {1: (17, 24), 5: (17, 24), 2: (95, 98), 6: (66, 71), 3: (3, 4), 4: (2, 3)}[g.sport_idx]
        home_ahead = bool(g.fav_mask & 2) != (style in (ST_SAD, ST_LOSS)) if g.fav_mask else True
        g = replace(g, away_score=str(lo if home_ahead else hi), home_score=str(hi if home_ahead else lo),
                    status_text="DEMO")
    fav_home = bool(g.fav_mask & 2) if g.fav_mask else _int(g.home_score) >= _int(g.away_score)
    if style in (ST_SAD, ST_LOSS):          # about the team beating the favorite
        home = not fav_home
    elif kind == EV_FINAL and style == ST_AUTO:
        home = _int(g.home_score) >= _int(g.away_score)
    else:
        home = fav_home
    if kind == EV_UPSET:   # the ranked side (`home` says which) trailing
        lo, hi = sorted((_int(g.away_score), _int(g.home_score)))
        rank = (g.home_rank if home else g.away_rank) or 5
        g = replace(g, home_rank=rank if home else 0, away_rank=0 if home else rank,
                    home_score=str(lo if home else hi + 1), away_score=str(hi + 1 if home else lo))
    return Event(g, home, delta, True, kind, False, style, tag)
