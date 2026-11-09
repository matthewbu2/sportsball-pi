"""Virtual LED-matrix panel, Glance-ticker style (port of the ESP32 Panel.h).

Everything is drawn into a small low-res framebuffer of "LEDs" (a numpy
array, rows x cols x RGB). display.py expands it to the TV with each LED as
a round dot; an all-zero pixel is an unlit LED (faint grey there).

A Canvas is a view onto part of the framebuffer with a horizontal clip
window, so a card can clip a scrolling line to its own width. All drawing
clips fully on every edge, since cards crawl partly off-screen.
"""

import os

import numpy as np

from font5x7 import FONT

PANEL_H = 80
TICKER_H = 16      # score crawl strip under the cards (ticker.py)
TOTAL_H = PANEL_H + TICKER_H
MIN_W = 160        # the layouts' design width


def _screen_size():
    try:
        with open("/sys/class/graphics/fb0/virtual_size") as f:
            w, h = (int(v) for v in f.read().split(","))
        return w, h
    except (OSError, ValueError):
        return None


def _panel_width():
    """LEDs across: as many as fit the screen at the dot size the height
    allows, so a wide TV is filled edge to edge (1360x768 -> 170, 1080p ->
    174). Cards and scenes lay out relative to the edges and center, so any
    width >= 160 works. SPORTSBALL_PANEL_W overrides (previews)."""
    if os.environ.get("SPORTSBALL_PANEL_W"):
        return max(MIN_W, int(os.environ["SPORTSBALL_PANEL_W"]))
    size = _screen_size()
    if not size:
        return MIN_W
    w, h = size
    scale = max(1, h // TOTAL_H)
    return max(MIN_W, w // scale)


PANEL_W = _panel_width()   # card area, LEDs


def c565(v):
    """An RGB565 constant from the ESP32 code, as RGB888."""
    r, g, b = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


LED_OFF = c565(0x10A2)   # faint grey "unlit LED"

WHITE = c565(0xFFFF)
GREY = c565(0x8410)
LTGREY = c565(0xC618)
YELLOW = c565(0xFFE0)
ORANGE = c565(0xFD20)
CYAN = c565(0x07FF)
RED = c565(0xF800)
DKRED = c565(0x6000)
TEAL = c565(0x0451)
GREEN = c565(0x07E0)
DIMDOT = c565(0x2104)    # unlit count dot / timeout bar
BLACKISH = c565(0x0841)


class Canvas:
    def __init__(self, fb):
        self.fb = fb
        self.h, self.w = fb.shape[:2]
        self.clip_l, self.clip_r = 0, self.w

    def clip(self, l, r):
        self.clip_l, self.clip_r = max(l, 0), min(r, self.w)

    def no_clip(self):
        self.clip_l, self.clip_r = 0, self.w

    def clear(self):
        self.fb[:] = 0

    def pixel(self, x, y, c):
        if self.clip_l <= x < self.clip_r and 0 <= y < self.h:
            self.fb[y, x] = c

    def fill(self, x, y, w, h, c):
        x1, y1 = max(x, self.clip_l), max(y, 0)
        x2, y2 = min(x + w, self.clip_r), min(y + h, self.h)
        if x1 < x2 and y1 < y2:
            self.fb[y1:y2, x1:x2] = c

    def mask(self, x, y, m, c):
        """Lights the True cells of boolean array m with its top-left at (x, y)."""
        mh, mw = m.shape
        x1, y1 = max(x, self.clip_l), max(y, 0)
        x2, y2 = min(x + mw, self.clip_r), min(y + mh, self.h)
        if x1 >= x2 or y1 >= y2:
            return
        sub = m[y1 - y:y2 - y, x1 - x:x2 - x]
        self.fb[y1:y2, x1:x2][sub] = c

    def bitmap(self, x, y, img, scale=1):
        """Draws an RGB array (h x w x 3); black pixels stay unlit."""
        if scale > 1:
            img = img.repeat(scale, 0).repeat(scale, 1)
        h, w = img.shape[:2]
        x1, y1 = max(x, self.clip_l), max(y, 0)
        x2, y2 = min(x + w, self.clip_r), min(y + h, self.h)
        if x1 < x2 and y1 < y2:
            self.fb[y1:y2, x1:x2] = img[y1 - y:y2 - y, x1 - x:x2 - x]

    # 5x7 text (classic font), 6px advance per char at size 1.
    def text(self, x, y, s, c, size=1):
        adv = 6 * size
        for ch in s:
            if x >= self.clip_r:
                break
            if x + 5 * size > self.clip_l:
                self.mask(x, y, _glyph(ch, size), c)
            x += adv

    def text_right(self, x_right, y, s, c, size=1):
        self.text(x_right - text_w(s, size) + 1, y, s, c, size)

    def text_center(self, x_center, y, s, c, size=1):
        self.text(x_center - text_w(s, size) // 2, y, s, c, size)

    # Tiny 3x5 font, 4px advance.
    def tiny(self, x, y, s, c):
        for ch in s:
            g = _tiny(ch)
            if g is not None:
                self.mask(x, y, g, c)
            x += 4


def text_w(s, size=1):
    return (len(s) * 6 - 1) * size if s else 0


def tiny_w(s):
    return len(s) * 4 - 1 if s else 0


_glyph_cache = {}


def _glyph(ch, size):
    key = (ch, size)
    g = _glyph_cache.get(key)
    if g is None:
        code = ord(ch)
        if code > 255:
            code = ord("?")
        cols = FONT[code * 5:code * 5 + 5]
        g = np.array([[(cols[col] >> row) & 1 for col in range(5)] for row in range(8)], dtype=bool)
        if size > 1:
            g = g.repeat(size, 0).repeat(size, 1)
        _glyph_cache[key] = g
    return g


# Each glyph: 5 rows of 3 bits, top row in the high bits.
_TINY = {
    "0": 0b111101101101111, "1": 0b010110010010111, "2": 0b111001111100111,
    "3": 0b111001111001111, "4": 0b101101111001001, "5": 0b111100111001111,
    "6": 0b111100111101111, "7": 0b111001001010010, "8": 0b111101111101111,
    "9": 0b111101111001111, "-": 0b000000111000000, "#": 0b101111101111101,
    ".": 0b000000000000010, "/": 0b001001010100100, "A": 0b010101111101101,
    "B": 0b110101110101110, "C": 0b011100100100011, "D": 0b110101101101110,
    "E": 0b111100110100111, "F": 0b111100110100100, "G": 0b011100101101011,
    "H": 0b101101111101101, "I": 0b111010010010111, "J": 0b001001001101010,
    "K": 0b101101110101101, "L": 0b100100100100111, "M": 0b101111111101101,
    "N": 0b110101101101101, "O": 0b010101101101010, "P": 0b110101110100100,
    "Q": 0b010101101110011, "R": 0b110101110101101, "S": 0b011100010001110,
    "T": 0b111010010010010, "U": 0b101101101101111, "V": 0b101101101101010,
    "W": 0b101101111111101, "X": 0b101101010101101, "Y": 0b101101010010010,
    "Z": 0b111001010100111, ":": 0b000010000010000, "+": 0b000010111010000,
    "%": 0b101001010100101, "'": 0b010010000000000, ",": 0b000000000010100,
    "!": 0b010010010000010,
}
_tiny_cache = {}


def _tiny(ch):
    ch = ch.upper()
    if ch not in _tiny_cache:
        bits = _TINY.get(ch)
        _tiny_cache[ch] = None if bits is None else np.array(
            [[bool(bits & (1 << (14 - (r * 3 + c)))) for c in range(3)] for r in range(5)])
    return _tiny_cache[ch]


def new_framebuffer():
    return np.zeros((TOTAL_H, PANEL_W, 3), dtype=np.uint8)
