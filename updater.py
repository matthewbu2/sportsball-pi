"""Self-update from a signed release (user timer, nightly 03:10, before the
03:30 reboot; or "Check for updates" in the web remote).

  1. GET {update_url}/latest.json: {"version", "file", "sha256", "sig"}
  2. if newer than ./VERSION: download the tarball, check its sha256 and the
     Ed25519 signature of "version sha256" against PUBLIC_KEY (the private
     key stays on the build PC; see release.sh)
  3. unpack to ~/ticker.new, move config.json, the logo cache and the logs
     across, swap: ~/ticker -> ~/ticker.prev, ~/ticker.new -> ~/ticker
  4. restart the ticker; if it isn't running steadily 90 s later, swap
     back to ~/ticker.prev and restart that

    python3 updater.py           check and update
    python3 updater.py --force   update even if auto-update is off
"""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logstamp  # noqa: E402
logstamp.install()

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
LIVE, NEW, PREV = (os.path.join(HOME, d) for d in ("ticker", "ticker.new", "ticker.prev"))
STATE = ["config.json", "cache", "notes", "ticker.log", "ticker.log.1"]   # kept across updates
HEALTHY_AFTER = 90

PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEARJOqwd7MFSwd3XvUZ/Ig/jgnj7YqWIkkM17ZfSQTaeQ=
-----END PUBLIC KEY-----
"""


def log(msg):
    print(f"[Update] {msg}", flush=True)


def current_version(d=HERE):
    try:
        with open(os.path.join(d, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "0"


def _verify(meta, blob):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    sha = hashlib.sha256(blob).hexdigest()
    if sha != meta["sha256"]:
        raise ValueError("download doesn't match its checksum")
    key = load_pem_public_key(PUBLIC_KEY)
    key.verify(base64.b64decode(meta["sig"]), f"{meta['version']} {sha}".encode())   # raises if forged


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True).stdout


def _install_units(d):
    units = os.path.join(HOME, ".config/systemd/user")
    os.makedirs(units, exist_ok=True)
    for name in os.listdir(os.path.join(d, "setup")):
        if name.endswith((".service", ".timer")) and not name.startswith("sportsball-wifi"):
            shutil.copy(os.path.join(d, "setup", name), units)
    _systemctl("daemon-reload")


def _move_state(src, dst):
    for name in STATE:
        s = os.path.join(src, name)
        if os.path.exists(s):
            d = os.path.join(dst, name)
            if os.path.isdir(d):
                shutil.rmtree(d)
            os.replace(s, d)


def _steady():
    """The ticker has been up, without restarting, for most of the wait."""
    out = _systemctl("show", "sportsball", "-p", "ActiveState", "-p", "ActiveEnterTimestampMonotonic")
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if props.get("ActiveState") != "active":
        return False
    up_since = int(props.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
    return time.monotonic() - up_since > HEALTHY_AFTER - 30


def _swap_in(new_dir, start=True):
    if os.path.exists(PREV):
        shutil.rmtree(PREV)
    _move_state(LIVE, new_dir)
    os.replace(LIVE, PREV)
    os.replace(new_dir, LIVE)
    _install_units(LIVE)
    if start:
        _systemctl("restart", "sportsball")


def _roll_back():
    log("new version isn't running steadily; going back")
    bad = os.path.join(HOME, "ticker.bad")
    if os.path.exists(bad):
        shutil.rmtree(bad)
    _move_state(LIVE, PREV)
    os.replace(LIVE, bad)
    os.replace(PREV, LIVE)
    _install_units(LIVE)
    # Only if this look is still the one on: if it crash-looped meanwhile, the
    # fallback (style.py) has already put the other look on the TV.
    if _systemctl("is-enabled", "sportsball").strip() == "enabled":
        _systemctl("reset-failed", "sportsball")
        _systemctl("restart", "sportsball")
    _systemctl("start", "--no-block", "sportsball-status.service")    # tell the maker


HD_URL = "https://github.com/matthewbu2/sportsball-hd-releases/releases/latest/download"
HD_DIR = os.path.join(HOME, "hd")
HD_UNITS = ("sportsball-hd.service", "sportsball-hd-update.service", "sportsball-hd-update.timer")


def ensure_hd():
    """Put the HD look next to this one (switched off), so the remote's Look
    picker offers both. Signed like our own releases (same key) and must say
    PRODUCT hd. Done once: after that HD updates itself (its own timer)."""
    if os.path.exists(os.path.join(HD_DIR, "main.py")):
        return
    meta = requests.get(HD_URL + "/latest.json", timeout=20).json()
    blob = requests.get(HD_URL + "/" + meta["file"], timeout=180).content
    _verify(meta, blob)
    tmp = HD_DIR + ".new"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(tmp, filter="data")
    try:
        with open(os.path.join(tmp, "PRODUCT")) as f:
            if f.read().strip() != "hd":
                raise ValueError("not an HD release")
    except OSError:
        raise ValueError("not an HD release") from None
    if current_version(tmp) != meta["version"]:
        raise ValueError("HD release VERSION doesn't match latest.json")
    # carry this ticker's settings over (teams, favorites, sound...)
    mine = os.path.join(LIVE, "config.json")
    if os.path.exists(mine):
        with open(mine) as f:
            cfg = json.load(f)
        cfg.pop("update_url", None)
        with open(os.path.join(tmp, "config.json"), "w") as f:
            json.dump(cfg, f, indent=1)
    os.replace(tmp, HD_DIR)
    units = os.path.join(HOME, ".config/systemd/user")
    os.makedirs(units, exist_ok=True)
    for name in HD_UNITS:
        src = os.path.join(HD_DIR, "setup", name)
        if os.path.exists(src):
            shutil.copy(src, units)
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", "sportsball-hd-update.timer")      # HD keeps itself up to date
    log(f"installed the HD look {meta['version']} (switched off; pick it in the remote's Look section)")


def run(force=False):
    sys.path.insert(0, HERE)
    import settings
    if settings.get("auto_update") or force:
        try:
            ensure_hd()
        except Exception as e:  # noqa: BLE001 -- never let this block our own update
            log(f"HD look not installed: {e!r}")
    url = (settings.get("update_url") or "").rstrip("/")
    if not url:
        log("no update_url set")
        return
    if not settings.get("auto_update") and not force:
        log("auto-update is off")
        return
    meta = requests.get(url + "/latest.json", timeout=20).json()
    have = current_version()
    if meta["version"] <= have:
        log(f"up to date ({have})")
        return
    log(f"{have} -> {meta['version']}")
    blob = requests.get(url + "/" + meta["file"], timeout=120).content
    _verify(meta, blob)
    if os.path.exists(NEW):
        shutil.rmtree(NEW)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(NEW, filter="data")
    if current_version(NEW) != meta["version"]:
        raise ValueError("release VERSION doesn't match latest.json")
    on = _systemctl("is-enabled", "sportsball").strip() == "enabled"
    _swap_in(NEW, start=on)
    if not on:                     # the HD look is on (style.py): update quietly, don't start
        log(f"installed {meta['version']} (not running: the other look is on)")
        return
    time.sleep(HEALTHY_AFTER)
    if _steady():
        log(f"now running {meta['version']}")
    else:
        _roll_back()


if __name__ == "__main__":
    try:
        run(force="--force" in sys.argv)
    except Exception as e:  # noqa: BLE001
        log(f"failed: {e!r}")
        sys.exit(1)
