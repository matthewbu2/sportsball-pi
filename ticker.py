"""The strip under the cards (port of the ESP32 Ticker.h).

Left: a clock/weather box. Right: a crawl at TICK_SPEED LEDs per second.
While games are live it is play-by-play -- each live game's latest play
after its matchup/score/clock, in LIVE order; a newer play waits until that
item has crawled past. With nothing live it is a score crawl: per sport, a
label then today's games (live, finals, upcoming), or the whole window if
nothing is on today.

While a live game's card is up, focus() makes the strip crawl only that
game's play from the right, repeating until the card moves on; other cards
resume the general crawl where it left off.
"""

import time

import panel as P
import weather
from cards import STALE_FINAL_HOURS, is_stale_final, iso_now, live_sort_key
from espn import IN, POST, PRE, SPORT_LABELS
from panel import PANEL_W, TICKER_H, text_w

TICK_BOX_W = 40
TICK_SPEED = 30
TICK_GAP = 11
TICK_PLAY_GAP = 24
TICK_RULE = (0, 70, 110)
TICK_CRAWL_W = PANEL_W - TICK_BOX_W - 2
PLAY_COLOR = (150, 200, 255)
DIVIDER = (60, 60, 70)
DAY_SHIFT_S = 9 * 3600   # day boundary: 4am Eastern (EST)


class Item:
    __slots__ = ("key", "w", "label", "play", "state", "fav", "win", "away", "home", "as_", "hs",
                 "st1", "st2", "away_color", "home_color", "status", "text")

    def __init__(self, **kw):
        self.label = self.play = False
        self.state = PRE
        self.fav = self.win = 0
        self.away = self.home = self.as_ = self.hs = self.st1 = self.st2 = self.status = self.text = ""
        self.away_color = self.home_color = P.WHITE
        for k, v in kw.items():
            setattr(self, k, v)

    def ident(self):
        return (self.key, self.label, self.play)


def _status_lines(g, today):
    s = g.status_text
    if g.state == PRE:
        if " " not in s:
            return s, ""
        head, tail = s.rsplit(" ", 1)
        return (tail, g.network) if today else (head, tail)
    cut = "/" if g.state == POST else " "
    if cut not in s:
        return ("HALF", "TIME") if s == "HALFTIME" else (s, "")
    a, b = s.split(cut, 1)
    return a, b


def _game_item(g, today):
    it = Item(key=g.key, state=g.state, fav=g.fav_mask, away=g.away_abbr, home=g.home_abbr,
              as_=g.away_score, hs=g.home_score, away_color=g.away_color, home_color=g.home_color)
    if g.state == POST:
        a, h = _int(g.away_score), _int(g.home_score)
        it.win = 1 if a > h else 2 if h > a else 0
    it.st1, it.st2 = _status_lines(g, today)
    w = (3 if it.fav else 0) + max(text_w(it.away), text_w(it.home)) + 4
    if it.state != PRE:
        w += max(text_w(it.as_), text_w(it.hs)) + 5
    it.w = w + max(text_w(it.st1), text_w(it.st2)) + TICK_GAP
    return it


def _play_item(g):
    it = Item(key=g.key, play=True, state=g.state, fav=g.fav_mask, away=g.away_abbr, home=g.home_abbr,
              as_=g.away_score, hs=g.home_score, away_color=g.away_color, home_color=g.home_color,
              status=g.status_text, text=g.play_text)
    it.w = _play_w(it)
    return it


def _play_w(it):
    return (text_w(it.away) + 4 + text_w(it.as_) + 8 + text_w(it.home) + 4 + text_w(it.hs) + 8 +
            text_w(it.status) + 10 + text_w(it.text) + TICK_PLAY_GAP)


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def pass_seconds(text):
    """How long the focused play takes to cross the strip once."""
    return (TICK_CRAWL_W + text_w(text)) / TICK_SPEED


class Ticker:
    def __init__(self, fb):
        self.cv = P.Canvas(fb[P.PANEL_H:P.PANEL_H + TICKER_H])
        self.fav_live = []          # live favorite games (their score shows in the box)
        self.items = []
        self.head = 0          # item at the crawl's left edge...
        self.off = 0           # ...and how many LEDs of it have gone by
        self.focus_key = None
        self.focus_text = ""
        self.focus_next = ""
        self.focus_off = 0
        self.acc = 0.0         # fractional LEDs not yet stepped
        self.last_t = time.monotonic()

    # -- building --------------------------------------------------------
    def rebuild(self, games, teams):
        self.fav_live = [g for g in games if g.state == IN and g.fav_mask]
        out = [_play_item(g) for g in sorted((g for g in games if g.state == IN and g.play_text),
                                              key=live_sort_key)]
        stale_before = iso_now(-STALE_FINAL_HOURS * 3600)
        s = time.time() - DAY_SHIFT_S
        day_end = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(s - s % 86400 + 86400 + DAY_SHIFT_S))
        for pass_ in (0, 1):
            if out:
                break
            done = set()
            for sport, _q in teams:
                if sport <= 0 or sport in done:
                    continue
                done.add(sport)
                sel = []
                for g in games:
                    if g.sport_idx != sport or is_stale_final(g, stale_before):
                        continue
                    today = g.state == IN or g.date < day_end
                    if pass_ == 0 and not today:
                        continue
                    sel.append(g)
                if not sel:
                    continue
                # live, then finals latest first, then upcoming by start time
                by_date = sorted(sel, key=lambda g: g.date)
                sel = [g for g in by_date if g.state == IN] + \
                    [g for g in reversed(by_date) if g.state == POST] + [g for g in by_date if g.state == PRE]
                lab = SPORT_LABELS[sport]
                out.append(Item(key=sport, label=True, away=lab, w=text_w(lab) + 12))
                out += [_game_item(g, pass_ == 0 or g.date < day_end) for g in sel]

        if self.focus_key:   # the focused game's newest play, for its next pass
            for g in games:
                if g.key == self.focus_key and g.play_text:
                    self.focus_next = g.play_text
                    break
        cur = self.items[self.head] if self.items else None
        head = None
        if cur:
            for i, it in enumerate(out):
                if it.ident() == cur.ident():
                    head = i
                    break
        if head is not None and cur.play and out[head].text != cur.text:
            out[head] = cur    # a newer play waits until this item has gone by
        if head is None:
            self.head = min(self.head, len(out) - 1) if out else 0
            self.off = 0
        else:
            self.head = head
            self.off = min(self.off, out[head].w - 1)
        self.items = out

    def focus(self, g):
        key = g.key if g is not None and g.state == IN and g.play_text else None
        if key != self.focus_key:
            self.focus_key = key
            self.focus_off = 0
            if key:
                self.focus_text = self.focus_next = g.play_text

    # -- drawing ---------------------------------------------------------
    def _weather_icon(self, x, y, code, day):
        cv = self.cv
        SUN, MOON, CLOUD, DARK, RAIN, SNOW = P.YELLOW, (220, 220, 170), P.LTGREY, P.GREY, (80, 150, 255), P.WHITE

        def cloud(cx, cy, c):
            cv.fill(cx + 1, cy, 3, 1, c)
            cv.fill(cx, cy + 1, 6, 3, c)

        def sun(cx, cy):
            if day:
                cv.fill(cx + 1, cy, 3, 5, SUN)
                cv.fill(cx, cy + 1, 5, 3, SUN)
            else:
                cv.fill(cx + 1, cy, 2, 1, MOON)
                cv.fill(cx, cy + 1, 2, 3, MOON)
                cv.fill(cx + 1, cy + 4, 2, 1, MOON)

        if code == 0:
            sun(x + 1, y + 1)
            if day:
                for px, py in ((x + 3, y - 1), (x + 3, y + 7), (x - 1, y + 3), (x + 7, y + 3)):
                    cv.pixel(px, py, SUN)
        elif code <= 2:
            sun(x + 2, y)
            cloud(x, y + 3, CLOUD)
        elif code == 3:
            cloud(x, y + 1, CLOUD)
            cloud(x + 1, y + 3, DARK)
        elif code in (45, 48):
            for r in (1, 3, 5):
                cv.fill(x + (0 if r % 4 == 1 else 1), y + r, 6, 1, CLOUD)
        elif 71 <= code <= 77 or code in (85, 86):
            cloud(x, y, CLOUD)
            for k in range(3):
                cv.pixel(x + 1 + k * 2, y + 6 - k % 2, SNOW)
        elif code >= 95:
            cloud(x, y, DARK)
            for px, py in ((3, 4), (2, 5), (3, 5), (2, 6)):
                cv.pixel(x + px, y + py, P.YELLOW)
        elif code >= 51:
            cloud(x, y, CLOUD)
            for k in range(3):
                cv.fill(x + 1 + k * 2, y + 5 - k % 2, 1, 2, RAIN)

    def _draw_fav_score(self, g):
        """A live favorite's score in the box: away row, home row."""
        cv = self.cv
        for y, abbr, score, color in ((1, g.away_abbr, g.away_score, g.away_color),
                                      (9, g.home_abbr, g.home_score, g.home_color)):
            cv.text(1, y, abbr[:4], color)
            cv.text_right(TICK_BOX_W - 2, y, score, P.YELLOW)
        cv.fill(TICK_BOX_W, 2, 1, TICKER_H - 3, TICK_RULE)

    def _draw_box(self):
        cv = self.cv
        # While favorites play, every other 5 s shows their score (cycling).
        if self.fav_live and int(time.time() / 5) % 2:
            self._draw_fav_score(self.fav_live[int(time.time() / 10) % len(self.fav_live)])
            return
        t = time.localtime()
        h = t.tm_hour % 12 or 12
        line = f"{h}:{t.tm_min:02d}{'A' if t.tm_hour < 12 else 'P'}"
        cv.text(TICK_BOX_W // 2 - text_w(line) // 2, 1, line, P.WHITE)
        if weather.temp_f is not None and int(time.time() / 6) % 2 == 0:
            tmp = str(weather.temp_f)
            w = 7 + 3 + text_w(tmp) + 4 + 5
            x = TICK_BOX_W // 2 - w // 2
            self._weather_icon(x, 9, weather.code, weather.is_day)
            x += 10
            cv.text(x, 9, tmp, P.LTGREY)
            x += text_w(tmp) + 1
            for px, py in ((1, 9), (0, 10), (2, 10), (1, 11)):   # degree ring
                cv.pixel(x + px, py, P.LTGREY)
            cv.text(x + 4, 9, "F", P.LTGREY)
        else:
            line = f"{time.strftime('%b').upper()} {t.tm_mday}"
            cv.text(TICK_BOX_W // 2 - text_w(line) // 2, 9, line, P.ORANGE)
        cv.fill(TICK_BOX_W, 2, 1, TICKER_H - 3, TICK_RULE)

    def _draw_item(self, x, it):
        cv = self.cv
        if it.play:
            y = 5
            for s, c, gap in ((it.away, it.away_color, 4), (it.as_, P.YELLOW, 8), (it.home, it.home_color, 4),
                              (it.hs, P.YELLOW, 8), (it.status, P.GREEN, 10)):
                cv.text(x, y, s, c)
                x += text_w(s) + gap
            cv.text(x, y, it.text, PLAY_COLOR)
            x += text_w(it.text)
            cv.fill(x + TICK_PLAY_GAP // 2, 5, 1, 7, DIVIDER)
            return
        if it.label:
            w = text_w(it.away) + 6
            cv.fill(x, 2, w, TICKER_H - 3, P.ORANGE)
            cv.text(x + 3, 5, it.away, P.BLACKISH)
            return
        if it.fav:
            cv.fill(x, 2, 1, TICKER_H - 3, P.YELLOW)
            x += 3
        cv.text(x, 1, it.away, it.away_color)
        cv.text(x, 9, it.home, it.home_color)
        x += max(text_w(it.away), text_w(it.home)) + 4
        if it.state != PRE:
            sw = max(text_w(it.as_), text_w(it.hs))
            ac = P.YELLOW if it.state == IN else P.GREY if it.win == 2 else P.WHITE
            hc = P.YELLOW if it.state == IN else P.GREY if it.win == 1 else P.WHITE
            cv.text(x + sw - text_w(it.as_), 1, it.as_, ac)
            cv.text(x + sw - text_w(it.hs), 9, it.hs, hc)
            x += sw + 5
        c1 = P.GREEN if it.state == IN else P.ORANGE if it.state == POST else P.WHITE
        c2 = P.CYAN if it.state == PRE else c1
        cv.text(x, 1, it.st1, c1)
        cv.text(x, 9, it.st2, c2)
        x += max(text_w(it.st1), text_w(it.st2))
        cv.fill(x + TICK_GAP // 2, 5, 1, 7, DIVIDER)

    def frame_saver(self):
        """Screensaver strip: just the time, drifting slowly back and forth."""
        cv = self.cv
        cv.clear()
        t = time.localtime()
        line = f"{t.tm_hour % 12 or 12}:{t.tm_min:02d}{'A' if t.tm_hour < 12 else 'P'}"
        if weather.temp_f is not None:
            line += f"  {weather.temp_f}F"
        span = PANEL_W - text_w(line) - 2
        pos = int(time.time() / 3) % (2 * span)
        cv.text(1 + (pos if pos < span else 2 * span - pos), 5, line, P.GREY)

    def frame(self):
        """Draws the strip and advances the crawl by the time since last frame."""
        now = time.monotonic()
        self.acc += (now - self.last_t) * TICK_SPEED
        self.last_t = now
        steps = int(self.acc)
        self.acc -= steps

        cv = self.cv
        cv.clear()
        cv.fill(0, 0, PANEL_W, 1, TICK_RULE)
        self._draw_box()
        if self.focus_key:
            cv.clip(TICK_BOX_W + 1, PANEL_W)
            cv.text(PANEL_W - self.focus_off, 5, self.focus_text, PLAY_COLOR)
            cv.no_clip()
            self.focus_off += steps
            if self.focus_off >= TICK_CRAWL_W + text_w(self.focus_text) + 20:
                self.focus_off = 0
                self.focus_text = self.focus_next
        elif self.items:
            cv.clip(TICK_BOX_W + 1, PANEL_W)
            x, i, n = TICK_BOX_W + 2 - self.off, self.head, len(self.items)
            while x < PANEL_W:
                self._draw_item(x, self.items[i])
                x += self.items[i].w
                i = (i + 1) % n
            cv.no_clip()
            self.off += steps
            while self.off >= self.items[self.head].w:
                self.off -= self.items[self.head].w
                self.head = (self.head + 1) % n
