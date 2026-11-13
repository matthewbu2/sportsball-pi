"""Read the tickers' status reports (status.py) on this PC.

    tools/fleet.sh                 one line per ticker, newest report each
    tools/fleet.sh HOST|ID         that ticker's latest report in full
    tools/fleet.sh HOST|ID --all   every report from it, oldest first

Reports are comments on issue 1 of the private repo matthewbu2/sportsball-status
(fetched with gh), sealed for ~/.config/sportsball/status-key.pem (X25519; its
public half is MAKER_KEY in status.py). Not shipped to the Pis.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import zlib

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import load_pem_private_key

REPO, ISSUE = "matthewbu2/sportsball-status", 1
KEY = os.path.expanduser("~/.config/sportsball/status-key.pem")
BLOB = re.compile(r"sportsball-status v1\s*```\s*([A-Za-z0-9+/=\s]+?)\s*```")


def unseal(priv, b64):
    raw = base64.b64decode(b64)
    epk, nonce, ct = raw[:32], raw[32:44], raw[44:]
    shared = priv.exchange(X25519PublicKey.from_public_bytes(epk))
    key = HKDF(SHA256(), 32, salt=None, info=b"sportsball-status v1").derive(shared + epk)
    return json.loads(zlib.decompress(AESGCM(key).decrypt(nonce, ct, epk)))


def reports():
    with open(KEY, "rb") as f:
        priv = load_pem_private_key(f.read(), None)
    out = subprocess.run(["gh", "api", "--paginate", f"repos/{REPO}/issues/{ISSUE}/comments?per_page=100",
                          "--jq", ".[] | {at: .created_at, body: .body}"],
                         capture_output=True, text=True, check=True).stdout
    got, junk = [], 0
    for line in out.splitlines():
        c = json.loads(line)
        m = BLOB.search(c["body"])
        try:
            r = unseal(priv, m.group(1))
        except Exception:  # noqa: BLE001 -- not ours, or tampered with
            junk += 1
            continue
        r["_posted"] = c["at"]
        got.append(r)
    if junk:
        print(f"({junk} comment(s) that aren't valid reports were skipped)")
    return got


def health(r, row):
    rows = r.get("health")
    for k, v, ok in rows if isinstance(rows, list) else []:
        if k == row:
            return v if ok else f"!{v}"
    return "-"


def ago(iso):
    t = time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    h = (time.time() - t) / 3600
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} d ago"


def summary(rs):
    latest = {}
    for r in rs:
        latest[r["id"]] = r
    print(f"{'ticker':18} {'last report':12} {'on':8} {'classic':20} {'hd':20} {'temp':>5}  frame rate / problems")
    for r in sorted(latest.values(), key=lambda r: r["host"]):
        looks = r.get("looks", {})
        flags = []
        if any(v.get("broken") for v in looks.values()):
            flags.append("BENCHED " + ",".join(k for k, v in looks.items() if v.get("broken")))
        if r.get("on") and looks.get(r["on"], {}).get("state") != "active":
            flags.append(f"{r['on']} is {looks.get(r['on'], {}).get('state')}")
        errs = sum(v.get("log", {}).get("new_errors", len(v.get("log", {}).get("errors", []))) for v in looks.values())
        if errs:
            flags.append(f"{errs} new error lines")
        if isinstance(r.get("health"), dict):
            flags.append("remote not answering")
        print(f"{r['host'][:18]:18} {ago(r['_posted']):12} {str(r.get('on')):8} "
              f"{looks.get('classic', {}).get('version', '-'):20} {looks.get('hd', {}).get('version', '-'):20} "
              f"{r.get('temp_c') or 0:5.0f}  {health(r, 'Frame rate')}  {'; '.join(flags)}")


def main():
    rs = reports()
    if not rs:
        print("no reports yet")
        return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        summary(rs)
        return
    mine = [r for r in rs if args[0] in (r["host"], r["id"])]
    for r in (mine if "--all" in sys.argv else mine[-1:]):
        print(json.dumps(r, indent=1))


if __name__ == "__main__":
    main()
