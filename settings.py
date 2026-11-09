"""Settings: config.json next to this file, merged over DEFAULTS.

The web remote edits them live (update()); everything else reads them with
get() each time it needs one, so changes apply without a restart. The team
list and favorites are the exception: changing them rebuilds the Fetcher
(main.py watches `version`).

    teams       [[sport, "team query"], ...]  1 NFL 2 NBA 3 MLB 4 NHL 5 CFB 6 CBB;
                                              "" = every game of that sport
    favorites   "Colts, IU Hoosiers"          comma-separated team names
"""

import copy
import json
import os
import threading

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULTS = {
    "teams": [[1, ""], [5, ""], [3, ""], [2, ""], [4, ""], [6, ""]],
    "favorites": "",
    "brightness": 100,          # percent
    "night_dim": True,          # dim between night_start and night_end
    "night_start": 23,          # hour, local
    "night_end": 7,
    "night_level": 30,          # percent brightness at night
    "sound_fx": True,           # cutscene sound effects
    "announcer": True,          # read the play-by-play aloud
    "voice": "en-US-GuyNeural", # announcer voice (announcer.VOICES)
    "voice_radio": False,       # old AM-radio filter on the voice
    "roundups": True,           # announcer reads other live scores now and then
    "red_zone": True,           # cut to a football game when it reaches the red zone
    "show_odds": True,          # betting line on upcoming games
    "cfb_top25": True,          # college football: only favorites' and Top 25 teams' games
    "delay": 10,                # seconds to hold scores back, to match a streaming TV's lag
    "update_url": "https://github.com/matthewbu2/sportsball-pi/releases/latest/download",   # updater.py
    "auto_update": True,        # install new releases nightly
    "volume": 70,               # percent
    "muted": False,             # all sound off (the Control tab's mute button)
    "saver": True,              # overnight screensaver: the animations, silent, while nothing is live
    "saver_start": 0,           # hour, local
    "saver_end": 8,
    "pregame": True,            # announcer: "Kickoff in fifteen minutes..." for favorites
    "quiet_start": 23,          # no sound between these hours
    "quiet_end": 8,
    "cec": True,                # TV power over HDMI-CEC (when the TV answers)
    "cec_on_before": 30,        # turn the TV on this many minutes before a favorite plays
    "cec_off_at": 1,            # hour to turn the TV off again (only if we turned it on)
    "setup_done": False,        # the phone setup page (remote /setup) was finished
}

_lock = threading.Lock()
_cfg = {}
version = 0                     # bumps when teams/favorites/the college filter change


def load():
    global _cfg
    try:
        with open(PATH) as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    with _lock:
        _cfg = copy.deepcopy(DEFAULTS)
        _cfg.update(user)


def get(key):
    with _lock:
        return copy.deepcopy(_cfg[key])


def snapshot():
    with _lock:
        return copy.deepcopy(_cfg)


def update(changes):
    """Applies and saves changes (unknown keys are ignored)."""
    global version
    with _lock:
        changed_teams = False
        for k, v in changes.items():
            if k not in DEFAULTS:
                continue
            if isinstance(DEFAULTS[k], bool):
                v = bool(v)
            elif isinstance(DEFAULTS[k], int):
                v = int(v)
            if k in ("teams", "favorites", "cfb_top25", "setup_done") and v != _cfg.get(k):
                changed_teams = True
            _cfg[k] = v
        out = {k: _cfg[k] for k in DEFAULTS}
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f, indent=2)
            f.flush()
            os.fsync(f.fileno())     # a pulled plug can't leave a torn config
        os.replace(tmp, PATH)
        if changed_teams:
            version += 1


def in_hours(hour, start, end):
    """Whether hour falls in [start, end), wrapping past midnight."""
    return start <= hour < end if start <= end else hour >= start or hour < end


load()
