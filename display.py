"""Puts the LED framebuffer on the screen as round dots.

The dot size comes from the screen mode: the biggest whole number of pixels
per LED that fits the 160x96 panel, centered (a 1360x768 TV gets 8px dots,
1080p gets 11px). Pi 3 HDMI tops out at 1080p, so QHD/4K sets get 1080p.

Each frame: unlit LEDs become faint grey, the panel is scaled up with
nearest-neighbour, then multiplied by a precomputed mask of round dots.

Burn-in: the whole picture drifts a few pixels (under half an LED) to a new
spot every SHIFT_EVERY seconds, so the parts that never change -- the
clock box, the strip's rule, card frames -- don't sit on the same pixels.
"""

import time

import os

import numpy as np

SHIFT_EVERY = 120
# Offsets in eighths of an LED, walked in a loop.
SHIFT_PATH = [(0, 0), (2, 1), (3, 3), (1, 3), (-1, 3), (-3, 2), (-3, 0), (-2, -2), (0, -3), (2, -2)]

from panel import LED_OFF, PANEL_W, TOTAL_H


def dot_mask(scale):
    """One cell's brightness (0-255): a soft-edged round dot with a dark gap."""
    c = (scale - 1) / 2
    yy, xx = np.mgrid[0:scale, 0:scale]
    d = np.hypot(xx - c, yy - c)
    r = scale * 0.44
    return (np.clip(r + 0.5 - d, 0, 1) * 255).astype(np.uint8)


def prepare(fb, level=255):
    """Unlit LEDs to grey, then night dimming. Returns a new array."""
    out = fb.copy()
    unlit = ~out.any(axis=2)
    if level < 255:
        out = (out.astype(np.uint16) * level >> 8).astype(np.uint8)
        # Unlit LEDs dim less, so the grid stays faintly visible at night.
        off = tuple(v * min(255, level + 64) >> 8 for v in LED_OFF)
    else:
        off = LED_OFF
    out[unlit] = off
    return out


def expand(fb, scale, level=255, overlays=()):
    """Full-size RGB image of the panel (numpy, for PNG previews)."""
    from cards import qr_image
    big = prepare(fb, level).repeat(scale, 0).repeat(scale, 1).astype(np.uint16)
    m = np.tile(dot_mask(scale), (fb.shape[0], fb.shape[1]))
    out = (big * m[:, :, None] // 255).astype(np.uint8)
    for x, y, data, qs, border in overlays:
        img = (qr_image(data, qs, border).repeat(scale, 0).repeat(scale, 1).astype(np.uint16) * level // 255).astype(np.uint8)
        x0, y0 = x * scale, y * scale
        sx = max(0, -x0)
        w = min(img.shape[1], out.shape[1] - x0) - sx
        if w > 0:
            out[y0:y0 + img.shape[0], x0 + sx:x0 + sx + w] = img[:, sx:sx + w]
    return out


class Screen:
    """GPU path (SDL renderer): each frame only the 160x96 LED image is
    uploaded; the GPU scales it up (nearest) and multiplies in the dot mask."""

    def __init__(self):
        os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
        os.environ.setdefault("SDL_RENDER_SCALE_QUALITY", "0")   # nearest: crisp LEDs
        # SDL would turn SIGTERM into a quit event; let it stop the process.
        os.environ.setdefault("SDL_NO_SIGNAL_HANDLERS", "1")
        import pygame
        from pygame._sdl2 import video
        self.pg = pygame
        pygame.display.init()
        mode = pygame.display.get_desktop_sizes()[0]
        self.win = video.Window("sportsball", size=mode, fullscreen=True)
        self.ren = video.Renderer(self.win, accelerated=1, vsync=True)
        pygame.mouse.set_visible(False)
        self.W, self.H = mode
        self.scale = max(1, min(self.W // PANEL_W, self.H // TOTAL_H))
        self.pw, self.ph = PANEL_W * self.scale, TOTAL_H * self.scale
        self.dst = pygame.Rect((self.W - self.pw) // 2, (self.H - self.ph) // 2, self.pw, self.ph)
        m = np.tile(dot_mask(self.scale), (TOTAL_H, PANEL_W))
        msurf = pygame.surfarray.make_surface(np.repeat(m[:, :, None], 3, axis=2).swapaxes(0, 1))
        self.mask = video.Texture.from_surface(self.ren, msurf)
        self.mask.blend_mode = 4   # SDL_BLENDMODE_MOD: dst = dst * mask
        self.small = pygame.Surface((PANEL_W, TOTAL_H), depth=32)
        self.tex = video.Texture(self.ren, (PANEL_W, TOTAL_H), streaming=True)
        self.qr_tex = {}
        print(f"[Screen] {self.W}x{self.H}, {self.scale}px per LED", flush=True)

    def show(self, fb, level=255, overlays=()):
        pg = self.pg
        pg.surfarray.blit_array(self.small, prepare(fb, level).swapaxes(0, 1))
        self.tex.update(self.small)
        self.ren.draw_color = (0, 0, 0, 255)
        self.ren.clear()
        sx, sy = SHIFT_PATH[int(time.time() // SHIFT_EVERY) % len(SHIFT_PATH)]
        dst = self.dst.move(sx * self.scale // 8, sy * self.scale // 8)
        self.tex.draw(dstrect=dst)
        self.mask.draw(dstrect=dst)
        for x, y, data, qs, border in overlays:     # QR codes: solid squares, no dots
            tex = self._qr(data, qs, border)
            tex.color = (level, level, level)
            w, h = tex.width * self.scale, tex.height * self.scale
            tex.draw(dstrect=(dst.x + x * self.scale, dst.y + y * self.scale, w, h))
        self.ren.present()
        pg.event.pump()


    def _qr(self, data, qs, border):
        key = (data, qs, border)
        if key not in self.qr_tex:
            from pygame._sdl2 import video
            from cards import qr_image
            surf = self.pg.surfarray.make_surface(qr_image(data, qs, border).swapaxes(0, 1))
            self.qr_tex[key] = video.Texture.from_surface(self.ren, surf)
        return self.qr_tex[key]


def save_png(fb, path, scale=5, level=255, overlays=()):
    from PIL import Image
    Image.fromarray(expand(fb, scale, level, overlays)).save(path)
