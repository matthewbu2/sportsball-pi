"""Two looks on one Pi: the HD broadcast version (~/hd, sportsball-hd.service)
and the classic 8-bit LED ticker (~/ticker, sportsball.service). Only one
runs at a time (both want the screen, the sound and port 8080).

    installed()          -> ["hd", "classic"] (the ones on this Pi)
    current()            -> the one that is switched on
    switch_to(name)      -> carries the settings over, then a systemd job
                            stops this one and starts the other

The same file is in both projects; keep them identical.
"""

import json
import os
import subprocess

HOME = os.path.expanduser("~")
LOOKS = {
    "hd": {"dir": os.path.join(HOME, "hd"), "unit": "sportsball-hd.service", "label": "HD broadcast"},
    "classic": {"dir": os.path.join(HOME, "ticker"), "unit": "sportsball.service", "label": "Classic 8-bit"},
}
UNIT_DIR = os.path.join(HOME, ".config", "systemd", "user")
KEEP_OWN = {"update_url"}          # settings each version keeps for itself


def installed():
    return [k for k, v in LOOKS.items()
            if os.path.exists(os.path.join(v["dir"], "main.py")) and os.path.exists(os.path.join(UNIT_DIR, v["unit"]))]


def _enabled(name):
    r = subprocess.run(["systemctl", "--user", "is-enabled", LOOKS[name]["unit"]], capture_output=True, text=True)
    return r.stdout.strip() == "enabled"


def current():
    for k in LOOKS:
        if k in installed() and _enabled(k):
            return k
    return installed()[0] if installed() else "hd"


def labels():
    """For the remote: [(name, label, is_current)]."""
    cur = current()
    return [(k, LOOKS[k]["label"], k == cur) for k in installed()]


def _carry_settings(src, dst):
    """Teams, favorites, sound... follow you to the other look (it ignores
    settings it doesn't have)."""
    try:
        with open(os.path.join(LOOKS[src]["dir"], "config.json")) as f:
            mine = json.load(f)
    except (OSError, ValueError):
        return
    path = os.path.join(LOOKS[dst]["dir"], "config.json")
    try:
        with open(path) as f:
            theirs = json.load(f)
    except (OSError, ValueError):
        theirs = {}
    theirs.update({k: v for k, v in mine.items() if k not in KEEP_OWN})
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(theirs, f, indent=1)
    os.replace(tmp, path)


def switch_to(name):
    """Hand the screen to the other look (returns at once; this process is
    stopped a second later by a transient systemd job)."""
    if name not in installed():
        raise ValueError(f"{name} is not installed")
    cur = current()
    if name == cur:
        return False
    _carry_settings(cur, name)
    other = LOOKS[cur]["unit"]
    target = LOOKS[name]["unit"]
    subprocess.Popen(["systemd-run", "--user", "--collect", "--unit", "sportsball-switch",
                      "/bin/sh", "-c", f"sleep 1; systemctl --user disable --now {other}; "
                                       f"systemctl --user enable --now {target}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
