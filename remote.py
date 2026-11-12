"""Web remote on port 8080: http://sportsball.local:8080/

A phone-friendly page with three tabs:
  Demos     a button per scene
  Control   pin/skip the card on screen, TV on/off (CEC), test the voice
  Settings  teams, favorites, brightness and night dimming, sound effects,
            announcer, volume, quiet hours, TV power -- saved to config.json
            and applied live

/snap.png is the current frame (for checking the screen remotely).
Requests land on the HTTP thread; anything that touches the crawl is
queued and run on the frame loop (run_pending).
"""

import io
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anim as A
import settings
import style
from espn import IN, SPORT_LABELS

PORT = 8080
HERE = os.path.dirname(os.path.abspath(__file__))
_pending = []
_lock = threading.Lock()
_crawl = None
_snap_want = threading.Event()   # /snap.png asks the frame loop for a finished frame
_snap_done = threading.Event()
_snap = None                     # (framebuffer, QR overlays)

_GROUPS = [
    ("Scores", ["td", "fg", "safety", "two", "hr", "grandslam", "walkoff", "goal", "hattrick", "dunk", "three",
                "buzzer"]),
    ("Moments", ["int", "fumble", "lead", "start", "upset", "break", "final"]),
    ("My teams", ["sad", "short", "victory", "loss"]),
]
_LABELS = {"td": "Touchdown", "fg": "Field goal", "safety": "Safety", "two": "Two-point", "hr": "Home run",
           "grandslam": "Grand slam", "walkoff": "Walk-off", "goal": "Hockey goal", "hattrick": "Hat trick",
           "dunk": "Slam dunk", "three": "Three", "buzzer": "Buzzer beater", "int": "Interception",
           "fumble": "Fumble", "lead": "Lead change", "start": "Game start", "upset": "Upset alert",
           "break": "Halftime roundup", "final": "Final", "sad": "Opponent scores", "short": "Short flash",
           "victory": "Victory", "loss": "Loss"}

_CARD_NAMES = ["header", "game", "nodata", "msg", "live", "favs", "nextup", "ranks", "standings", "news", "remote", "setup"]

FLAVOR = "8-BIT"
FEATURES = [   # (setting, label, on = setting True)  -- switches on the Features tab
    ("muted", "Sound", False),
    ("announcer", "Announcer voice", True),
    ("roundups", "Around-the-league score roundups", True),
    ("pregame", "Announce favorites' kickoffs", True),
    ("red_zone", "Jump to red-zone drives", True),
    ("saver", "Overnight screensaver", True),
]


def _page_html():
    """The remote page (web/remote.html, read each time so edits show on refresh)."""
    with open(os.path.join(HERE, "web", "remote.html"), encoding="utf-8") as f:
        return f.read()


def _setup_html():
    """The phone setup page (web/setup.html, shared by both looks)."""
    with open(os.path.join(HERE, "web", "setup.html"), encoding="utf-8") as f:
        return f.read()


_teams_cache = {}
ACCOUNT_DIR = "/run/sportsball/account"    # handed to the root side (account.py)


def _account_state():
    try:
        with open(os.path.join(ACCOUNT_DIR, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _account_available():
    """The login step is offered once: during first setup, while nobody can log in yet."""
    return bool(_account_state().get("available")) and not settings.get("setup_done")


def _root_request(req):
    """Hands req to the root side (account.py) and waits for its answer."""
    rid = os.urandom(8).hex()
    tmp = os.path.join(ACCOUNT_DIR, "request.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({**req, "id": rid}, f)
    os.replace(tmp, os.path.join(ACCOUNT_DIR, "request.json"))
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(0.5)
        res = _account_state().get("result") or {}
        if res.get("id") == rid:
            return {k: v for k, v in res.items() if k != "id"}
    return {"ok": False, "error": "No answer from the ticker. Try again in a minute."}


def _make_account(user, password):
    if not _account_available():
        return {"ok": False, "error": "This ticker already has a login, or setup is finished."}
    return _root_request({"user": user, "password": password})


def _names():
    """This ticker's name, and the one to suggest: if another ticker already
    answers to it, avahi has fallen back to name-2 here (see wifi_setup.py)."""
    name = socket.gethostname()
    r = subprocess.run(["busctl", "call", "org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server",
                        "GetHostName"], capture_output=True, text=True)
    mdns = r.stdout.split('"')[1] if r.returncode == 0 and '"' in r.stdout else name
    return {"name": name, "suggest": mdns}


def _teams(sport):
    """[{name, abbr, logo}] of every team in a sport (ESPN), cached."""
    if sport not in _teams_cache:
        from espn import SESSION, SPORT_PATHS
        url = f"https://site.api.espn.com/apis/site/v2/sports/{SPORT_PATHS[sport]}/teams"
        d = SESSION.get(url, params={"limit": 1000}, timeout=15).json()
        out = []
        for t in d["sports"][0]["leagues"][0]["teams"]:
            t = t["team"]
            logo = next((l["href"] for l in t.get("logos") or [] if l.get("href")), "")
            out.append({"name": t.get("displayName", ""), "abbr": t.get("abbreviation", ""), "logo": logo})
        _teams_cache[sport] = out
    return _teams_cache[sport]


def _ago(t):
    if not t:
        return "never"
    s = int(time.time() - t)
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


_sys_cache = (0.0, [])


def _system_rows():
    """Slow-changing rows (timers, disk, last OS update), refreshed every minute."""
    global _sys_cache
    if time.time() - _sys_cache[0] < 60:
        return _sys_cache[1]
    rows = []
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            c = int(f.read()) / 1000
        rows.append(("CPU temperature", f"{c:.0f} C", c < 75))
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        rows.append(("Up since boot", f"{int(up // 3600)} h {int(up % 3600 // 60)} min", True))
    except (OSError, ValueError):
        pass
    st = os.statvfs("/")
    free = st.f_bavail * st.f_frsize / 1e9
    rows.append(("SD card free", f"{free:.1f} GB", free > 1))
    try:
        with open("/var/log/sportsball-update.log") as f:
            tail = f.read()[-4000:].strip().splitlines()
        ex = next((l for l in reversed(tail) if l.startswith("exit ")), "")
        rows.append(("Last system update", ex.replace("exit 0", "OK").replace("exit ", "failed, code ") or "running",
                     ex in ("", "exit 0")))
    except OSError:
        rows.append(("Last system update", "not yet", True))
    for scope, unit, label in (([], "sportsball-update.timer", "Next system update"),
                               ([], "sportsball-reboot.timer", "Next reboot"),
                               (["--user"], "sportsball-selfupdate.timer", "Next ticker update")):
        out = subprocess.run(["systemctl", *scope, "show", unit, "-p", "NextElapseUSecRealtime", "--value"],
                             capture_output=True, text=True).stdout.strip()
        rows.append((label, out[4:20] if out else "not scheduled", bool(out)))
    _sys_cache = (time.time(), rows)
    return rows


def _health():
    import announcer
    import extras
    import updater
    import weather
    f = _crawl.fetcher
    rows = []
    if f is not None:
        fresh = time.time() - f.last_ok < 15 * 60
        rows.append(("Scores", f"updated {_ago(f.last_ok)}", fresh))
        if f.last_error:
            rows.append(("Last score error", f.last_error, fresh))
    rows.append(("Standings, rankings, news", f"updated {_ago(extras.updated_at)}"
                 + (f" ({extras.last_error})" if extras.last_error else ""), time.time() - extras.updated_at < 3600))
    rows.append(("Weather", f"updated {_ago(weather.updated_at)}", time.time() - weather.updated_at < 3600))
    v = announcer.voice_state()
    rows.append(("Announcer", v, v != "offline voice (Piper)"))
    delay = settings.get("delay")
    rows.append(("Broadcast delay", f"{delay} s" if delay else "off", True))
    fps = getattr(_crawl, "fps", None)
    if fps:
        rows.append(("Frame rate", f"{fps[0]:.0f} fps, slowest frame {fps[1]:.0f} ms", fps[0] >= 25))
    rows.append(("Version", updater.current_version(), True))
    return rows + _system_rows()


def _page():
    # Only demos for the sports the ticker follows (football-only in the
    # fall, hoops and hockey in the winter).
    sports = {s for s, _q in settings.get("teams")}
    parts = []
    for title, names in _GROUPS:
        names = [n for n in names if sports & set(A.SPORT_FAMILY[A.DEMOS[n][0]])]
        btns = "".join(f'<button onclick="go(\'{n}\')">{_LABELS[n]}</button>' for n in names)
        if btns:
            parts.append(f"<h2>{title}</h2><div class=g>{btns}</div>")
    import announcer
    voices = "".join(f'<option value="{v}">{label}</option>' for v, label in announcer.VOICES.items())
    return _page_html().replace("@FLAVOR@", FLAVOR).replace("@REPLAYS@", "").replace("@DEMOS@", "".join(parts)).replace("@VOICES@", voices).replace("@SPORTS@", json.dumps(list(SPORT_LABELS))).encode()


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, _page(), "text/html; charset=utf-8")
        elif self.path.startswith("/snap.png"):
            from PIL import Image
            from display import expand
            buf = io.BytesIO()
            # Copying the framebuffer from this thread could catch it mid-draw
            # (cards just cleared); take_snapshot() hands over a whole frame.
            shot = _frame()
            if not shot:
                return self._send(503, b"busy")
            fb, overlays = shot
            Image.fromarray(expand(fb, 4, overlays=overlays)).save(buf, "PNG")
            self._send(200, buf.getvalue(), "image/png")
        elif self.path in ("/status", "/status?health"):
            import cec
            games = _crawl.games
            cur = _crawl.player.current
            playing = None
            if cur is not None:
                playing = f"{A.abbr_of(cur.game, cur.home)} {A.KIND_NAMES[cur.kind]}"
            import updater
            if "health" in self.path:
                return self._json(_health())
            self._json({"games": len(games), "live": sum(1 for g in games if g.state == IN), "playing": playing,
                        "pinned": _crawl.pinned, "cec": cec.state, "version": updater.current_version(),
                        "muted": settings.get("muted"), "looks": style.labels(), "look_wanted": style.wanted(), "setup_done": bool(settings.get("setup_done") or settings.get("favorites")),
                        "now": _crawl.now_info() if hasattr(_crawl, "now_info") else None,
                        "features": [(k, label, bool(settings.get(k)) == on) for k, label, on in FEATURES],
                        "card": f"{_CARD_NAMES[_crawl.cards[_crawl.cur].type]} {_crawl.cur + 1}/{len(_crawl.cards)}"})
        elif self.path == "/board":
            self._json(_crawl.board() if hasattr(_crawl, "board") else [])
        elif self.path in ("/look/hd.jpg", "/look/classic.jpg"):     # the setup page's previews
            with open(os.path.join(HERE, "web", "look-" + self.path[6:]), "rb") as f:
                self._send(200, f.read(), "image/jpeg")
        elif self.path.startswith("/font/"):
            name = os.path.basename(self.path[6:])
            path = os.path.join(HERE, "assets", "fonts", name)
            if not name.endswith(".ttf") or not os.path.isfile(path):
                return self._send(404, b"no")
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "font/ttf")
            self.send_header("Cache-Control", "max-age=604800")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/settings":
            self._json(settings.snapshot())
        elif self.path == "/account":
            self._json({"available": _account_available()})
        elif self.path == "/name":
            self._json(_names())
        elif self.path.startswith("/setup"):
            import socket
            self._send(200, _setup_html().replace("@HOST@", f"{socket.gethostname()}.local:{PORT}").replace("@HOST_ONLY@", f"{socket.gethostname()}.local").replace("@PORT@", str(PORT)).encode(),
                       "text/html; charset=utf-8")
        elif self.path.startswith("/teams?sport="):
            try:
                sport = int(self.path.split("=", 1)[1])
                if not 1 <= sport <= 6:
                    raise ValueError
                self._json(_teams(sport))
            except ValueError:
                self._send(400, b"bad sport")
            except Exception as e:  # noqa: BLE001 - ESPN down: the page says no teams found
                self._send(502, str(e).encode())
        else:
            self._send(404, b"not found")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self.path.startswith("/demo/"):
            name = self.path[len("/demo/"):]
            if name not in A.DEMOS:
                return self._send(404, b"no such demo")
            cmd = ("demo", name)
        elif self.path == "/settings":
            try:
                new = json.loads(body)
                look = new.pop("look", None)          # the setup page's "Pick a look"
                if look and look not in style.installed():
                    raise ValueError(f"{look} is not installed")
            except (ValueError, TypeError, AttributeError) as e:
                return self._send(400, str(e).encode())
            if look:
                style._remember(look)    # before setup_done lands, so classic hands back the right way
            settings.update(new)
            if look:
                style.switch_to(look)    # after: carries the finished setup over
            return self._send(200, b"ok")
        elif self.path in ("/pin", "/skip", "/voice/test", "/livedemo"):
            cmd = (self.path.strip("/"), None)
        elif self.path == "/note":
            return self._send(200, _save_note(body.decode("utf-8", "replace")).encode())
        elif self.path.startswith("/show/"):
            from urllib.parse import unquote
            cmd = ("show", unquote(self.path[len("/show/"):]))
        elif self.path.startswith("/toggle/"):
            k = self.path[len("/toggle/"):]
            if k not in {f[0] for f in FEATURES}:
                return self._send(404, b"no such switch")
            settings.update({k: not settings.get(k)})
            return self._send(200, b"ok")
        elif self.path.startswith("/style/"):
            try:
                switched = style.switch_to(self.path[len("/style/"):])
            except (ValueError, OSError) as e:
                return self._send(400, str(e).encode())
            return self._send(200, b"switching" if switched else b"already on")
        elif self.path == "/mute":
            settings.update({"muted": not settings.get("muted")})
            return self._send(200, b"ok")
        elif self.path == "/update":
            # A transient unit, so it survives the ticker restarting under it.
            log = os.path.join(HERE, "ticker.log")
            subprocess.Popen(["systemd-run", "--user", "--collect", "--unit", "sportsball-update-now",
                              "-p", f"StandardOutput=append:{log}", "-p", f"StandardError=append:{log}",
                              "/usr/bin/python3", "-u", os.path.join(HERE, "updater.py"), "--force"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._send(200, b"ok")
        elif self.path == "/account":
            try:
                req = json.loads(body)
            except ValueError:
                return self._send(400, b"bad request")
            return self._json(_make_account(str(req.get("user", "")), str(req.get("password", ""))))
        elif self.path == "/name":
            try:
                name = str(json.loads(body).get("name", ""))
            except (ValueError, AttributeError):
                return self._send(400, b"bad request")
            return self._json(_root_request({"kind": "name", "name": name}))
        elif self.path in ("/tv/on", "/tv/off"):
            import cec
            threading.Thread(target=cec.tv_on if self.path == "/tv/on" else cec.tv_off, daemon=True).start()
            return self._send(200, b"ok")
        else:
            return self._send(404, b"not found")
        with _lock:
            _pending.append(cmd)
        self._send(200, b"ok")

    def log_message(self, *args):
        pass


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        import sys
        if not isinstance(sys.exc_info()[1], ConnectionError):   # a phone dropping off is normal
            super().handle_error(request, client_address)


def start(crawl):
    global _crawl
    _crawl = crawl
    srv = _Server(("", PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[Remote] http://sportsball.local:{PORT}/", flush=True)


NOTES = os.path.join(HERE, "notes")


def _frame():
    """A finished frame from the frame loop: (framebuffer, overlays) or None."""
    _snap_done.clear()
    _snap_want.set()
    return _snap if _snap_done.wait(2) else None


def _save_note(text):
    """notes/<time>/: the note, the TV picture, the games and recent log, for
    working through later."""
    import dataclasses
    from PIL import Image
    from display import expand
    d = os.path.join(NOTES, time.strftime("%Y-%m-%d_%H%M%S"))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "note.txt"), "w") as f:
        f.write(text.strip() + "\n")
    shot = _frame()
    if shot:
        Image.fromarray(expand(shot[0], 4, overlays=shot[1])).save(os.path.join(d, "screen.png"))
    c = _crawl.cards[_crawl.cur]
    info = {"card": f"{_CARD_NAMES[c.type]} {_crawl.cur + 1}/{len(_crawl.cards)}",
            "card_game": c.ref.key if c.type in (1, 6) else None, "settings": settings.snapshot(),
            "health": _health(), "games": [dataclasses.asdict(g) for g in _crawl.games]}
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump(info, f, indent=1, default=str)
    try:
        with open(os.path.join(HERE, "ticker.log")) as f:
            tail = f.readlines()[-300:]
        with open(os.path.join(d, "log.txt"), "w") as f:
            f.writelines(tail)
    except OSError:
        pass
    n = len(os.listdir(NOTES))
    print(f"[Note] {text.strip()[:60]}", flush=True)
    return f"Saved. {n} note{'s' if n != 1 else ''} so far."


def take_snapshot(crawl):
    """Frame loop, after drawing: hand a finished frame to /snap.png."""
    global _snap
    if _snap_want.is_set():
        import cards
        _snap = (crawl.fb.copy(), list(cards.OVERLAYS))
        _snap_want.clear()
        _snap_done.set()


def run_pending(crawl):
    with _lock:
        cmds = _pending[:]
        _pending.clear()
    for cmd, arg in cmds:
        if cmd == "demo":
            ev = A.demo_event(arg, crawl.games)
            if ev is not None:
                crawl.player.add([ev])
        elif cmd == "pin":
            crawl.pinned = not crawl.pinned
        elif cmd == "skip":
            crawl.skip()
        elif cmd == "show" and hasattr(crawl, "show"):
            crawl.show(arg)
        elif cmd == "livedemo":
            from livedemo import LiveDemo
            # Pressed again mid-demo: restart from the real games, not the demo's.
            crawl.live_demo = LiveDemo(crawl.live_demo.source if crawl.live_demo else crawl.games)
        elif cmd == "voice/test":
            ev = A.demo_event("td", crawl.games)
            if ev is not None:
                import announcer
                announcer.hook("", ev, 0)
