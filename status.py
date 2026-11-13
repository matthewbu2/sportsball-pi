"""Daily status report to the ticker's maker (sportsball-status.timer, and
right after a crash fallback or an update rollback).

On by default; the owner can switch it off on the remote's Features tab
(setting share_status). What is sent: versions of both looks, which one is
on and which one the owner picked, crash benching (style.py), the remote's
health table (scores, frame rate, memory, temperature, disk...) with the
weather row's town left out, uptime, the Pi model, and the last error /
update / look lines from the logs. Not sent: teams, favorites, notes, Wi-Fi,
addresses.

The report is sealed for the maker (X25519 + AES-GCM: only the private key on
the maker's PC opens it) and posted as a comment on issue 1 of a private
GitHub repo with a token that can only reach that repo. The token ships in
the public releases, so it is not a secret: whoever digs it out could post
junk to that repo, never read a report.

    python3 status.py           send one report (if share_status is on)
    python3 status.py --print   show the report instead of sending it

The same file is in both projects; keep them identical.
"""

import base64
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import uuid
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.append(os.path.join(HERE, "vendor"))

import style  # noqa: E402

REPO = "matthewbu2/sportsball-status"
ISSUE = 1
MAKER_KEY = "X7vsbSmrzte/nCDRgTlnTtkqAsYQdqQ1UByw1Mzu2CY="      # X25519 public key (raw, base64)
TOKEN_FILE = os.path.join(HERE, "status-token")                 # in releases, not in git
ID_FILE = os.path.join(style.HOME, ".config", "sportsball", "id")
LOGS = {"hd": "run.log", "classic": "ticker.log"}
HIDE_ROWS = {"Weather"}                                           # value names the town
FORMAT = "sportsball-status v1"


def log(msg):
    print(f"[Status] {msg}", flush=True)


def ticker_id():
    """A random id, made once, so reports from one box line up even if it's renamed."""
    try:
        with open(ID_FILE) as f:
            return f.read().strip()
    except OSError:
        tid = uuid.uuid4().hex[:12]
        os.makedirs(os.path.dirname(ID_FILE), exist_ok=True)
        with open(ID_FILE, "w") as f:
            f.write(tid + "\n")
        return tid


def _setting(name, default):
    """The running look's setting (both looks carry the same settings)."""
    d = style.LOOKS[style.current()]["dir"]
    try:
        with open(os.path.join(d, "config.json")) as f:
            return json.load(f).get(name, default)
    except (OSError, ValueError):
        return default


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _log_lines(name, n_err=8, n_upd=4, n_look=4):
    path = os.path.join(style.LOOKS[name]["dir"], LOGS[name])
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 300_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    err = [ln for ln in lines if re.search(r"Traceback|Error|Exception|failed|error:|\b[1-9]\d* errors", ln)]
    upd = [ln for ln in lines if ln.startswith("[Update]")]
    look = [ln for ln in lines if ln.startswith("[Look]")]
    cut = lambda xs, n: [x[:240] for x in xs[-n:]]   # noqa: E731
    return {"errors": cut(err, n_err), "updates": cut(upd, n_upd), "look": cut(look, n_look),
            "last": cut(lines, 3)}


def _local_json(path):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8080{path}", timeout=8) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)[:200]}


def collect():
    sc = ["systemctl", "--user"]
    looks = {}
    for k in style.installed():
        unit = style.LOOKS[k]["unit"]
        state = subprocess.run(sc + ["is-active", unit], capture_output=True, text=True).stdout.strip()
        looks[k] = {"version": style.version(k), "enabled": style._enabled(k), "state": state,
                    "broken": style.broken(k), "log": _log_lines(k)}
    health = _local_json("/status?health")
    if isinstance(health, list):
        health = [[k, "(hidden)" if k in HIDE_ROWS else v, ok] for k, v, ok in health]
    try:
        throttled = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout.strip()
    except OSError:
        throttled = ""
    try:
        temp = int(_read("/sys/class/thermal/thermal_zone0/temp", "0")) / 1000
    except ValueError:
        temp = None
    try:
        up_h = round(float(_read("/proc/uptime", "0").split()[0]) / 3600, 1)
    except ValueError:
        up_h = None
    return {"id": ticker_id(), "host": socket.gethostname(), "sent": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "model": _read("/proc/device-tree/model").rstrip("\0"), "arch": platform.machine(),
            "os": _read("/etc/debian_version"), "uptime_h": up_h, "temp_c": temp, "throttled": throttled,
            "on": style.current(), "wanted": style.wanted(), "benched": style._read_broken(),
            "looks": looks, "health": health}


def seal(report):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    eph = X25519PrivateKey.generate()
    epk = eph.public_key().public_bytes_raw()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(MAKER_KEY)))
    key = HKDF(SHA256(), 32, salt=None, info=b"sportsball-status v1").derive(shared + epk)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, zlib.compress(json.dumps(report).encode(), 9), epk)
    return base64.b64encode(epk + nonce + ct).decode()


def send(report):
    import requests
    token = _read(TOKEN_FILE)
    if not token:
        log("no status-token in this build; not sent")
        return False
    body = f"{FORMAT}\n```\n{seal(report)}\n```"
    for attempt in range(3):
        try:
            r = requests.post(f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments",
                              headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                       "User-Agent": "sportsball-status"},
                              json={"body": body}, timeout=30)
            if r.status_code == 201:
                log(f"sent ({len(body)} bytes)")
                return True
            log(f"GitHub said {r.status_code}: {r.text[:200]}")
            if r.status_code in (401, 403, 404, 422):
                return False
        except Exception as e:  # noqa: BLE001
            log(f"send failed: {e!r}")
        time.sleep(20 * (attempt + 1))
    return False


def main():
    if "--print" in sys.argv:
        print(json.dumps(collect(), indent=1))
        return
    if not _setting("share_status", True):
        log("switched off on this ticker (share_status)")
        return
    send(collect())


if __name__ == "__main__":
    main()
