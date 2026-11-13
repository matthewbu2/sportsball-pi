"""Two looks on one Pi: the HD broadcast version (~/hd, sportsball-hd.service)
and the classic 8-bit LED ticker (~/ticker, sportsball.service). Only one
runs at a time (both want the screen, the sound and port 8080).

    installed()          -> ["hd", "classic"] (the ones on this Pi)
    current()            -> the one that is switched on
    switch_to(name)      -> carries the settings over, then a systemd job
                            stops this one and starts the other
    wanted()             -> the look the owner picked (remote or setup page), or None
    handoff(me, cfg)     -> called every few seconds by the running look:
                            setup screens (Wi-Fi hotspot, phone setup) are the
                            classic look's job, so HD lends it the screen
                            while they are needed, and classic gives it back
    fallback(unit)       -> a look's service crashed 5 times in 10 minutes
                            (systemd OnFailure -> sportsball-fallback@.service):
                            mark that version broken and put the other look on
                            the TV; the owner's pick comes back after the next
                            update of the broken one, or after a day
    ensure_units(here)   -> install the shared unit files (fallback, status
                            report) and switch the status timer on; called by
                            both looks at start, so old updaters needn't know

The same file is in both projects; keep them identical.
"""

import json
import os
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
LOOKS = {
    "hd": {"dir": os.path.join(HOME, "hd"), "unit": "sportsball-hd.service", "label": "HD broadcast"},
    "classic": {"dir": os.path.join(HOME, "ticker"), "unit": "sportsball.service", "label": "Classic 8-bit"},
}
UNIT_DIR = os.path.join(HOME, ".config", "systemd", "user")
KEEP_OWN = {"update_url"}          # settings each version keeps for itself
PREF = os.path.join(HOME, ".config", "sportsball", "look")    # the look the owner picked
WIFI_STATUS = "/run/sportsball/wifi.json"                      # wifi_setup.py (root side)
BROKEN = os.path.join(HOME, ".config", "sportsball", "broken.json")  # {look: {"version", "at"}}
BROKEN_FOR = 24 * 3600            # then try the owner's pick again
SHARED_UNITS = ("sportsball-fallback@.service", "sportsball-status.service", "sportsball-status.timer")


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


def wanted():
    """The owner's pick, or None if nobody has picked (then nothing moves)."""
    try:
        with open(PREF) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _remember(name):
    os.makedirs(os.path.dirname(PREF), exist_ok=True)
    with open(PREF + ".tmp", "w") as f:
        f.write(name + "\n")
    os.replace(PREF + ".tmp", PREF)


def setup_needed(cfg):
    """The TV has to show setup screens: the Wi-Fi hotspot is up, or nobody
    has done the phone setup yet. cfg = the running look's settings.get."""
    try:
        with open(WIFI_STATUS) as f:
            if json.load(f).get("mode") in ("setup", "joining"):
                return True
    except (OSError, ValueError):
        pass
    return not cfg("setup_done") and not cfg("favorites")


def version(name):
    try:
        with open(os.path.join(LOOKS[name]["dir"], "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_broken():
    try:
        with open(BROKEN) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_broken(d):
    os.makedirs(os.path.dirname(BROKEN), exist_ok=True)
    with open(BROKEN + ".tmp", "w") as f:
        json.dump(d, f)
    os.replace(BROKEN + ".tmp", BROKEN)


def broken(name):
    """This look's installed version kept crashing within the last day."""
    b = _read_broken().get(name)
    return bool(b) and b.get("version") == version(name) and time.time() - b.get("at", 0) < BROKEN_FOR


def _set_broken(name, on):
    d = _read_broken()
    if on:
        d[name] = {"version": version(name), "at": time.time()}
    elif name in d:
        del d[name]
    else:
        return
    _write_broken(d)


def trouble():
    """For the remote: labels of looks that are benched for crashing."""
    return [LOOKS[k]["label"] for k in installed() if broken(k)]


_handed = False


def handoff(me, cfg):
    """See the module doc. Returns True once a switch has been started."""
    global _handed
    if _handed:
        return False
    pick = wanted()
    if me == "hd" and setup_needed(cfg) and "classic" in installed():
        print("[Look] setup screens needed: handing the TV to the classic look", flush=True)
        _handed = switch_to("classic", remember=False)
    elif (pick and pick != me and pick in installed() and not broken(pick)
          and not (me == "classic" and setup_needed(cfg))):
        print(f"[Look] back to the look the owner picked ({pick})", flush=True)
        _handed = switch_to(pick)
    return _handed


def _report():
    """Ask for a status report now (status.py; it checks share_status itself)."""
    subprocess.run(["systemctl", "--user", "start", "--no-block", "sportsball-status.service"],
                   capture_output=True)


def fallback(unit):
    """See the module doc. Runs in sportsball-fallback@<unit>.service."""
    name = next((k for k, v in LOOKS.items() if v["unit"] == unit), None)
    if name is None:
        print(f"[Look] fallback: unknown unit {unit}", flush=True)
        return
    sc = ["systemctl", "--user"]
    # systemd runs OnFailure on every crash, also while it is still going to
    # restart the unit; act only once it has given up (start limit hit).
    out = subprocess.run(sc + ["show", unit, "-p", "ActiveState", "-p", "Result"], capture_output=True, text=True).stdout
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if props.get("ActiveState") != "failed":
        return
    print(f"[Look] {unit} gave up ({props.get('Result')})", flush=True)
    _set_broken(name, True)
    other = "classic" if name == "hd" else "hd"
    if other in installed() and not broken(other):
        print(f"[Look] {name} {version(name)} keeps crashing: switching the TV to {other}", flush=True)
        _carry_settings(name, other)
        subprocess.run(sc + ["disable", "--now", unit], capture_output=True)
        subprocess.run(sc + ["reset-failed", unit], capture_output=True)
        subprocess.run(sc + ["enable", "--now", LOOKS[other]["unit"]], capture_output=True)
    else:
        # Nothing to fall back to: keep trying this one, slowly (the old
        # behaviour; a dead TV picture is worse than retrying).
        print(f"[Look] {name} {version(name)} keeps crashing and there is no other look to show; "
              "trying again in a minute", flush=True)
        time.sleep(60)
        subprocess.run(sc + ["reset-failed", unit], capture_output=True)
        subprocess.run(sc + ["start", unit], capture_output=True)
    _report()


def ensure_units(here):
    """See the module doc. Never raises (a look must start even if this fails)."""
    try:
        os.makedirs(UNIT_DIR, exist_ok=True)
        changed = False
        for name in SHARED_UNITS:
            src, dst = os.path.join(here, "setup", name), os.path.join(UNIT_DIR, name)
            if not os.path.exists(src):
                continue
            with open(src, "rb") as f:
                new = f.read()
            try:
                with open(dst, "rb") as f:
                    if f.read() == new:
                        continue
            except OSError:
                pass
            shutil.copy(src, dst)
            changed = True
        sc = ["systemctl", "--user"]
        if changed:
            subprocess.run(sc + ["daemon-reload"], capture_output=True)
        if subprocess.run(sc + ["is-enabled", "sportsball-status.timer"], capture_output=True,
                          text=True).stdout.strip() != "enabled":
            subprocess.run(sc + ["enable", "--now", "sportsball-status.timer"], capture_output=True)
    except Exception as e:  # noqa: BLE001
        print(f"[Look] shared units: {e!r}", flush=True)


def switch_to(name, remember=True):
    """Hand the screen to the other look (returns at once; this process is
    stopped a second later by a transient systemd job). remember=False: a
    loan for the setup screens; the owner's pick stays."""
    if name not in installed():
        raise ValueError(f"{name} is not installed")
    if remember:
        _remember(name)
        _set_broken(name, False)     # the owner asked for it: give it another go
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


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fallback"] and len(sys.argv) > 2:
        fallback(sys.argv[2])
    else:
        print("usage: style.py --fallback UNIT")
        sys.exit(2)
