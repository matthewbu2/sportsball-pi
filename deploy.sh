#!/bin/sh
# Copies the ticker to the Pi and restarts it. The Pi's config.json (edited in
# the web remote), logs and logo cache stay as they are. VERSION is stamped
# with the time, so any release made after this deploy counts as newer.
set -e
cd "$(dirname "$0")"
HOST=${1:-sportsball}          # ./deploy.sh jeter  (any ssh host alias)
rsync -a --delete --exclude __pycache__ --exclude cache --exclude 'ticker.log*' --exclude config.json \
  --exclude 'config.json.tmp' --exclude deploy.sh --exclude release.sh --exclude dist --exclude VERSION --exclude notes --exclude 'image*' --exclude .git --exclude .gitignore --exclude check --exclude check.sh --exclude SHARED --exclude tools \
  ./ "$HOST":ticker/
ssh "$HOST" "mkdir -p ~/.config/systemd/user && echo $(date +%Y.%m.%d.%H%M%S) > ~/ticker/VERSION &&
  install -m644 ~/ticker/setup/*.service ~/ticker/setup/*.timer ~/.config/systemd/user/ &&
  systemctl --user daemon-reload && systemctl --user enable -q sportsball-selfupdate.timer &&
  systemctl --user start sportsball-selfupdate.timer &&
  if systemctl --user is-enabled -q sportsball-hd 2>/dev/null; then echo 'updated; not started (the HD look is on)';
  else systemctl --user enable -q sportsball && systemctl --user restart sportsball && echo restarted; fi"
