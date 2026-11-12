"""Sportsball: Glance-style LED sports ticker for a Raspberry Pi on HDMI.

Python port of the ESP32 ticker (Waveshare 5" layout). The screen is a
virtual LED panel (PANEL_W x 96, as wide as the TV allows): 80 rows of cards crawling right-to-left with a
hold on each, over a 16-row strip (clock/weather + play-by-play or scores).

    python3 main.py              run on the screen (KMSDRM, fullscreen)
    python3 main.py --png DIR    fetch once, write each card as a PNG (no screen)
    python3 main.py --scenes DIR fetch once, write a contact sheet of every demo scene

Web remote on port 8080: demo buttons for every scene (remote.py).

Settings live in config.json next to this file (see load_config()).
"""

import json
import os
import socket
import sys
import threading
import time

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import anim as A
import espn
import espn_worker
import cards as C
import extras
import logos
import panel as P
import settings
import style
import weather
from espn import IN, NUM_SPORTS, PRE, SPORT_LABELS, FetchError, parse_favs
from ticker import Ticker, pass_seconds

HERE = os.path.dirname(os.path.abspath(__file__))

# Refresh timing
UPDATE_INTERVAL = 5 * 60       # full refresh, all sports
LIVE_POLL_BASKETBALL = 10      # per-sport live refresh, today's games only
LIVE_POLL_OTHER = 15
WINDOW_DAYS = 3                # today + next 2 days
DAY_SHIFT_S = 9 * 3600         # day boundary: 4am Eastern (EST)

# Crawl
CARD_GAP = 10                  # blank LEDs between cards
CARD_STRIDE = P.PANEL_W + CARD_GAP
SCROLL_PX_PER_SEC = 60
HOLD_GAME = 5.0
HOLD_CRUNCH = 8.0
HOLD_HEADER = 2.0
HOLD_OTHER = 3.0
HOLD_PLAY_MAX = 20.0           # a long play stretches a live card's hold up to this
CRUNCH_EVERY = 3               # revisit a crunch-time game after this many cards
HOLD_FILLER = {C.RANKS: 9.0, C.STANDINGS: 7.0, C.REMOTE: 7.0, C.SETUP: 15.0}
RED_ZONE_MAX = 300             # longest a red-zone game holds the screen (seconds)


class Delay:
    """Holds each published games list back `delay` seconds, so scores,
    plays and animations land when a streaming TV (30-90 s behind ESPN)
    shows them. With nothing old enough yet (just started), the oldest."""

    def __init__(self):
        self.buf = []              # (published at, version, games), oldest first
        self.shown = None

    def push(self, version, games):
        self.buf.append((time.monotonic(), version, games))

    def reset(self):
        self.buf.clear()
        self.shown = None

    def due(self, delay):
        """The games to show now, if they changed since last time, else None."""
        if not self.buf:
            return None
        cutoff = time.monotonic() - delay
        i = max([k for k, b in enumerate(self.buf) if b[0] <= cutoff], default=0)
        self.buf = self.buf[i:]          # older ones are never shown again
        pick = self.buf[0]
        if pick[1] == self.shown:
            return None
        self.shown = pick[1]
        return pick[2]


def load_config():
    """(teams, favorites) from settings.py (config.json, editable in the web remote)."""
    teams = [(int(s), str(q)) for s, q in settings.get("teams")]
    return teams, parse_favs(settings.get("favorites"))


def clock_ok():
    """The clock was set from the internet. A Pi has no clock battery: until then it
    shows the date the OS image was made, so "night" can be the middle of the day."""
    return os.path.exists("/run/systemd/timesync/synchronized")


def panel_level():
    """Overall brightness 0-255: the brightness setting, lowered at night."""
    level = settings.get("brightness") / 100
    if settings.get("night_dim") and clock_ok() and settings.in_hours(time.localtime().tm_hour, settings.get("night_start"),
                                                         settings.get("night_end")):
        level *= settings.get("night_level") / 100
    return max(8, min(255, int(level * 255)))


# ---------------------------------------------------------------------------
# Day/date helpers (ESPN dates are UTC ISO; the day turns over at 4am ET)
# ---------------------------------------------------------------------------
def day_start_utc(now, offset_days):
    s = now - DAY_SHIFT_S
    return s - s % 86400 + offset_days * 86400 + DAY_SHIFT_S


def day_ymd(now, offset_days):
    return time.strftime("%Y%m%d", time.gmtime(day_start_utc(now, offset_days) - DAY_SHIFT_S))


def iso_utc(t):
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(t))


# ---------------------------------------------------------------------------
# Fetching (background thread). Publishes a new games list + version.
# ---------------------------------------------------------------------------
class Fetcher:
    def __init__(self, teams, favs):
        self.teams, self.favs = teams, favs
        self.sports = []
        for s, _q in teams:
            if 0 < s < NUM_SPORTS and s not in self.sports:
                self.sports.append(s)
        self.by_sport = {s: [] for s in self.sports}
        self.games = []
        self.version = 0
        self.errors = 0
        self.last_full = 0.0
        self.last_ok = 0.0             # last refresh with no errors (health)
        self.last_error = ""
        self.last_live = {s: 0.0 for s in self.sports}
        self.stopped = False

    def queries(self, sport):
        return [(slot, q) for slot, (s, q) in enumerate(self.teams) if s == sport]

    def publish(self):
        games = [g for s in self.sports for g in self.by_sport[s]]
        if settings.get("cfb_top25"):
            # A college Saturday is 50+ games: keep favorites and ranked teams.
            games = [g for g in games if g.sport_idx != 5 or g.fav_mask or g.away_rank or g.home_rank]
        self.games = games
        self.version += 1

    def full(self):
        now = time.time()
        rng = f"{day_ymd(now, 0)}-{day_ymd(now, WINDOW_DAYS - 1)}"
        cutoff = iso_utc(day_start_utc(now, WINDOW_DAYS))
        errs = 0
        for s in self.sports:
            try:
                self.by_sport[s] = espn_worker.fetch(s, self.queries(s), self.favs, rng, cutoff)
            except (FetchError, OSError, ValueError) as e:
                errs += 1
                self.last_error = f"{time.strftime('%H:%M')} {str(e)[:100]}"
                print(f"[Fetch] {e}", flush=True)
            self.last_live[s] = time.time()
        self.errors = errs
        if not errs:
            self.last_ok = time.time()
        # A failed sport keeps its last good games; retry the refresh soon.
        self.last_full = time.time() - (UPDATE_INTERVAL - 30 if errs else 0)
        print(f"[Fetch] full: {sum(len(v) for v in self.by_sport.values())} games, {errs} errors", flush=True)
        self.publish()

    def live_sports(self):
        now_iso = iso_utc(time.time())
        return {g.sport_idx for g in self.games if g.state == IN or (g.state == PRE and g.date <= now_iso)}

    def live(self, sports):
        now = time.time()
        day, day_end = day_ymd(now, 0), iso_utc(day_start_utc(now, 1))
        for s in sports:
            self.last_live[s] = time.time()
            try:
                today = espn_worker.fetch(s, self.queries(s), self.favs, day, day_end)
            except (FetchError, OSError, ValueError) as e:
                self.last_error = f"{time.strftime('%H:%M')} live: {str(e)[:100]}"
                print(f"[Fetch] live {e}", flush=True)
                continue
            self.last_ok = time.time()
            self.by_sport[s] = today + [g for g in self.by_sport[s] if g.date >= day_end]
        self.publish()

    def run(self):
        while not self.stopped:
            if time.time() - self.last_full >= UPDATE_INTERVAL:
                self.full()
            else:
                due = set()
                for s in self.live_sports():
                    iv = LIVE_POLL_BASKETBALL if s in (2, 6) else LIVE_POLL_OTHER
                    if time.time() - self.last_live.get(s, 0) >= iv:
                        due.add(s)
                if due:
                    self.live(due)
            time.sleep(1)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------
class Crawl:
    def __init__(self, fb, teams):
        self.fb = fb
        self.cv = P.Canvas(fb[:P.PANEL_H])
        self.teams = teams
        self.ticker = Ticker(fb)
        self.games = []
        self.cards = [C.Card(C.MSG, 0, C.wrap_message("Fetching scores..."))]
        self.cur = self.nxt = 0
        self.holding = True
        self.hold_start = self.scroll_start = time.monotonic()
        self.return_to = -1
        self.since_crunch = 0
        self.crunch_turn = 0
        self.have_favs = False
        self.detector = A.Detector()
        self.player = A.Player()
        self.pinned = False
        self.override = None      # message lines shown instead of the cards (Wi-Fi setup)
        self.on_focus = None      # callable(game or None): the card on screen changed
        self.live_demo = None     # livedemo.LiveDemo while the play-by-play demo runs
        self.extra = None         # extras.snapshot(): favorites' other games + filler cards
        self.red_zone = True      # settings: jump to a game when it reaches the red zone
        self.rz_key = None        # the game holding the screen for its red-zone drive
        self.rz_since = 0.0
        self.rz_seen = set()      # games already shown for their current red-zone trip
        self.on_red_zone = None   # callable(game): the screen jumped to a red-zone game
        self.fetcher = None       # for the health panel
        self.saver = False        # overnight screensaver running (set_saver)
        self.saver_hooks = None   # the player's sound hooks, parked while the saver is silent
        self.saver_last = ""

    def card_id(self, c):
        key = c.ref.key if c.type in (C.GAME, C.NEXTUP) else None
        ref = c.ref if c.type in (C.NODATA, C.NEWS) else c.ref[0] if c.type == C.STANDINGS else None
        return (c.type, c.sport if c.type not in (C.LIVE, C.FAVS) else 0, key, ref)

    def set_games(self, games, detect=True):
        prev = self.card_id(self.cards[self.cur]) if self.cards else None
        prev_next = self.card_id(self.cards[self.nxt]) if not self.holding else None
        self.games = games
        self.player.games = games
        if detect:
            self.player.add(self.detector.detect(games))
        self.cards, self.game_day = C.build_cards(games, self.teams, self.extra)
        if not settings.get("setup_done") and not settings.get("favorites"):
            self.cards.insert(0, C.Card(C.SETUP, 0, None))     # new ticker: point at the phone setup page
        self.ticker.rebuild(games, self.teams)
        self.return_to = -1    # card indices just changed
        ids = [self.card_id(c) for c in self.cards]
        self.cur = ids.index(prev) if prev in ids else min(self.cur, len(self.cards) - 1)
        if prev_next is not None:
            self.nxt = ids.index(prev_next) if prev_next in ids else (self.cur + 1) % len(self.cards)
        for c in self.cards:
            for u in C.card_logos(c):
                logos.want(u)
        if self.holding:
            self.focus()
        self.check_red_zone(games)

    def check_red_zone(self, games):
        """A football game just reached the red zone: cut to it and stay
        there until the drive ends (scores, turns it over, backs out)."""
        rz = {g.key: g for g in games if g.state == IN and g.sport_idx in (1, 5) and g.is_red_zone}
        self.rz_seen &= set(rz)
        if self.rz_key and self.rz_key not in rz:
            self.rz_key = None
        if not self.red_zone or self.rz_key or self.pinned or self.player.active:
            return
        new = sorted((g for k, g in rz.items() if k not in self.rz_seen),
                     key=lambda g: (not g.fav_mask, not C.is_crunch(g), g.date))
        if not new:
            return
        g = new[0]
        self.rz_seen.add(g.key)
        self.rz_key, self.rz_since = g.key, time.monotonic()
        self.park_on(g)
        if self.on_red_zone:
            self.on_red_zone(g)

    def is_crunch_card(self, c):
        return c.type == C.GAME and C.is_crunch(c.ref)

    def hold_time(self, c):
        if (self.rz_key and c.type == C.GAME and c.ref.key == self.rz_key
                and time.monotonic() - self.rz_since < RED_ZONE_MAX):
            return float("inf")
        if c.type in HOLD_FILLER:
            return HOLD_FILLER[c.type]
        if c.type == C.NEWS:
            return min(12.0, 4.0 + len(c.ref[1]) / 15)
        if c.type == C.NEXTUP:
            return HOLD_GAME
        if c.type == C.GAME:
            g = c.ref
            base = HOLD_CRUNCH if C.is_crunch(g) else HOLD_GAME
            if g.state != IN or not g.play_text:
                return base
            return min(HOLD_PLAY_MAX, max(base, pass_seconds(g.play_text) + 0.5))
        return HOLD_HEADER if c.type in (C.HEADER, C.LIVE, C.FAVS) else HOLD_OTHER

    def pick_next(self):
        n = len(self.cards)
        if n <= 1:
            return self.cur
        if 0 <= self.return_to < n:
            r, self.return_to = self.return_to, -1
            return r
        self.return_to = -1
        natural = (self.cur + 1) % n
        if self.since_crunch < CRUNCH_EVERY:
            return natural
        crunch = [i for i, c in enumerate(self.cards) if self.is_crunch_card(c)]
        if not crunch:
            return natural
        i = crunch[self.crunch_turn % len(crunch)]
        self.crunch_turn += 1
        if i in (self.cur, natural):
            return natural
        self.return_to = natural
        return i

    def focus(self):
        c = self.cards[self.cur]
        g = c.ref if c.type == C.GAME else None
        self.ticker.focus(g)
        if self.on_focus:
            self.on_focus(g)

    def skip(self):
        if self.holding:
            self.pinned = False
            self.hold_start = -1e9

    def start_hold(self, i):
        self.cur = i
        self.holding = True
        self.hold_start = time.monotonic()
        if self.is_crunch_card(self.cards[i]):
            self.since_crunch = 0
        else:
            self.since_crunch += 1
        self.focus()

    def start_scroll(self):
        if len(self.cards) <= 1:
            self.hold_start = time.monotonic()
            return
        self.nxt = self.pick_next()
        self.holding = False
        self.scroll_start = time.monotonic()

    # ---- for the web remote's Now tab
    @staticmethod
    def _team(g, side):
        fill = g.away_fill if side == 1 else g.home_fill
        return {"abbr": g.away_abbr if side == 1 else g.home_abbr,
                "name": g.away_name if side == 1 else g.home_name,
                "score": (g.away_score if side == 1 else g.home_score) or "",
                "rank": (g.away_rank if side == 1 else g.home_rank) or 0,
                "record": (g.away_record if side == 1 else g.home_record) or "",
                "color": "#%02x%02x%02x" % tuple(fill[:3]),
                "logo": espn.team_logo_url(g, side == 2)}

    def game_info(self, g):
        state = {IN: "live", PRE: "pre"}.get(g.state, "final")
        return {"key": g.key, "sport": SPORT_LABELS[g.sport_idx], "state": state, "status": g.status_text,
                "away": self._team(g, 1), "home": self._team(g, 2), "fav": bool(g.fav_mask),
                "down": getattr(g, "down_dist", "") or "", "network": g.network or ""}

    CARD_TITLES = {C.HEADER: "SCOREBOARD", C.LIVE: "LIVE GAMES", C.FAVS: "MY TEAMS", C.NEXTUP: "NEXT UP",
                   C.RANKS: "TOP 25", C.STANDINGS: "STANDINGS", C.NEWS: "HEADLINES", C.REMOTE: "REMOTE",
                   C.SETUP: "SETUP"}

    def now_info(self):
        if not self.cards:
            return {"type": "none"}
        c = self.cards[self.cur]
        if c.type == C.GAME:
            out = self.game_info(c.ref)
            out["type"] = "game"
            if c.ref.state == IN and getattr(c.ref, "play_text", ""):
                out["play"] = c.ref.play_text[:160]
            return out
        if c.type == C.NEXTUP and hasattr(c.ref, "key"):
            out = self.game_info(c.ref)
            out["type"] = "game"
            return out
        return {"type": "card", "kind": self.CARD_TITLES.get(c.type, "CARD").lower(), "title": self.CARD_TITLES.get(c.type, ""),
                "sport": SPORT_LABELS[c.sport] if isinstance(c.sport, int) and 0 < c.sport < len(SPORT_LABELS) else "",
                "image": ""}

    def board(self):
        order = {IN: 0, PRE: 1}
        games = sorted(self.games, key=lambda g: (order.get(g.state, 2), not g.fav_mask, g.date))
        return [self.game_info(g) for g in games[:40]]

    def show(self, key):
        """Put this game's card on the TV now (from the remote)."""
        g = next((x for x in self.games if x.key == key), None)
        if g is not None:
            self.pinned = False
            self.park_on(g)

    def park_on(self, game):
        """After a moment's scene: hold on that game's card (its newest copy)."""
        for i, c in enumerate(self.cards):
            if c.type == C.GAME and c.ref.key == game.key:
                self.start_hold(i)
                return
        self.start_hold(self.cur)

    def set_saver(self, on):
        """Overnight screensaver on/off: the animations back to back, silent."""
        if on == self.saver:
            return
        self.saver = on
        if on:
            self.saver_hooks, self.player.hooks = self.player.hooks, []
            self.player.queue.clear()
        else:
            self.player.hooks = self.saver_hooks or self.player.hooks
            self.hold_start = time.monotonic()
        print(f"[Saver] {'on' if on else 'off'}", flush=True)

    def saver_frame(self):
        C.OVERLAYS.clear()
        if not self.player.active:
            sports = {s for s, _q in self.teams}
            names = [n for n in A.DEMOS if n != self.saver_last and sports & set(A.SPORT_FAMILY[A.DEMOS[n][0]])]
            if names:
                import random
                self.saver_last = random.choice(names)
                ev = A.demo_event(self.saver_last, self.games)
                if ev is not None:
                    self.player.add([ev])
        if self.player.active:
            self.player.frame(self.cv, self.have_favs)
        else:
            self.cv.clear()
        self.ticker.frame_saver()

    def frame(self):
        if self.saver and not self.override:
            self.saver_frame()
            return
        if self.override:
            self.cv.clear()
            C.OVERLAYS.clear()
            if self.override.get("mode") == "setup":
                C.draw_wifi_card(self.cv, 0, self.override)
            else:
                C.draw_msg_card(self.cv, 0, ("CONNECTING TO", self.override.get("network", "")[:20].upper(), ""))
            self.ticker.frame()
            return
        # Moments play while the crawl is holding; the strip keeps running.
        if self.player.active and self.holding:
            C.OVERLAYS.clear()
            done = self.player.frame(self.cv, self.have_favs)
            if done is not None and not self.player.active:
                self.park_on(done)
            else:
                self.ticker.frame()
                return
        now = time.monotonic()
        offset = 0
        if self.holding and not self.pinned:
            if now - self.hold_start >= self.hold_time(self.cards[self.cur]):
                self.start_scroll()
        if not self.holding:
            offset = int((now - self.scroll_start) * SCROLL_PX_PER_SEC)
            if offset >= CARD_STRIDE:
                self.start_hold(self.nxt)
                offset = 0
        self.cv.clear()
        C.OVERLAYS.clear()
        C.draw_card(self.cv, -offset, self.cards[self.cur], self.games, self.teams)
        if offset > 0:
            C.draw_card(self.cv, CARD_STRIDE - offset, self.cards[self.nxt], self.games, self.teams)
        if self.pinned and offset == 0:
            self.draw_pin_corners()
        self.ticker.frame()

    def draw_pin_corners(self):
        L, W, H = 6, P.PANEL_W, P.PANEL_H
        for x, y, w, h in ((0, 0, L, 1), (0, 0, 1, L), (W - L, 0, L, 1), (W - 1, 0, 1, L), (0, H - 1, L, 1),
                           (0, H - L, 1, L), (W - L, H - 1, L, 1), (W - 1, H - L, 1, L)):
            self.cv.fill(x, y, w, h, P.CYAN)


WIFI_STATUS = "/run/sportsball/wifi.json"


def wifi_message():
    """The Wi-Fi portal's status while it runs (setup or joining), else None."""
    try:
        with open(WIFI_STATUS) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    return st if st.get("mode") in ("setup", "joining") else None


def remote_urls():
    """The web remote's address by name and by IP (for the QR card)."""
    ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 53))       # no packet is sent; just picks the route
            ip = s.getsockname()[0]
    except OSError:
        pass
    name = f"http://{socket.gethostname()}.local:8080"
    return name, (f"http://{ip}:8080" if ip else "")


def sd_notify(msg):
    """systemd's notify protocol (Type=notify + WatchdogSec in the service)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.send(msg.encode())
    except OSError:
        pass


def open_screen():
    """With the TV off there is no HDMI signal and SDL says "kmsdrm not
    available". Wait for the TV instead of exiting: a crash loop restarts us
    thousands of times overnight and makes the updater roll back a good release."""
    import pygame
    from display import Screen
    waiting = False
    while True:
        try:
            return Screen()
        except pygame.error as e:
            pygame.display.quit()
            if not waiting:
                print(f"[Screen] {e}; waiting for the TV", flush=True)
                sd_notify("READY=1")
                waiting = True
            sd_notify("WATCHDOG=1")
            time.sleep(10)


def run_screen():
    import announcer
    import cec
    import remote
    import sfx
    teams, favs = load_config()
    fb = P.new_framebuffer()
    crawl = Crawl(fb, teams)
    crawl.have_favs = bool(favs)
    screen = open_screen()
    sfx.init()
    announcer.start()
    crawl.player.hooks = [sfx.hook, announcer.hook]
    crawl.on_focus = announcer.focus

    def red_zone_alert(g):
        sfx.play("flash")
        announcer.red_zone(g)
    crawl.on_red_zone = red_zone_alert
    extras.start(teams, favs)
    weather.start()
    cec.start()
    fetcher = Fetcher(teams, favs)
    fetcher.start()
    remote.start(crawl)
    crawl.fetcher = fetcher
    delay_buf = Delay()
    seen, cfg_version, level, level_at, wifi_at = 0, settings.version, 255, 0.0, 0.0
    extras_seen, slow_at, beat_at = 0, 0.0, 0.0
    fps_at, fps_n, worst, last = time.monotonic(), 0, 0.0, time.monotonic()
    sd_notify("READY=1")
    while True:
        now = time.monotonic()
        # Frame rate for the remote's health table: average + slowest frame, per 10 s.
        fps_n, worst, last = fps_n + 1, max(worst, now - last), now
        if now - fps_at >= 10:
            crawl.fps = (fps_n / (now - fps_at), worst * 1000)
            fps_at, fps_n, worst = now, 0, 0.0
        if now - beat_at > 10:             # the frame loop is alive (WatchdogSec)
            beat_at = now
            sd_notify("WATCHDOG=1")
        if now - wifi_at > 2:
            wifi_at = now
            crawl.override = wifi_message()
            try:
                style.handoff("classic", settings.get)   # setup finished: back to the look they picked
            except Exception as e:  # noqa: BLE001
                print(f"[Look] {e}", flush=True)
        if now - slow_at > 60:
            slow_at = now
            C.REMOTE_URLS = remote_urls()
        C.SHOW_ODDS = settings.get("show_odds")
        crawl.red_zone = settings.get("red_zone")
        # Not on a new ticker: until the clock is set and setup is done, the welcome
        # and setup cards stay up (a blank saver at a wrong "midnight" looks dead).
        crawl.set_saver(bool(settings.get("saver") and crawl.live_demo is None
                             and clock_ok() and crawl.games
                             and (settings.get("setup_done") or settings.get("favorites"))
                             and settings.in_hours(time.localtime().tm_hour, settings.get("saver_start"),
                                                   settings.get("saver_end"))
                             and not any(g.state == IN for g in crawl.games)))
        if extras.version != extras_seen and crawl.live_demo is None:
            extras_seen = extras.version
            crawl.extra = extras.snapshot()
            delay_buf.shown = None         # rebuild the cards with them
        if settings.version != cfg_version:       # teams/favorites/filter edited in the remote
            cfg_version = settings.version
            teams, favs = load_config()
            fetcher.stopped = True
            fetcher = crawl.fetcher = Fetcher(teams, favs)
            fetcher.start()
            crawl.teams, crawl.have_favs = teams, bool(favs)
            extras.configure(teams, favs)
            delay_buf.reset()              # new teams show right away, no delay
            seen = 0
        if fetcher.version != seen:
            seen = fetcher.version
            delay_buf.push(seen, fetcher.games)
        if crawl.live_demo is not None:
            if crawl.live_demo.done:
                crawl.live_demo = None
                delay_buf.shown = None     # back to the real scores
            else:
                if crawl.live_demo.roundup_due():
                    announcer.roundup_now(crawl.live_demo.games)
                demo_games = crawl.live_demo.update()
                if demo_games:
                    crawl.set_games(demo_games, detect=False)
                    announcer.prefetch(demo_games)
        elif (games := delay_buf.due(settings.get("delay"))) is not None:
            crawl.set_games(games)
            announcer.prefetch(games)
            cec.update(games)
        remote.run_pending(crawl)
        crawl.frame()
        remote.take_snapshot(crawl)
        announcer.roundup_tick(crawl.games)
        announcer.tick()
        if time.monotonic() - level_at > 1:
            level, level_at = panel_level(), time.monotonic()
        screen.show(fb, level, C.OVERLAYS)


def run_png(out_dir):
    """Fetch once and write every card (with the strip) as a PNG."""
    from display import save_png
    teams, favs = load_config()
    f = Fetcher(teams, favs)
    f.full()
    os.makedirs(out_dir, exist_ok=True)
    extras.configure(teams, favs)
    extras._refresh()
    C.REMOTE_URLS = remote_urls()
    fb = P.new_framebuffer()
    crawl = Crawl(fb, teams)
    crawl.extra = extras.snapshot()
    crawl.set_games(f.games)
    for c in crawl.cards:          # logos load in the background
        for u in C.card_logos(c):
            logos.get(u)
    deadline = time.time() + 30
    while logos._pending and time.time() < deadline:
        time.sleep(0.2)
    for i, c in enumerate(crawl.cards):
        crawl.cur = i
        crawl.focus()
        crawl.ticker.focus_off = 70
        crawl.ticker.off += 37
        crawl.cv.clear()
        C.OVERLAYS.clear()
        C.draw_card(crawl.cv, 0, c, crawl.games, teams)
        crawl.ticker.frame()
        name = {C.GAME: lambda: f"{SPORT_LABELS[c.sport]}-{c.ref.away_abbr}-{c.ref.home_abbr}"}.get(
            c.type, lambda: ["header", "game", "nodata", "msg", "live", "favs", "nextup", "ranks", "standings",
                             "news", "remote"][c.type])()
        save_png(fb, os.path.join(out_dir, f"{i:03d}-{name}.png"), overlays=C.OVERLAYS)
    print(f"{len(crawl.cards)} cards -> {out_dir}")


def run_scenes(out_dir):
    """Fetch once; a contact sheet per demo scene (frames across its length)."""
    import numpy as np
    from PIL import Image
    from display import expand
    teams, favs = load_config()
    f = Fetcher(teams, favs)
    f.full()
    for g in f.games:
        for home in (False, True):
            logos.get(A.team_logo_url(g, home))
    deadline = time.time() + 30
    while logos._pending and time.time() < deadline:
        time.sleep(0.2)
    os.makedirs(out_dir, exist_ok=True)
    fb = P.new_framebuffer()
    cv = P.Canvas(fb[:P.PANEL_H])
    for name in A.DEMOS:
        ev = A.demo_event(name, f.games)
        if ev is None:
            print(f"{name}: no game of that sport")
            continue
        seq = A.sequence(ev, A.event_style(ev, bool(favs)), f.games)
        total = sum(d for d, _, _ in seq)
        tiles = []
        for k in range(12):
            t = total * k // 12
            for dur, draw, _snd in seq:
                if t < dur:
                    break
                t -= dur
            cv.clear()
            draw(cv, t, t * A.STEP_HZ // 1000)
            tiles.append(expand(fb[:P.PANEL_H], 3))
        rows = [np.hstack(tiles[r * 4:r * 4 + 4]) for r in range(3)]
        Image.fromarray(np.vstack(rows)).save(os.path.join(out_dir, f"{name}.png"))
        print(f"{name}: {ev.game.away_abbr}@{ev.game.home_abbr} {total} ms")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--png":
        run_png(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "--scenes":
        run_scenes(sys.argv[2])
    else:
        run_screen()
