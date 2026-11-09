#!/bin/sh
# Runs inside the privileged Debian container started by build-image.sh.
# /cache/$BASE = Raspberry Pi OS Lite .img.xz, /dist = release bundle,
# /out = where the finished image goes (owned by $OWNER afterwards).
set -eu
GROW_MB=1536                      # room for the ticker's packages
IMG=/work/sportsball.img
R=/work/root

apt-get -qq update
apt-get -qq install -y xz-utils fdisk parted e2fsprogs zerofree qemu-user-static >/dev/null

# arm64 programs run through qemu. The F flag loads the emulator now, so it
# also works inside the chroot. (Kernel-wide until reboot; removed at the end.)
REGISTERED=
if [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
  [ -e /proc/sys/fs/binfmt_misc/register ] || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
  CONF=$(ls /usr/lib/binfmt.d/qemu-aarch64*.conf | head -1)
  # Keep Debian's flags (P: qemu expects argv[0] passed through); make sure F is there.
  sed '/^:/!d; s/:\([A-Z]*\)$/:\1F/; s/F\(F*\)$/F/' "$CONF" > /proc/sys/fs/binfmt_misc/register
  REGISTERED=1
fi

cleanup() {
  set +e
  for m in boot/firmware dev/pts dev proc sys; do umount "$R/$m" 2>/dev/null; done
  umount "$R" 2>/dev/null
  [ -n "$REGISTERED" ] && echo -1 > /proc/sys/fs/binfmt_misc/qemu-aarch64
}
trap cleanup EXIT

echo "== unpacking $BASE"
mkdir -p /work "$R"
xz -dc "/cache/$BASE" > "$IMG"
truncate -s +${GROW_MB}M "$IMG"
parted -s "$IMG" resizepart 2 100%
P1=$(sfdisk -d "$IMG" | awk -F'start=|,' '/img1/{print $2+0}')
P1S=$(sfdisk -d "$IMG" | awk -F'size=|,' '/img1/{print $3+0}')
P2=$(sfdisk -d "$IMG" | awk -F'start=|,' '/img2/{print $2+0}')
LOOP=$(losetup -f --show -o $((P2 * 512)) "$IMG")
e2fsck -pf "$LOOP" >/dev/null || true
resize2fs "$LOOP" >/dev/null
losetup -d "$LOOP"
mount -o loop,offset=$((P2 * 512)) "$IMG" "$R"
mount -o loop,offset=$((P1 * 512)),sizelimit=$((P1S * 512)) "$IMG" "$R/boot/firmware"
for m in proc sys dev dev/pts; do mount --bind /$m "$R/$m"; done
cp "$R/etc/resolv.conf" /work/resolv.conf.orig
cp /etc/resolv.conf "$R/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$R/usr/sbin/policy-rc.d"     # don't start services in the chroot
chmod +x "$R/usr/sbin/policy-rc.d"

echo "== ticker $VER"
mkdir -p /work/t
tar xzf /dist/sportsball.tar.gz -C /work/t
[ "$(cat /work/t/VERSION)" = "$VER" ]

echo "== packages and setup (arm64 chroot)"
cp -a /work/t "$R/tmp/ticker"
chroot "$R" /bin/bash -eu <<'CHROOT'
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update
# Ship current: otherwise a new ticker spends its first half hour (on a Pi 3)
# catching up on everything since the Raspberry Pi OS release.
apt-get -qq -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade >/dev/null
apt-get -qq install -y --no-install-recommends \
  python3-pygame python3-numpy python3-pil python3-requests python3-cryptography \
  libegl1 libgles2 libgl1 libglx-mesa0 libgl1-mesa-dri libgbm1 dnsmasq-base v4l-utils >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/*

# The ticker runs as its own account (no password, no login; SSH stays off
# unless turned on in Raspberry Pi Imager's settings).
useradd -m -s /bin/bash -G video,render,audio,input,netdev sportsball
passwd -l sportsball >/dev/null
H=/home/sportsball
mv /tmp/ticker $H/ticker
# (~/.asoundrc, the TV's HDMI sound device, is written by the ticker when it starts.)
U=$H/.config/systemd/user
mkdir -p $U/default.target.wants $U/timers.target.wants
for f in $H/ticker/setup/*.service $H/ticker/setup/*.timer; do
  case "$(basename "$f")" in sportsball-wifi*) ;; *) cp "$f" $U/ ;; esac
done
ln -s ../sportsball.service $U/default.target.wants/sportsball.service
ln -s ../sportsball-selfupdate.timer $U/timers.target.wants/sportsball-selfupdate.timer
mkdir -p /var/lib/systemd/linger && touch /var/lib/systemd/linger/sportsball   # runs without anyone logged in
chown -R sportsball:sportsball $H

# Root side: nightly OS updates and reboot, Wi-Fi setup hotspot, DNS, log caps.
sh $H/ticker/setup/install-root.sh --image

# No console "create a user" dialog: nobody has a keyboard on a ticker.
systemctl disable userconfig.service >/dev/null 2>&1 || true
echo sportsball > /etc/hostname
sed -i 's/\braspberrypi\b/sportsball/g' /etc/hosts
CHROOT

# The chroot must really have worked (a broken emulator can fail quietly).
for f in home/sportsball/ticker/main.py usr/lib/python3/dist-packages/pygame/__init__.py \
         home/sportsball/.config/systemd/user/default.target.wants/sportsball.service \
         var/lib/systemd/linger/sportsball etc/systemd/system/multi-user.target.wants/sportsball-wifi.service \
         etc/systemd/system/timers.target.wants/sportsball-update.timer; do
  [ -e "$R/$f" ] || [ -L "$R/$f" ] || { echo "BUILD FAILED: /$f missing"; exit 1; }
done
[ ! -e "$R/etc/systemd/system/multi-user.target.wants/userconfig.service" ] || { echo "BUILD FAILED: userconfig still on"; exit 1; }
chroot "$R" python3 -c "import pygame, numpy, PIL, requests, cryptography" || { echo "BUILD FAILED: python modules"; exit 1; }
# The setup page's optional login needs SSH that takes passwords.
[ -e "$R/usr/sbin/sshd" ] || { echo "BUILD FAILED: no openssh-server"; exit 1; }
! grep -rqsi '^ *PasswordAuthentication *no' "$R/etc/ssh/sshd_config" "$R/etc/ssh/sshd_config.d" || { echo "BUILD FAILED: SSH passwords off"; exit 1; }
[ -e "$R/home/sportsball/ticker/account.py" ] || { echo "BUILD FAILED: account.py missing"; exit 1; }
[ -e "$R/usr/lib/aarch64-linux-gnu/libGL.so.1" ] || { echo "BUILD FAILED: libGL missing (SDL needs desktop GL on the Pi 3)"; exit 1; }

# Hostname for cloud-init too (Imager's settings replace these files if used),
# and a Wi-Fi country so the hotspot works (Imager's settings can change it).
grep -q local-hostname "$R/boot/firmware/meta-data" || echo "local-hostname: sportsball" >> "$R/boot/firmware/meta-data"
grep -q ieee80211_regdom "$R/boot/firmware/cmdline.txt" || sed -i 's/$/ cfg80211.ieee80211_regdom=US/' "$R/boot/firmware/cmdline.txt"

echo "== cleaning up"
rm -f "$R/usr/sbin/policy-rc.d"
cp /work/resolv.conf.orig "$R/etc/resolv.conf"
rm -rf "$R/tmp/"* "$R/var/tmp/"* "$R/root/.bash_history"
find "$R/var/log" -type f -exec truncate -s 0 {} +
for m in boot/firmware dev/pts dev proc sys; do umount "$R/$m"; done
umount "$R"
LOOP=$(losetup -f --show -o $((P2 * 512)) "$IMG")
e2fsck -pf "$LOOP" >/dev/null || true
zerofree "$LOOP"                   # unused blocks to zero: smaller download
losetup -d "$LOOP"

echo "== compressing"
xz -T0 -6 -c "$IMG" > "/out/sportsball-$VER.img.xz"
(cd /out && sha256sum "sportsball-$VER.img.xz" > "sportsball-$VER.img.xz.sha256")
chown "$OWNER" "/out/sportsball-$VER.img.xz" "/out/sportsball-$VER.img.xz.sha256"
ls -la /out
