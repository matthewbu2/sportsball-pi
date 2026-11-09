# Sportsball SD-card image

## Putting it on a card

1. Install **Raspberry Pi Imager** (raspberrypi.com/software).
2. **Choose Device:** your Pi (see "Which Pis" below).
3. **Choose OS:** scroll to the bottom, **Use custom**, pick `sportsball-<version>.img.xz`.
4. **Choose Storage:** the microSD card (8 GB or bigger; everything on it is erased).
5. When Imager asks about OS customisation you can say **No**: the ticker sets itself up.
   If you do use it, Wi-Fi and the time zone set there are used, and SSH plus a login
   user are only available this way.

## First start (out of the box)

1. Put the card in the Pi, plug the Pi into the TV's HDMI, then power it on.
   The first start takes a few minutes (it grows to fill the card).
2. The TV shows **WELCOME TO SPORTSBALL!** with a QR code. Scan it with your phone's
   camera: the phone joins the ticker's own Wi-Fi, **Sportsball-Setup** (open, no
   password), and the setup page opens. If it doesn't open, browse to http://10.42.0.1.
3. Pick your home Wi-Fi and type its password. The TV says **CONNECTING**.
   (Wrong password: the welcome screen comes back with an error.)
4. Switch the phone back to your home Wi-Fi. The TV shows **SCAN TO PICK YOUR TEAMS**.
   Scan it and answer three questions: which sports (and a name for the ticker),
   your favorite teams, and sound (plus how far behind your TV picture is). That's it.
   The name is its network address (`den` -> http://den.local:8080). Every ticker in
   one house needs its own; if another already uses `sportsball`, setup suggests
   `sportsball-2`. Rename later with **Run the first-time setup again**.
5. Optional, for tinkerers: the last step offers a **login** (user name + password) for
   SSH (`ssh you@sportsball.local`). It's an admin account and turns SSH on. It can
   only be made during first setup, while the ticker has no login yet (the web remote
   has no password, so it can't stay open). Skip it and SSH stays off. Later password
   changes: `passwd` over SSH.

The time zone is set automatically from the ticker's location. Later changes: the
web remote at http://sportsball.local:8080 (the REMOTE card on the ticker has a QR code
for it), including **Run the first-time setup again**.

If the ticker loses Wi-Fi for two minutes (moved house, new router), the setup hotspot
comes back by itself.

## Which Pis

Any Pi that runs 64-bit Raspberry Pi OS. Tested on the **Raspberry Pi 3 Model B**.
The Pi 3 B+, 4, 400, 5 and 500 should work the same (the ticker scales to the TV's
resolution and finds the HDMI port the TV is on for sound), but haven't been tried.
The Pi Zero 2 W and 3 A+ start but have only 512 MB of memory, likely too little.
The Pi 1, the original Zero / Zero W and early Pi 2 boards can't run it.

## Looking after itself

- New ticker versions install at about 3:05 every night (signed releases from
  github.com/matthewbu2/sportsball-pi; a version that doesn't run steadily is rolled back).
- Raspberry Pi OS updates install at 2:30, and the Pi restarts at 3:30.
- With the TV off, the ticker waits for the screen instead of failing.

## Building a new image

```
image/build-image.sh [--out DIR]
```

Needs Docker (your user in the `docker` group) and about 6 GB of free space; no sudo.
It downloads the latest Raspberry Pi OS Lite (64-bit) into `image-cache/` (checksum
verified), builds the current code into a signed release bundle with `release.sh`
(not published), and assembles the image in a throwaway Debian container with arm64
emulation. The result is `DIR/sportsball-<version>.img.xz` (default `image-out/`) and
a `.sha256`. Takes 10-20 minutes.

What the image adds to Raspberry Pi OS Lite: the ticker's packages (pygame, numpy,
Pillow, requests, cryptography, Mesa/EGL, dnsmasq-base, v4l-utils), a `sportsball`
account with no password that runs the ticker as a user service (lingering, so it
starts at boot), the root services from `setup/install-root.sh` (nightly update and
reboot, Wi-Fi setup hotspot, backup DNS, log caps), hostname `sportsball`, Wi-Fi
country US, and no console "create a user" dialog. SSH is off unless the setup page's login step
or Imager's settings turn it on. The offline Piper voice is left out (it's 370 MB); the announcer needs the
internet anyway.
