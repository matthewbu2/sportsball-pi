"""Wi-Fi setup portal (runs as root: sportsball-wifi.service).

If wlan0 has had no connection for OFFLINE_S, the Pi scans for networks,
then becomes an open hotspot "Sportsball-Setup" (no password). Phones
that join are sent to the setup page (all DNS answers point at the Pi, so
the phone's captive-portal popup opens it; otherwise browse to
http://10.42.0.1). Picking a network and entering its password connects the
Pi and brings the ticker back. A wrong password reopens the hotspot with
an error.

The ticker shows the instructions on the TV while this runs: the state
goes to STATUS (JSON) for main.py to read.

A brand-new ticker (no saved Wi-Fi network, no cable) opens the hotspot
after FIRST_WAIT_S instead, and the TV says welcome. Once online, a ticker
still on the image's default time zone sets it from its location (once).
"""

import html
import json
import os
import pwd
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

IFACE = "wlan0"
AP_CON = "sportsball-setup"
AP_SSID = "Sportsball-Setup"
AP_URL = "http://10.42.0.1"
OFFLINE_S = 120
FIRST_WAIT_S = 25           # no network saved yet: open the hotspot almost right away
STATUS = "/run/sportsball/wifi.json"
DEFAULT_TZ = {"", "UTC", "Etc/UTC", "Europe/London"}    # what a fresh Raspberry Pi OS image says
HERE = os.path.dirname(os.path.abspath(__file__))

_networks = []
_error = ""
_first = False
_done = threading.Event()


def nmcli(*args, timeout=40):
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[WiFi] nmcli {' '.join(args[:3])} timed out", flush=True)
        return 1, "", "timed out"
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def connected():
    """wlan0 (or eth0) up on a network other than our own hotspot."""
    _, out, _ = nmcli("-t", "-f", "DEVICE,STATE,CONNECTION", "dev")
    for line in out.splitlines():
        dev, state, con = (line.split(":") + ["", "", ""])[:3]
        if dev in (IFACE, "eth0") and state == "connected" and con != AP_CON:
            return True
    return False


def saved_wifi():
    """Any Wi-Fi network saved (from the setup page, or Raspberry Pi Imager's settings)."""
    _, out, _ = nmcli("-t", "-f", "TYPE,NAME", "con", "show")
    return any(line.startswith("802-11-wireless:") and line.split(":", 1)[1] != AP_CON
               for line in out.splitlines())


def auto_timezone():
    """Sets the time zone from the public IP's location if it's still the image default.
    True when nothing more needs doing."""
    tz = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"],
                        capture_output=True, text=True).stdout.strip()
    if tz not in DEFAULT_TZ:
        return True
    try:
        from urllib.request import urlopen
        with urlopen("http://ip-api.com/json/?fields=status,timezone", timeout=10) as r:
            d = json.load(r)
    except (OSError, ValueError) as e:
        print(f"[WiFi] time zone lookup failed: {e}", flush=True)
        return False
    new = d.get("timezone") if d.get("status") == "success" else ""
    if not new or new == tz:
        return bool(new)
    if subprocess.run(["timedatectl", "set-timezone", new]).returncode != 0:
        return False
    print(f"[WiFi] time zone set to {new}", flush=True)
    # The ticker reads the zone when it starts: restart it so the clock is right.
    user = pwd.getpwuid(os.stat(HERE).st_uid).pw_name
    subprocess.run(["systemctl", "--user", "-M", f"{user}@", "restart", "sportsball.service"],
                   capture_output=True)
    return True


_mdns_tries = 0


def fix_mdns_name():
    """avahi renames itself (sportsball-2.local) after a moment's name clash, e.g.
    hearing its own announcement when the hotspot hands over to the house Wi-Fi,
    and never takes the name back. Restart it so sportsball.local works again.
    A few tries per boot only: a second ticker really using the name (setup's
    name box fixes that) would otherwise mean a restart every 10 minutes."""
    global _mdns_tries
    r = subprocess.run(["busctl", "call", "org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server",
                        "GetHostName"], capture_output=True, text=True)
    name = r.stdout.split('"')[1] if r.returncode == 0 and '"' in r.stdout else ""
    if name and name != socket.gethostname() and _mdns_tries < 3:
        _mdns_tries += 1
        print(f"[WiFi] mDNS name was {name}.local, restarting avahi", flush=True)
        subprocess.run(["systemctl", "restart", "avahi-daemon"], capture_output=True)


def write_status(mode, **kw):
    os.makedirs(os.path.dirname(STATUS), exist_ok=True)
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"mode": mode, "ssid": AP_SSID, "url": AP_URL, **kw}, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS)


def scan():
    global _networks
    nmcli("dev", "wifi", "rescan", "ifname", IFACE)
    time.sleep(4)
    _, out, _ = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "ifname", IFACE)
    seen = {}
    for line in out.splitlines():
        parts = line.replace("\\:", "\x00").split(":")
        if len(parts) < 3 or not parts[0]:
            continue
        ssid = parts[0].replace("\x00", ":")
        sig = int(parts[1] or 0)
        if ssid != AP_SSID and sig > seen.get(ssid, (-1, ""))[0]:
            seen[ssid] = (sig, parts[2])
    _networks = sorted(((s, sig, sec) for s, (sig, sec) in seen.items()), key=lambda n: -n[1])


def hotspot_up():
    # The welcome card goes up first: starting the hotspot can take a while.
    write_status("setup", error=_error, first=_first)
    nmcli("con", "delete", AP_CON)
    # Spelled out instead of `nmcli dev wifi hotspot`: the Pi 3's BCM43430 can't start
    # NetworkManager's default access point, and with WPA it then rejects the right
    # password. So open (like a Chromecast's setup network; it only lives until the
    # ticker joins the house Wi-Fi, and only serves the setup page), 2.4 GHz, ch 6.
    nmcli("con", "add", "type", "wifi", "ifname", IFACE, "con-name", AP_CON, "autoconnect", "no",
          "ssid", AP_SSID, "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
          "802-11-wireless.channel", "6", "ipv4.method", "shared", "ipv6.method", "ignore")
    code, _, err = nmcli("con", "up", AP_CON, timeout=100)
    print(f"[WiFi] hotspot {AP_SSID} " + ("up" if code == 0 else f"failed: {err}"), flush=True)


def hotspot_down():
    nmcli("con", "down", AP_CON)
    nmcli("con", "delete", AP_CON)


def try_join(ssid, password):
    """Drops the hotspot and joins ssid. True on success."""
    global _error
    write_status("joining", network=ssid)
    hotspot_down()
    time.sleep(2)
    args = ["dev", "wifi", "connect", ssid, "ifname", IFACE]
    if password:
        args += ["password", password]
    code, out, err = nmcli(*args, timeout=60)
    if code == 0 and connected():
        print(f"[WiFi] joined {ssid}", flush=True)
        _error = ""
        return True
    _error = f"Couldn't join {ssid}: {err or out or 'no answer'}"
    print(f"[WiFi] {_error}", flush=True)
    nmcli("con", "delete", ssid)   # don't keep a profile with a bad password
    return False


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sportsball Wi-Fi</title>
<style>body{background:#0b0d12;color:#e8e8e8;font:17px system-ui,sans-serif;max-width:480px;margin:auto;padding:20px}
h1{color:#ffd400;font-size:22px}select,input,button{width:100%;font-size:17px;padding:12px;margin:8px 0;border-radius:10px;
border:1px solid #2c3448;background:#1b2030;color:#fff;box-sizing:border-box}button{background:#ffd400;color:#000;font-weight:600}
.err{color:#ff6b6b}.dim{color:#8a93a6;font-size:14px}</style></head><body>
<h1>SPORTSBALL Wi-Fi</h1><p class=dim>Pick the network this ticker should use.</p>@ERR@
<form method=post action="/join"><select name=ssid>@OPTS@<option value="">Other (type below)</option></select>
<input name=other placeholder="Network name (if not listed)"><input name=password type=password placeholder="Password">
<button>Connect</button></form><p class=dim>The hotspot closes while the ticker connects. If the password is
wrong, Sportsball-Setup comes back in a minute with an error.</p></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Every URL (phones probe /generate_204, /hotspot-detect.html...) gets
        # the setup page, which makes the captive-portal popup appear.
        opts = "".join(f'<option value="{html.escape(s)}">{html.escape(s)} ({sig}%)</option>'
                       for s, sig, _sec in _networks)
        err = f'<p class=err>{html.escape(_error)}</p>' if _error else ""
        self._send(200, _PAGE.replace("@OPTS@", opts).replace("@ERR@", err).encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
        ssid = (form.get("other", [""])[0] or form.get("ssid", [""])[0]).strip()
        password = form.get("password", [""])[0]
        if not ssid:
            return self.do_GET()
        self._send(200, f"<meta name=viewport content='width=device-width'><body style='background:#0b0d12;"
                        f"color:#eee;font:18px system-ui;padding:20px'>Connecting to {html.escape(ssid)}..."
                        f" Watch the TV. When the ticker is up, switch your phone back to your own Wi-Fi"
                        f" and scan the code on the TV to pick your teams.</body>".encode())
        threading.Thread(target=self._join, args=(ssid, password), daemon=True).start()

    @staticmethod
    def _join(ssid, password):
        time.sleep(1)
        if try_join(ssid, password):
            _done.set()
        else:
            hotspot_up()

    def log_message(self, *args):
        pass


def portal():
    """Hotspot + setup page until a network is joined."""
    _done.clear()
    scan()
    hotspot_up()
    srv = ThreadingHTTPServer(("", 80), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _done.wait()
    srv.shutdown()
    srv.server_close()


def main():
    global _first
    offline_since = None
    tz_done, tz_at, mdns_at = False, 0.0, 0.0
    write_status("ok")
    # Raspberry Pi OS keeps Wi-Fi switched off until a Wi-Fi country is set (Imager
    # or raspi-config does that); a blank image has none, so turn the radio on here.
    try:
        subprocess.run(["rfkill", "unblock", "wifi"], capture_output=True)
    except OSError:                  # not installed (32-bit Pi OS Lite lacks it)
        print("[WiFi] rfkill not found", flush=True)
    nmcli("radio", "wifi", "on")
    import account
    account.start(pwd.getpwuid(os.stat(HERE).st_uid).pw_name)
    while True:
        if connected():
            offline_since = None
            write_status("ok")
            if not tz_done and time.monotonic() - tz_at > 600:
                tz_at = time.monotonic()
                tz_done = auto_timezone()
            if time.monotonic() - mdns_at > 600:
                mdns_at = time.monotonic()
                fix_mdns_name()
        else:
            offline_since = offline_since or time.monotonic()
            _first = not saved_wifi()
            if time.monotonic() - offline_since >= (FIRST_WAIT_S if _first else OFFLINE_S):
                print("[WiFi] offline, starting setup portal" + (" (first setup)" if _first else ""), flush=True)
                portal()
                offline_since = None
                write_status("ok")
        time.sleep(5 if offline_since else 10)


if __name__ == "__main__":
    main()
