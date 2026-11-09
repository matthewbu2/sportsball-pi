"""TV power over HDMI-CEC (cec-ctl, /dev/cec0).

  * cec_on_before minutes before a favorite's game starts (or while one is
    live), a TV in standby is switched on and pointed at the Pi.
  * At cec_off_at o'clock, if the ticker was the one that turned the TV
    on and no favorite is playing, it goes back to standby. A TV someone
    turned on by hand is never switched off.

Many TVs ship with CEC off (Samsung "Anynet+", LG "SimpLink", Sony "Bravia
Sync", others "HDMI-CEC"); until the TV answers, this does nothing.
"""

import subprocess
import threading
import time

import cards as C
import settings
from espn import IN, PRE

DEV = "/dev/cec0"
_games = []
_we_turned_on = False
_handled = set()          # game keys we already woke the TV for
state = "unknown"         # last power state seen: on / standby / no answer


def _ctl(*args, timeout=8):
    try:
        r = subprocess.run(["cec-ctl", "-d", DEV, *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def power():
    """'on', 'standby', or None if the TV doesn't answer."""
    out = _ctl("--to", "0", "--give-device-power-status")
    for line in out.splitlines():
        if "pwr-state" in line:
            return "standby" if "standby" in line else "on"
    return None


def tv_on():
    _ctl("--to", "0", "--image-view-on")
    _ctl("--to", "0", "--active-source", "phys-addr=1.0.0.0")


def tv_off():
    _ctl("--to", "0", "--standby")


def update(games):
    global _games
    _games = games


def _loop():
    global _we_turned_on, state
    _ctl("--playback", "-o", "Sportsball")
    while True:
        try:
            if settings.get("cec"):
                _step()
        except Exception as e:  # noqa: BLE001
            print(f"[CEC] {e}", flush=True)
        time.sleep(60)


def _step():
    global _we_turned_on, state
    p = power()
    state = p or "no answer"
    if p is None:
        return
    now = time.time()
    soon = now + settings.get("cec_on_before") * 60
    favs = [g for g in _games if g.fav_mask]
    live = [g for g in favs if g.state == IN]
    upcoming = [g for g in favs if g.state == PRE and now - 3600 < C.iso_to_time(g.date) <= soon]
    for g in live + upcoming:
        if g.key in _handled:
            continue
        _handled.add(g.key)
        if p == "standby":
            print(f"[CEC] TV on for {g.away_abbr} at {g.home_abbr}", flush=True)
            tv_on()
            _we_turned_on = True
            return
    t = time.localtime()
    if _we_turned_on and t.tm_hour == settings.get("cec_off_at") and not live and p == "on":
        print("[CEC] TV to standby (night)", flush=True)
        tv_off()
        _we_turned_on = False


def start():
    threading.Thread(target=_loop, daemon=True).start()
