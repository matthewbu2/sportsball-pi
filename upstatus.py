"""Live progress of a self-update, for the web remote (shared by both looks).

The updater (a separate process, so it outlives the ticker restarting under
it) writes ~/.config/sportsball/update.json at every step; the remote reads it
for /status and the page shows it while "Check for updates" runs:

  {"state": "running" | "ok" | "error",
   "step": "check" | "download" | "verify" | "install" | "restart" | "watch" | "done",
   "text": "Downloading 2026.11.19.151910", "pct": 0-100 or null,
   "look": "hd" | "classic", "from": "...", "to": "...", "manual": bool,
   "started": unix, "updated": unix, "boot": boot id}

Restart notice: files the root side runs (Wi-Fi setup, account/rename)
are only reloaded at boot, so an update that changes them sets a marker for
this boot (reboot.json); apt's /run/reboot-required counts too. Both clear
themselves after a reboot.
"""

import hashlib
import json
import os
import time

DIR = os.path.expanduser("~/.config/sportsball")
STATUS = os.path.join(DIR, "update.json")
REBOOT = os.path.join(DIR, "reboot.json")
ROOT_FILES = ("wifi_setup.py", "account.py")      # run by root (sportsball-wifi.service)
WIFI_UNIT = "/etc/systemd/system/sportsball-wifi.service"
STALE_S = 150          # a "running" status not touched this long: the updater died


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write(path, obj):
    os.makedirs(DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class Progress:
    """One update run. Every call rewrites the status file (cheap: a few
    hundred bytes) and never raises -- reporting must not break an update."""

    def __init__(self, look, manual):
        now = time.time()
        self.s = {"state": "running", "step": "check", "text": "Checking for a new version",
                  "pct": None, "look": look, "from": "", "to": "", "manual": bool(manual),
                  "started": now, "updated": now, "boot": boot_id()}
        self._last = 0.0
        self._flush()

    def _flush(self):
        self.s["updated"] = time.time()
        try:
            _write(STATUS, self.s)
        except OSError:
            pass

    def step(self, step, text, pct=None, **kw):
        self.s.update(step=step, text=text, pct=pct, **kw)
        self._flush()

    def pct(self, pct, text=None):
        """Download progress; written at most twice a second."""
        now = time.time()
        if now - self._last < 0.5 and pct < 100:
            return
        self._last = now
        self.s["pct"] = int(pct)
        if text:
            self.s["text"] = text
        self._flush()

    def done(self, text, **kw):
        self.s.update(state="ok", step="done", text=text, pct=None, **kw)
        self._flush()

    def fail(self, text):
        self.s.update(state="error", text=text, pct=None)
        self._flush()


def start_marker(look, manual):
    """Before the updater process starts (the remote's button): the page shows
    progress at once, and a second tap sees an update already running."""
    return Progress(look, manual)


def read():
    """The last run's status, or {}; a run whose updater vanished (killed,
    power cut, reboot) is reported as an error rather than running forever."""
    s = _read(STATUS)
    if s.get("state") == "running":
        if s.get("boot") != boot_id() or time.time() - float(s.get("updated") or 0) > STALE_S:
            s.update(state="error", text="The update stopped before it finished. Try again.")
    return s


def busy():
    return read().get("state") == "running"


# ---------------------------------------------------------------- restart
def _digest(d, names):
    h = hashlib.sha256()
    for n in names:
        try:
            with open(os.path.join(d, n), "rb") as f:
                h.update(n.encode() + b"\0" + f.read())
        except OSError:
            h.update(n.encode() + b"\0-")
    return h.hexdigest()


def _root_runs_from(d):
    """The Wi-Fi/account service (root) runs its code from this folder."""
    try:
        with open(WIFI_UNIT) as f:
            unit = f.read()
    except OSError:
        return False
    d = os.path.realpath(d)
    return any(line.startswith("ExecStart") and os.path.join(d, "wifi_setup.py") in line
               for line in unit.splitlines())


def note_swap(old_dir, new_dir, live_dir):
    """After an update swapped new code in: if root runs from live_dir and
    the files it runs changed, a restart is needed to load them."""
    if not _root_runs_from(live_dir):
        return False
    if _digest(old_dir, ROOT_FILES) == _digest(new_dir, ROOT_FILES):
        return False
    try:
        _write(REBOOT, {"boot": boot_id(), "why": "update", "at": time.time()})
    except OSError:
        pass
    return True


def reboot_reasons():
    """Why the box should restart, in plain words ([] if it shouldn't)."""
    out = []
    if _read(REBOOT).get("boot") == boot_id() and boot_id():
        out.append("An update changed the Wi-Fi setup part, which loads at startup.")
    if os.path.exists("/run/reboot-required"):
        out.append("System updates are installed and finish on a restart.")
    return out


# ---------------------------------------------------------------- the button
def launch(look, here, unit, logfile):
    """The remote's "Check for updates": run updater.py --force as a transient
    unit (it outlives the ticker restarting under it). Returns {"ok", "error"}."""
    import subprocess
    if busy():
        return {"ok": True, "already": True}
    prog = start_marker(look, True)
    try:
        r = subprocess.run(["systemd-run", "--user", "--collect", "--unit", unit,
                            "-p", f"StandardOutput=append:{logfile}", "-p", f"StandardError=append:{logfile}",
                            "/usr/bin/python3", "-u", os.path.join(here, "updater.py"), "--force"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        prog.fail(f"Couldn't start the updater: {e}")
        return {"ok": False, "error": "Couldn't start the updater."}
    if r.returncode != 0:
        # Most likely an old run is stuck under the same name: clear it for next time.
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True)
        prog.fail("The updater didn't start (an earlier run was stuck; it's cleared now). Tap again.")
        return {"ok": False, "error": (r.stderr or "").strip()[:200]}
    return {"ok": True}
