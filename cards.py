"""Glance-style ticker cards (port of the ESP32 Cards.h, Waveshare 5" layout).

Each card is one panel wide (PANEL_W x 80 LEDs) and is drawn at a horizontal
offset x0, so the crawl can draw two at once while one slides out and the
next slides in.

Sequence: while anything is live, a LIVE section first -- every live game
across all sports, crunch-time games leading, then favorites. Next a MY
TEAMS section with the favorite teams' other games (recent finals, then
upcoming), led by a NEXT UP countdown card. Then for each configured sport
(in setup order) a section header card, then that sport's other games
(upcoming by start time, then finals), then a "NO GAME DATA" card for any
configured team with nothing in the fetched date range. Finals are dropped
once the game started STALE_FINAL_HOURS ago.

While nothing is live, the filler from extras.py follows: CFP and AP Top 25,
standings, headlines and a QR code for the web remote. The favorites'
next game and last result (also from extras.py) join MY TEAMS any time.
"""

import time
import textwrap
from collections import namedtuple
from datetime import datetime, timezone

import logos
import panel as P
from espn import (IN, POST, PRE, SPORT_LABELS, SPORT_LOGO_URLS, SPORT_SUBLABELS,
                  team_logo_url)
from logos import LOGO_SIZE
from panel import PANEL_W, text_w, tiny_w

HEADER, GAME, NODATA, MSG, LIVE, FAVS, NEXTUP, RANKS, STANDINGS, NEWS, REMOTE, SETUP = range(12)
STALE_FINAL_HOURS = 6

Card = namedtuple("Card", "type sport ref")   # ref: Game, team slot, or a count

ROW_AWAY_Y = 10
ROW_HOME_Y = 36
SIT_Y = 66
LINES_X = 60      # line score starts here, right of the team names

crunch_force_key = None   # demo: treat this game as crunch
SHOW_ODDS = True          # betting line on upcoming games (settings: show_odds)
REMOTE_URLS = ("", "")    # web remote by name and by IP (main.py)
# QR codes drawn this frame as (x, y, data, scale, border) in LEDs: display.py
# paints them as crisp squares over the LED dots, which phones can't scan.
OVERLAYS = []


def iso_now(offset_s=0):
    return datetime.fromtimestamp(time.time() + offset_s, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def iso_to_time(iso):
    try:
        return datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Crunch time: a close game near the end.
#   football    4th quarter, 2:00 or less, within one score (8); tied in the
#               last 5:00; any overtime
#   basketball  final period (or OT), 2:00 or less, within 5
#   hockey      3rd period, 5:00 or less, within 1; any overtime
#   baseball    9th inning on within 2, or tied from the 8th on
# ---------------------------------------------------------------------------
def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def is_crunch(g):
    if g.state != IN:
        return False
    if crunch_force_key and g.key == crunch_force_key:
        return True
    margin = abs(_int(g.away_score) - _int(g.home_score))
    p, clk = g.period, g.clock_sec
    s = g.sport_idx
    if s in (1, 5):
        return p > 4 or (p == 4 and ((clk <= 120 and margin <= 8) or (clk <= 300 and margin == 0)))
    if s == 2:
        return p >= 4 and clk <= 120 and margin <= 5
    if s == 6:
        return p >= 2 and clk <= 120 and margin <= 5
    if s == 4:
        return p > 3 or (p == 3 and clk <= 300 and margin <= 1)
    if s == 3:
        return (p >= 9 and margin <= 2) or (p >= 8 and margin == 0)
    return False


def is_stale_final(g, stale_before):
    return g.state == POST and g.date < stale_before


def live_sort_key(g):
    return (not is_crunch(g), not g.fav_mask, g.date)


def build_cards(games, teams, extra=None):
    """teams: [(sport, query)] in setup order; extra: extras.snapshot().
    Returns (cards, game_day_game)."""
    cards = []
    stale_before = iso_now(-STALE_FINAL_HOURS * 3600)
    fav_extra, filler = extra or ([], [])
    have = {g.key for g in games}
    fav_extra = [g for g in fav_extra if g.key not in have]

    live = sorted((g for g in games if g.state == IN), key=live_sort_key)
    if live:
        cards.append(Card(LIVE, 0, len(live)))
        cards += [Card(GAME, g.sport_idx, g) for g in live]
    game_day = next((g for g in live if g.fav_mask and is_crunch(g)), None) or \
        next((g for g in live if g.fav_mask), None)

    favs = [g for g in games if g.fav_mask and g.state != IN and not is_stale_final(g, stale_before)]
    favs += [g for g in fav_extra if g.state != IN]
    # A team that's playing now: its older finals (last week's score) would just confuse.
    playing = {(g.sport_idx, t) for g in live for t in (g.away_abbr, g.home_abbr)}
    favs = [g for g in favs if g.state != POST or not
            ((g.sport_idx, g.away_abbr) in playing or (g.sport_idx, g.home_abbr) in playing)]
    finals = sorted((g for g in favs if g.state == POST), key=lambda g: g.date, reverse=True)
    upcoming = sorted((g for g in favs if g.state != POST), key=lambda g: g.date)
    favs = finals + upcoming
    if favs:
        cards.append(Card(FAVS, 0, len(favs)))
        if upcoming:
            cards.append(Card(NEXTUP, upcoming[0].sport_idx, upcoming[0]))
        cards += [Card(GAME, g.sport_idx, g) for g in favs]

    done = set()
    for slot, (sport, _q) in enumerate(teams):
        if sport <= 0 or sport in done:
            continue
        done.add(sport)
        section = sorted(
            (g for g in games if g.sport_idx == sport and g.state != IN and not g.fav_mask
             and not is_stale_final(g, stale_before)),
            key=lambda g: ({PRE: 1, IN: 0, POST: 2}[g.state], g.date))
        body = [Card(GAME, sport, g) for g in section]
        for s, (sp, q) in enumerate(teams):
            if sp == sport and q.strip() and not any(g.sport_idx == sport and g.slot_mask & (1 << s) for g in games):
                body.append(Card(NODATA, sport, s))
        if body:
            cards.append(Card(HEADER, sport, None))
            cards += body

    if not live:
        cards += filler
    if not cards:
        cards.append(Card(MSG, 0, ("NO TEAMS SET", "USE THE WEB REMOTE", "")))
    return cards, game_day


def card_logos(c):
    if c.type == HEADER:
        return [SPORT_LOGO_URLS[c.sport]]
    if c.type in (GAME, NEXTUP):
        return [team_logo_url(c.ref, False), team_logo_url(c.ref, True)]
    return []


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
_STAR = ["...#...", "..###..", "#######", ".#####.", "..###..", ".##.##.", ".#...#."]


def draw_star(cv, x, y, scale=1):
    for r, row in enumerate(_STAR):
        for c, ch in enumerate(row):
            if ch == "#":
                cv.fill(x + c * scale, y + r * scale, scale, scale, P.YELLOW)


def draw_logo(cv, x, y, url, scale=1):
    img = logos.get(url)
    if img is not None:
        cv.bitmap(x, y, img, scale)


def draw_team_row(cv, x0, y, g, home):
    abbr = g.home_abbr if home else g.away_abbr
    score = g.home_score if home else g.away_score
    rec = g.home_record if home else g.away_record
    rank = g.home_rank if home else g.away_rank
    color = g.home_color if home else g.away_color
    tx = x0 + LOGO_SIZE + 4
    logo_dy, abbr_dy, sub_dy, score_dy = 1, 5, 13, 3

    draw_logo(cv, x0 + 1, y + logo_dy, team_logo_url(g, home))
    cv.text(tx, y + abbr_dy, abbr, color)

    sub = (f"#{rank} " if rank else "") + rec
    sub_x = tx
    if g.fav_mask & (2 if home else 1):
        draw_star(cv, tx, y + sub_dy - 1)
        sub_x += 9
    cv.tiny(sub_x, y + sub_dy, sub, P.LTGREY)

    if g.possession == (2 if home else 1):
        cv.fill(tx + text_w(abbr) + 2, y + abbr_dy + 2, 3, 3, P.YELLOW)

    # Football timeouts left: three bars under the record.
    to = g.home_to if home else g.away_to
    if g.state == IN and to >= 0:
        for i in range(3):
            cv.fill(tx + i * 5, y + sub_dy + 7, 4, 2, P.YELLOW if i < to else P.DIMDOT)

    if g.state != PRE and score:
        sc = P.YELLOW
        if g.state == POST:
            mine, theirs = _int(score), _int(g.away_score if home else g.home_score)
            sc = P.WHITE if mine >= theirs else P.GREY
        cv.text_right(x0 + PANEL_W - 2, y + score_dy, score, sc, 3)


def reg_periods(sport):
    return {3: 9, 4: 3, 6: 2}.get(sport, 4)


def draw_line_score(cv, x0, g):
    """One column per period between the names and the big scores (labels
    above, the current one highlighted), plus hits and errors for baseball.
    Columns that don't fit drop off the front."""
    n_lines = len(g.away_line)
    if g.state == PRE or n_lines == 0:
        return
    mlb = g.sport_idx == 3
    reg = reg_periods(g.sport_idx)
    last = max(g.line_first + n_lines - 1, reg)
    vals = [v for v in g.away_line + g.home_line if v is not None]
    max_v = max(vals) if vals else 0
    score_w = max(text_w(g.away_score, 3), text_w(g.home_score, 3))
    right = x0 + PANEL_W - 2 - score_w - 4
    extra = 3 + 8 + 5 if mlb else 0
    avail = right - (x0 + LINES_X) - extra
    cols = last - g.line_first + 1
    big = not mlb
    d_w = 11 if max_v >= 10 else 5
    col_w = d_w + 3
    if big and cols * col_w > avail:
        big = False
    if not big:
        d_w = 7 if max_v >= 10 else 3
        col_w = d_w + 2

    def gaps(frm, to):   # baseball innings grouped in threes
        return 2 * ((to - 1) // 3 - (frm - 1) // 3) if mlb else 0

    first = max(g.line_first, last - avail // col_w + 1)
    while first < last and (last - first + 1) * col_w + gaps(first, last) > avail:
        first += 1
    x1 = right - extra - (last - first + 1) * col_w - gaps(first, last)
    cur = g.line_first + n_lines - 1 if g.state == IN else -1

    cx = x1
    for p in range(first, last + 1):
        if p > first and mlb and (p - 1) % 3 == 0:
            cx += 2
        if p <= reg or mlb:
            lab = str(p % 10 if mlb else p)
        elif g.sport_idx == 4 and p == reg + 2:
            lab = "SO"
        else:
            lab = "OT"
        now = p == cur
        d_right = cx + col_w - 1
        cv.tiny(d_right - d_w // 2 - tiny_w(lab) // 2, ROW_AWAY_Y, lab, P.YELLOW if now else P.GREY)
        i = p - g.line_first
        if 0 <= i < n_lines:
            for side in (0, 1):
                v = (g.home_line if side else g.away_line)[i]
                if v is None:
                    continue
                t = str(v)
                ry = ROW_HOME_Y if side else ROW_AWAY_Y
                c = P.WHITE if now else P.LTGREY
                if big:
                    cv.text_right(d_right, ry + 5, t, c)
                else:
                    cv.tiny(d_right - tiny_w(t) + 1, ry + 6, t, c)
        cx += col_w
    if mlb:
        cx += 3
        for lab, w, a, h in (("H", 8, g.away_hits, g.home_hits), ("E", 5, g.away_errors, g.home_errors)):
            cv.tiny(cx + w - 1 - tiny_w(lab), ROW_AWAY_Y, lab, P.GREY)
            for side, v in ((0, a), (1, h)):
                t = str(v)
                cv.tiny(cx + w - 1 - tiny_w(t), (ROW_HOME_Y if side else ROW_AWAY_Y) + 6, t, P.WHITE)
            cx += w


FIELD_W = 60


def draw_field(cv, x, y, g):
    """Whole field, offense going right: its end zone left in its color, the
    defense's right, red zone shaded, ball (white) and line to gain (yellow)."""
    ez, h = 5, 7
    fw = FIELD_W - 2 * ez
    off = g.home_fill if g.possession == 2 else g.away_fill
    dfn = g.away_fill if g.possession == 2 else g.home_fill
    cv.fill(x, y, ez, h, off if any(off) else P.TEAL)
    cv.fill(x + ez + fw, y, ez, h, dfn if any(dfn) else P.TEAL)
    cv.fill(x + ez, y, fw, h, (0, 70, 20))
    if g.is_red_zone:
        cv.fill(x + ez + fw * 80 // 100, y, fw * 20 // 100, h, P.DKRED)
    for yd in range(10, 100, 10):
        for yy in range(y, y + h, 2):
            cv.pixel(x + ez + yd * fw // 100, yy, (70, 120, 80))
    ball = x + ez + (100 - g.yards_to_goal) * fw // 100
    if 0 < g.distance < g.yards_to_goal:
        cv.fill(ball + g.distance * fw // 100, y, 1, h, P.YELLOW)
    cv.fill(ball - 1, y + 1, 2, h - 2, P.WHITE)


def draw_count_dot(cv, x, y, on, c):
    col = c if on else P.DIMDOT
    cv.fill(x + 1, y, 3, 5, col)
    cv.fill(x, y + 1, 5, 3, col)


def draw_big_base(cv, cx, cy, on):
    for r in range(-3, 4):
        half = 3 - abs(r)
        if on:
            cv.fill(cx - half, cy + r, 2 * half + 1, 1, P.YELLOW)
        else:
            cv.pixel(cx - half, cy + r, P.LTGREY)
            cv.pixel(cx + half, cy + r, P.LTGREY)


def has_situation(g):
    return g.state == IN and (g.sport_idx == 3 or g.down_dist or g.yards_to_goal >= 0)


def draw_situation(cv, x0, g):
    if g.sport_idx == 3:
        x = x0 + 2
        for lab, n, on, c in (("B", 3, g.balls, P.GREEN), ("S", 2, g.strikes, P.RED), ("O", 2, g.outs, P.ORANGE)):
            cv.tiny(x, SIT_Y + 1, lab, P.LTGREY)
            x += 5
            for i in range(n):
                draw_count_dot(cv, x, SIT_Y + 1, i < on, c)
                x += 6
            x += 4
        r = x0 + PANEL_W - 6
        draw_big_base(cv, r - 7, SIT_Y + 1, g.bases & 2)    # 2nd
        draw_big_base(cv, r - 14, SIT_Y + 5, g.bases & 4)   # 3rd
        draw_big_base(cv, r, SIT_Y + 5, g.bases & 1)        # 1st
        return
    field_x = x0 + PANEL_W - 2 - FIELD_W
    if g.down_dist:
        cv.text(x0 + 2, SIT_Y, g.down_dist, P.RED if g.is_red_zone else P.WHITE)
        px = x0 + 2 + text_w(g.down_dist) + 6
        if px + text_w(g.poss_text) < field_x - 2:
            cv.text(px, SIT_Y, g.poss_text, P.LTGREY)
    if g.yards_to_goal >= 0 and g.possession:
        draw_field(cv, field_x, SIT_Y, g)


def draw_starts_in(cv, x0, g):
    t = iso_to_time(g.date)
    if not t:
        return
    s = int(t - time.time())
    if s <= 0:
        line = "STARTING SOON"
    elif s < 3600:
        line = f"STARTS IN {(s + 59) // 60}M"
    elif s < 86400:
        line = f"STARTS IN {s // 3600}H {s // 60 % 60:02d}M"
    else:
        return
    cv.text_center(x0 + PANEL_W // 2, 67, line, P.LTGREY)


_NEXTUP_LABEL = ["", "KICKOFF IN", "TIPOFF IN", "FIRST PITCH IN", "PUCK DROP IN", "KICKOFF IN", "TIPOFF IN"]


def draw_nextup_card(cv, x0, g):
    cv.fill(x0, 0, PANEL_W, 9, (70, 55, 0))
    draw_star(cv, x0 + 2, 1)
    cv.text(x0 + 11, 1, "NEXT UP", P.YELLOW)
    if g.network:
        cv.text_right(x0 + PANEL_W - 2, 1, g.network, P.CYAN)
    draw_logo(cv, x0 + 12, 12, team_logo_url(g, False))
    draw_logo(cv, x0 + PANEL_W - 12 - LOGO_SIZE, 12, team_logo_url(g, True))
    cv.text(x0 + 40, 16, g.away_abbr, g.away_color)
    cv.text_right(x0 + PANEL_W - 40, 16, g.home_abbr, g.home_color)
    cv.tiny(x0 + 40, 26, g.away_record, P.LTGREY)
    cv.tiny(x0 + PANEL_W - 40 - tiny_w(g.home_record) + 1, 26, g.home_record, P.LTGREY)
    cv.text_center(x0 + PANEL_W // 2, 18, "@", P.GREY)
    cv.text_center(x0 + PANEL_W // 2, 40, _NEXTUP_LABEL[g.sport_idx], P.ORANGE)
    t = iso_to_time(g.date)
    cd = "--:--:--"
    if t:
        s = max(0, int(t - time.time()))
        if s >= 86400:
            cd = f"{s // 86400}D {s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}"
        else:
            cd = f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"
    cv.text_center(x0 + PANEL_W // 2, 50, cd, P.WHITE, 2)
    cv.text_center(x0 + PANEL_W // 2, 70, g.status_text, P.LTGREY)


def draw_game_card(cv, x0, g):
    red_zone = g.state == IN and g.is_red_zone
    if is_crunch(g):
        # Blinks; alternates with RED ZONE when both apply.
        ph = int(time.monotonic() * 1000) // 400
        bright = ph % 2 == 0
        label = "RED ZONE" if red_zone and (ph // 4) % 2 else "CRUNCH"
        cv.fill(x0, 0, PANEL_W, 9, P.RED if bright else P.DKRED)
        cv.text(x0 + 2, 1, label, P.WHITE if bright else P.YELLOW)
        cv.text_right(x0 + PANEL_W - 2, 1, g.status_text, P.WHITE)
    elif red_zone:
        cv.fill(x0, 0, PANEL_W, 9, P.RED)
        cv.text(x0 + 2, 1, "RED ZONE", P.WHITE)
        cv.text_right(x0 + PANEL_W - 2, 1, g.status_text, P.WHITE)
    else:
        sc = P.ORANGE if g.state == POST else P.GREEN if g.state == IN else P.WHITE
        net_w = text_w(g.network) + 4 if g.network else 0
        max_chars = (PANEL_W - 2 - net_w) // 6
        status = g.status_text[:16]
        if len(status) > max_chars:   # too long: drop the network instead
            net_w = 0
            status = status[:(PANEL_W - 2) // 6]
        cv.text(x0 + 1, 1, status, sc)
        if net_w:
            cv.text_right(x0 + PANEL_W - 2, 1, g.network, P.CYAN)

    draw_team_row(cv, x0, ROW_AWAY_Y, g, False)
    draw_team_row(cv, x0, ROW_HOME_Y, g, True)
    draw_line_score(cv, x0, g)
    if g.state == PRE:
        draw_starts_in(cv, x0, g)
        if SHOW_ODDS:
            draw_odds(cv, x0, g)
    if g.state == IN and has_situation(g):
        draw_situation(cv, x0, g)


def draw_odds(cv, x0, g):
    """The betting line under the teams: "IU -7.5   O/U 42.5"."""
    parts = [(g.odds_line, P.LTGREY)] if g.odds_line and g.odds_line != "EVEN" else []
    if g.odds_line == "EVEN":
        parts = [("PICK EM", P.LTGREY)]
    if g.over_under:
        parts.append((f"O/U {g.over_under}", P.CYAN))
    if not parts:
        return
    gap = 12
    w = sum(tiny_w(t) for t, _c in parts) + gap * (len(parts) - 1)
    x = x0 + PANEL_W // 2 - w // 2
    for t, c in parts:
        cv.tiny(x, 59, t, c)
        x += tiny_w(t) + gap


def draw_header_card(cv, x0, sport):
    draw_logo(cv, x0 + PANEL_W // 2 - LOGO_SIZE, 1, SPORT_LOGO_URLS[sport], 2)
    cv.text_center(x0 + PANEL_W // 2, 53, SPORT_LABELS[sport], P.WHITE, 2)
    cv.text_center(x0 + PANEL_W // 2, 72, SPORT_SUBLABELS[sport], P.ORANGE)


def draw_live_card(cv, x0, live_count, games):
    crunch = sum(1 for g in games if is_crunch(g))
    lx = x0 + PANEL_W // 2 - 42           # light + gap + "LIVE" is 84 wide
    cv.fill(lx, 14, 9, 9, P.RED)          # "on air" light
    cv.text(lx + 15, 8, "LIVE", P.RED, 3)
    cv.text_center(x0 + PANEL_W // 2, 44, f"{live_count} GAME{'' if live_count == 1 else 'S'}", P.WHITE)
    if crunch:
        cv.text_center(x0 + PANEL_W // 2, 60, f"{crunch} CRUNCH TIME", P.ORANGE)


def draw_favs_card(cv, x0, count):
    draw_star(cv, x0 + PANEL_W // 2 - 14, 4, 4)
    cv.text_center(x0 + PANEL_W // 2, 40, "MY TEAMS", P.YELLOW, 2)
    cv.text_center(x0 + PANEL_W // 2, 64, f"{count} GAME{'' if count == 1 else 'S'}", P.WHITE)


def draw_nodata_card(cv, x0, sport, name):
    cv.text(x0 + 1, 1, SPORT_LABELS[sport], P.ORANGE)
    cv.text_center(x0 + PANEL_W // 2, 24, name.upper()[:8], P.WHITE, 2)
    cv.text_center(x0 + PANEL_W // 2, 50, "NO GAME DATA", P.CYAN)


def draw_msg_card(cv, x0, lines):
    ys = (22, 36, 50)
    for i, line in enumerate(lines[:3]):
        cv.text_center(x0 + PANEL_W // 2, ys[i], line, P.ORANGE if i == 0 else P.WHITE)


def _title_bar(cv, x0, left, right="", color=P.ORANGE):
    cv.text(x0 + 2, 1, left, color)
    if right:
        cv.text_right(x0 + PANEL_W - 2, 1, right, P.CYAN)


def draw_ranks_card(cv, x0, ref):
    """A top 25 (AP or the playoff committee's) in three columns; favorites in yellow."""
    title, rows = ref
    _title_bar(cv, x0, title, "COLLEGE FOOTBALL")
    col_w = PANEL_W // 3
    per = (len(rows) + 2) // 3
    for i, (rank, abbr, rec, fav) in enumerate(rows):
        cx, y = x0 + (i // per) * col_w, 12 + (i % per) * 7
        r = str(rank)
        cv.tiny(cx + 8 - tiny_w(r), y, r, P.GREY)
        cv.tiny(cx + 11, y, abbr, P.YELLOW if fav else P.WHITE)
        cv.tiny(cx + col_w - 4 - tiny_w(rec), y, rec, P.LTGREY)


def draw_standings_card(cv, x0, sport, ref):
    title, side_label, rows = ref
    _title_bar(cv, x0, title[:18], SPORT_LABELS[sport])
    if len(rows) <= 6:      # NFL division: big rows
        rx1, rx2 = x0 + PANEL_W - 52, x0 + PANEL_W - 4
        cv.tiny(rx1 - tiny_w("W-L") + 1, 12, "W-L", P.GREY)
        cv.tiny(rx2 - tiny_w(side_label) + 1, 12, side_label, P.GREY)
        for i, (abbr, overall, side, fav) in enumerate(rows):
            y = 20 + i * 14
            cv.text(x0 + 2, y, str(i + 1), P.GREY)
            cv.text(x0 + 14, y, abbr, P.YELLOW if fav else P.WHITE)
            if fav:
                draw_star(cv, x0 + 16 + text_w(abbr), y)
            cv.text_right(rx1, y, overall, P.WHITE)
            cv.text_right(rx2, y, side, P.LTGREY)
        return
    col_w = PANEL_W // 2
    per = (len(rows) + 1) // 2
    pitch = 7 if per <= 9 else 6
    for col in range(2):
        cx = x0 + col * col_w
        cv.tiny(cx + col_w - 30 - tiny_w("W-L") + 1, 11, "W-L", P.GREY)
        cv.tiny(cx + col_w - 5 - tiny_w(side_label) + 1, 11, side_label, P.GREY)
    for i, (abbr, overall, side, fav) in enumerate(rows):
        cx, y = x0 + (i // per) * col_w, 18 + (i % per) * pitch
        r = str(i + 1)
        cv.tiny(cx + 8 - tiny_w(r), y, r, P.GREY)
        cv.tiny(cx + 11, y, abbr, P.YELLOW if fav else P.WHITE)
        cv.tiny(cx + col_w - 30 - tiny_w(overall) + 1, y, overall, P.WHITE)
        cv.tiny(cx + col_w - 5 - tiny_w(side) + 1, y, side, P.LTGREY)


def wrap_lines(text, width, max_lines):
    lines = textwrap.wrap(text.upper(), width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:width - 3].rstrip() + "..."
    return lines


def draw_news_card(cv, x0, sport, ref):
    team, headline = ref
    _title_bar(cv, x0, "HEADLINES" if not team else team.upper()[:14], SPORT_LABELS[sport],
               P.YELLOW if team else P.ORANGE)
    for i, line in enumerate(wrap_lines(headline, (PANEL_W - 4) // 6, 6)):
        cv.text(x0 + 2, 14 + i * 11, line, P.WHITE)


_qr_cache = {}


def qr_matrix(data):
    m = _qr_cache.get(data)
    if m is None:
        import numpy as np
        import segno
        q = segno.make(data, error="l", micro=False)
        m = _qr_cache[data] = np.array([[bool(v) for v in row] for row in q.matrix], dtype=bool)
    return m


def draw_qr(cv, x, y, data, scale=2, border=2):
    """A QR code: dark modules unlit on a white field (and a crisp overlay
    of it for the screen, see OVERLAYS). Returns its size."""
    m = qr_matrix(data)
    size = (m.shape[0] + 2 * border) * scale
    cv.fill(x, y, size, size, P.WHITE)
    cv.mask(x + border * scale, y + border * scale, m.repeat(scale, 0).repeat(scale, 1), (0, 0, 0))
    if x < P.PANEL_W and x + size > 0:
        OVERLAYS.append((x, y, data, scale, border))
    return size


def qr_image(data, scale, border):
    """The QR as an RGB array in LEDs (white field, black modules)."""
    import numpy as np
    m = np.pad(qr_matrix(data), border)
    img = np.where(m[:, :, None], 0, 255).astype(np.uint8).repeat(3, 2)
    return img.repeat(scale, 0).repeat(scale, 1)


def draw_qr_card(cv, x0, data, lines):
    """QR on the left, up to 6 (text, color, tiny) lines on the right."""
    size = draw_qr(cv, x0 + 3, 0, data)
    size = min(size, P.PANEL_H)
    tx = x0 + 3 + size + 6
    y = max(2, (P.PANEL_H - sum(10 if not tiny else 8 for _t, _c, tiny in lines)) // 2)
    for text, color, tiny in lines:
        if tiny:
            cv.tiny(tx, y + 1, text, color)
            y += 8
        else:
            cv.text(tx, y, text, color)
            y += 10


def draw_remote_card(cv, x0):
    by_name, by_ip = REMOTE_URLS
    lines = [("SCAN FOR THE", P.WHITE, False), ("WEB REMOTE", P.YELLOW, False), ("", P.WHITE, True),
             ("TEAMS, SOUND, DEMOS", P.LTGREY, True)]
    if by_name:
        lines.append((by_name.replace("http://", "").upper(), P.CYAN, True))
    if by_ip:
        lines.append((by_ip.replace("http://", "").upper(), P.CYAN, True))
    draw_qr_card(cv, x0, by_ip or by_name, lines)


def draw_setup_card(cv, x0):
    """First run: a QR to the phone setup page (teams, favorites, sound)."""
    by_name, by_ip = REMOTE_URLS
    url = (by_ip or by_name) + "/setup"
    draw_qr_card(cv, x0, url, [("WELCOME!", P.YELLOW, False), ("SCAN TO PICK", P.WHITE, False),
                               ("YOUR TEAMS", P.WHITE, False), ("", P.WHITE, True),
                               ("POINT YOUR PHONE", P.CYAN, True), ("CAMERA AT THE CODE", P.CYAN, True)])


def draw_wifi_card(cv, x0, st):
    """The Wi-Fi setup hotspot: a QR that joins it, then what to do."""
    data = f"WIFI:T:nopass;S:{st['ssid']};;"
    if st.get("first"):     # brand-new ticker: say hello first
        draw_qr_card(cv, x0, data, [
            ("WELCOME TO", P.WHITE, True), ("SPORTSBALL!", P.YELLOW, False), ("SCAN TO START", P.WHITE, False),
            ("", P.WHITE, True), (st["ssid"].upper(), P.CYAN, True),
            ("NO PASSWORD", P.CYAN, True)])
        return
    draw_qr_card(cv, x0, data, [
        ("WI-FI SETUP", P.ORANGE, False), ("SCAN TO JOIN", P.WHITE, False),
        (st["ssid"].upper(), P.CYAN, True), ("NO PASSWORD", P.CYAN, True),
        ("THEN PICK THIS", P.LTGREY, True), ("HOUSE'S NETWORK", P.LTGREY, True)])


def draw_card(cv, x0, c, games, teams):
    if x0 >= PANEL_W or x0 + PANEL_W <= 0:
        return
    if c.type == HEADER:
        draw_header_card(cv, x0, c.sport)
    elif c.type == GAME:
        draw_game_card(cv, x0, c.ref)
    elif c.type == NODATA:
        draw_nodata_card(cv, x0, c.sport, teams[c.ref][1])
    elif c.type == MSG:
        draw_msg_card(cv, x0, c.ref)
    elif c.type == LIVE:
        draw_live_card(cv, x0, c.ref, games)
    elif c.type == FAVS:
        draw_favs_card(cv, x0, c.ref)
    elif c.type == NEXTUP:
        draw_nextup_card(cv, x0, c.ref)
    elif c.type == RANKS:
        draw_ranks_card(cv, x0, c.ref)
    elif c.type == STANDINGS:
        draw_standings_card(cv, x0, c.sport, c.ref)
    elif c.type == NEWS:
        draw_news_card(cv, x0, c.sport, c.ref)
    elif c.type == REMOTE:
        draw_remote_card(cv, x0)
    elif c.type == SETUP:
        draw_setup_card(cv, x0)


def wrap_message(msg):
    """Word-wraps msg into up to 3 lines of 17 chars (a MSG card's ref)."""
    lines, cur = [], ""
    for w in msg.upper().split():
        w = w[:17]
        if cur and len(cur) + 1 + len(w) > 17:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    lines = (lines + ["", "", ""])[:3]
    return tuple(lines)
