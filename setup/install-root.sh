#!/bin/sh
# One-time root setup for a Sportsball Pi. Safe to run again.
#
#   sudo ~/ticker/setup/install-root.sh
#   install-root.sh --image     (image builder, in a chroot: no running services)
#
# Installs:
#   sportsball-update.timer  02:30 daily: apt full-upgrade (+ autoremove), log in
#                            /var/log/sportsball-update.log
#   sportsball-reboot.timer  03:30 daily: reboot
#   sportsball-wifi.service  Wi-Fi setup hotspot when offline (wifi_setup.py)
#   captive DNS for the hotspot, so phones open the setup page by themselves
#   backup DNS (local cache asking the router + 1.1.1.1 + 8.8.8.8)
#   log limits: system journal capped at 50 MB, update log at 2 MB
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
TICKER=$(dirname "$(dirname "$(readlink -f "$0")")")
IMAGE=; [ "$1" = "--image" ] && IMAGE=1

cat > /etc/systemd/system/sportsball-update.service <<UNIT
[Unit]
Description=Sportsball daily system update
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
# A Pi has no clock battery: when the clock is set from the internet it jumps ahead
# weeks, and systemd fires every timer it "missed" (mid-setup!). Up 1 h first.
ExecCondition=/bin/sh -c 'test \$\$(cut -d. -f1 /proc/uptime) -gt 3600'
Environment=DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
ExecStartPre=/bin/sh -c 'f=/var/log/sportsball-update.log; if [ -f \$\$f ] && [ \$\$(stat -c%%s \$\$f) -gt 2000000 ]; then mv \$\$f \$\$f.1; fi'
ExecStart=/bin/sh -c '{ date; dpkg --configure -a; apt-get update && apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade && apt-get -y autoremove; echo "exit \$?"; } >> /var/log/sportsball-update.log 2>&1'
TimeoutStartSec=55min
UNIT

cat > /etc/systemd/system/sportsball-update.timer <<UNIT
[Unit]
Description=Sportsball daily system update (02:30)

[Timer]
OnCalendar=*-*-* 02:30:00

[Install]
WantedBy=timers.target
UNIT

cat > /etc/systemd/system/sportsball-reboot.service <<UNIT
[Unit]
Description=Sportsball daily reboot

[Service]
# A Pi has no clock battery: when the clock is set from the internet it jumps ahead
# weeks, and systemd fires every timer it "missed" (mid-setup!). Up 1 h first.
ExecCondition=/bin/sh -c 'test \$\$(cut -d. -f1 /proc/uptime) -gt 3600'
# Never in the middle of a system update.
ExecCondition=/bin/sh -c '! pgrep -x "apt-get|dpkg" >/dev/null'
Type=oneshot
ExecStart=/usr/bin/systemctl reboot
UNIT

cat > /etc/systemd/system/sportsball-reboot.timer <<UNIT
[Unit]
Description=Sportsball daily reboot (03:30)

[Timer]
OnCalendar=*-*-* 03:30:00

[Install]
WantedBy=timers.target
UNIT

cat > /etc/systemd/system/sportsball-wifi.service <<UNIT
[Unit]
Description=Sportsball Wi-Fi setup hotspot (when offline)
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
ExecStart=/usr/bin/python3 -u $TICKER/wifi_setup.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

# While the setup hotspot is up, every DNS name points at the Pi.
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
echo "address=/#/10.42.0.1" > /etc/NetworkManager/dnsmasq-shared.d/sportsball-captive.conf

# Backup DNS: NetworkManager runs a local caching dnsmasq (127.0.0.1) that
# asks the network's own DNS (from DHCP) and Cloudflare/Google together and
# takes the first answer, so a router that drops lookups doesn't matter.
# Works on any network the Pi joins.
cat > /etc/NetworkManager/conf.d/sportsball-dns.conf <<CONF
[main]
dns=dnsmasq
CONF
mkdir -p /etc/NetworkManager/dnsmasq.d
cat > /etc/NetworkManager/dnsmasq.d/sportsball-backup.conf <<CONF
server=1.1.1.1
server=8.8.8.8
all-servers
cache-size=1000
CONF
if [ -z "$IMAGE" ]; then
  systemctl reload NetworkManager
  sleep 3
  getent hosts site.api.espn.com >/dev/null && echo "DNS ok" || echo "DNS check FAILED: rm /etc/NetworkManager/conf.d/sportsball-dns.conf && systemctl reload NetworkManager"
fi

# The system journal: capped, so years of logs can't fill the card.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/sportsball.conf <<CONF
[Journal]
SystemMaxUse=50M
RuntimeMaxUse=20M
CONF
if [ -n "$IMAGE" ]; then
  systemctl enable sportsball-update.timer sportsball-reboot.timer sportsball-wifi.service
else
  systemctl restart systemd-journald
  systemctl daemon-reload
  systemctl enable --now sportsball-update.timer sportsball-reboot.timer sportsball-wifi.service
  systemctl list-timers 'sportsball-*' --no-pager
fi
echo "done"
