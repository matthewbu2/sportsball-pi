#!/bin/sh
# Builds a signed release in dist/: sportsball.tar.gz + latest.json; with
# --publish, uploads them as a GitHub release of matthewbu2/sportsball-pi,
# where every Pi's updater looks nightly (update_url, see updater.py).
# The signing key never leaves this PC: ~/.config/sportsball/release-key.pem
# (its public half is in updater.py).
set -e
cd "$(dirname "$0")"
KEY=$HOME/.config/sportsball/release-key.pem
VER=$(date +%Y.%m.%d.%H%M%S)
rm -rf dist
mkdir -p dist/stage
rsync -a --exclude __pycache__ --exclude cache --exclude 'ticker.log*' --exclude 'config.json*' \
  --exclude dist --exclude notes --exclude deploy.sh --exclude release.sh --exclude 'image*' --exclude .git --exclude .gitignore ./ dist/stage/
echo "$VER" > dist/stage/VERSION
tar czf dist/sportsball.tar.gz -C dist/stage .
SHA=$(sha256sum dist/sportsball.tar.gz | cut -d' ' -f1)
printf '%s %s' "$VER" "$SHA" > dist/msg
openssl pkeyutl -sign -inkey "$KEY" -rawin -in dist/msg -out dist/sig
printf '{"version": "%s", "file": "sportsball.tar.gz", "sha256": "%s", "sig": "%s"}\n' \
  "$VER" "$SHA" "$(base64 -w0 dist/sig)" > dist/latest.json
rm -rf dist/stage dist/msg dist/sig
echo "release $VER in dist/"
if [ "$1" = "--publish" ]; then
  gh release create "v$VER" dist/sportsball.tar.gz dist/latest.json --repo matthewbu2/sportsball-pi \
    --title "Sportsball $VER" --notes "Signed release for the Pi self-updater."
fi
