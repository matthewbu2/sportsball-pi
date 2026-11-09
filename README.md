# sportsball-pi

A Glance-style 8-bit LED sports ticker for a Raspberry Pi on an HDMI TV:
live scores for the NFL, college football, MLB, NBA, NHL and college
basketball, retro Tecmo-style scoring animations, an announcer voice, a web
remote on your phone (`http://sportsball.local:8080`), and a phone setup
wizard for new tickers.

- `main.py` runs the ticker (pygame on KMSDRM, straight to HDMI)
- `remote.py` + `web/remote.html` is the web remote
- `setup/install-root.sh` adds nightly OS updates, the Wi-Fi setup hotspot and backup DNS
- `image/` builds a ready-to-flash SD card image
- `updater.py` installs signed releases from this repo's Releases nightly

On a Pi that also has Sportsball HD installed, the remote's Look picker
switches between the two (`style.py`).

Releases are signed; the updater only installs a package whose Ed25519
signature matches the public key in `updater.py`.
