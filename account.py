"""Root jobs asked for by the phone setup page: the owner's own login (made
once) and the ticker's name on the network.

The web remote runs as the ticker's locked account and anyone on the home
network can open it, so it can't make accounts itself. It drops a request
in REQ_DIR (only the ticker's account can write there); this thread, inside
the root Wi-Fi service, checks it and makes the account: admin (sudo with
its password) and SSH switched on. Only while the Pi has no login yet: once
one exists the offer is gone, and the password is changed with `passwd`.
The password is never stored or logged; the request file is deleted first.

The name (sportsball.local) can be changed any time setup runs: harmless,
and two tickers in one house each need their own.
"""

import json
import os
import pwd
import re
import subprocess
import threading
import time

REQ_DIR = "/run/sportsball/account"
REQUEST = REQ_DIR + "/request.json"
STATE = REQ_DIR + "/state.json"       # {"available": bool, "result": {...}} for the web remote
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,30}")
RESERVED = {"root", "pi", "sportsball", "daemon", "bin", "sys", "sync", "games", "man", "lp",
            "mail", "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "nobody", "systemd"}
GROUPS = "sudo,adm,users,video,render,audio,input,netdev,plugdev"
HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?")


def has_login():
    """A person can already log in: an account (uid 1000+) with a usable password
    (Imager's, or one made here). The ticker's own and the image's pi are locked."""
    people = {p.pw_name for p in pwd.getpwall() if 1000 <= p.pw_uid < 60000}
    with open("/etc/shadow") as f:            # (Python 3.13 has no spwd module)
        for line in f:
            name, hashed = (line.split(":") + [""])[:2]
            if name in people and hashed and hashed[0] not in "!*":
                return True
    return False


def _write_state(owner_uid, result=None):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"available": not has_login(), "result": result}, f)
    os.chmod(tmp, 0o644)
    os.chown(tmp, owner_uid, -1)
    os.replace(tmp, STATE)


def _create(user, password):
    if has_login():
        return "This ticker already has a login. Change its password over SSH with passwd."
    if not NAME_RE.fullmatch(user) or user in RESERVED or user.startswith("systemd"):
        return "Pick a user name of lowercase letters and numbers, starting with a letter."
    try:
        pwd.getpwnam(user)
        return "That user name is taken. Pick another."
    except KeyError:
        pass
    if len(password) < 8 or "\n" in password or "\r" in password:
        return "The password needs at least 8 characters."
    r = subprocess.run(["useradd", "-m", "-s", "/bin/bash", "-G", GROUPS, user], capture_output=True, text=True)
    if r.returncode != 0:
        return "Could not make the account: " + r.stderr.strip()[:120]
    r = subprocess.run(["chpasswd"], input=f"{user}:{password}\n", capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["userdel", "-r", user], capture_output=True)
        return "Could not set the password: " + r.stderr.strip()[:120]
    try:
        subprocess.run(["systemctl", "enable", "--now", "ssh"], capture_output=True)
    except OSError:
        pass
    print(f"[Account] made login {user}, SSH on", flush=True)
    return ""


def _rename(name):
    if not HOST_RE.fullmatch(name):
        return "Use lowercase letters, numbers and dashes for the name."
    old = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    if name != old:
        r = subprocess.run(["hostnamectl", "set-hostname", name], capture_output=True, text=True)
        if r.returncode != 0:
            return "Could not rename: " + r.stderr.strip()[:120]
        with open("/etc/hosts") as f:
            hosts = [ln for ln in f.read().splitlines() if not ln.startswith("127.0.1.1")]
        with open("/etc/hosts", "w") as f:
            f.write("\n".join(hosts + [f"127.0.1.1\t{name}"]) + "\n")
        # cloud-init would put the image's name back at the next boot.
        os.makedirs("/etc/cloud/cloud.cfg.d", exist_ok=True)
        with open("/etc/cloud/cloud.cfg.d/99-sportsball-name.cfg", "w") as f:
            f.write("preserve_hostname: true\n")
        print(f"[Name] {old} -> {name}", flush=True)
    # Also when unchanged: drops a sportsball-2 left over from a clash.
    subprocess.run(["systemctl", "restart", "avahi-daemon"], capture_output=True)
    return ""


def _watch(owner_uid):
    while True:
        time.sleep(1)
        if not os.path.exists(REQUEST):
            continue
        try:
            with open(REQUEST) as f:
                req = json.load(f)
        except (OSError, ValueError):
            req = {}
        finally:
            try:
                os.remove(REQUEST)          # the password doesn't stay on disk
            except OSError:
                pass
        if req.get("kind") == "name":
            name = str(req.get("name", "")).strip().lower()
            try:
                err = _rename(name)
            except OSError as e:
                err = f"Could not rename: {e}"
            _write_state(owner_uid, {"id": req.get("id"), "ok": not err, "error": err, "name": name})
            continue
        user, password = str(req.get("user", "")).strip(), str(req.get("password", ""))
        try:
            err = _create(user, password)
        except OSError as e:          # keep watching whatever happens
            err = f"Could not make the account: {e}"
        _write_state(owner_uid, {"id": req.get("id"), "ok": not err, "error": err, "user": user})


def start(owner):
    """owner: the ticker's account, the only one allowed to ask."""
    p = pwd.getpwnam(owner)
    os.makedirs(REQ_DIR, exist_ok=True)
    os.chown(REQ_DIR, p.pw_uid, p.pw_gid)
    os.chmod(REQ_DIR, 0o700)
    _write_state(p.pw_uid)
    threading.Thread(target=_watch, args=(p.pw_uid,), daemon=True).start()
