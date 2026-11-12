#!/bin/sh
# Builds a Sportsball SD-card image for Raspberry Pi Imager ("Use custom").
#
#   image/build-image.sh [--out DIR]
#
# Output: DIR/sportsball-<version>.img.xz (+ .sha256). DIR defaults to
# image-out/ in the project. Needs Docker (no sudo): the image is put
# together in a throwaway privileged Debian container, from the official
# Raspberry Pi OS Lite (arm64) image, cached in image-cache/.
#
# What's in it: Raspberry Pi OS Lite + the ticker's packages, both looks as
# current signed releases (release.sh, not published): the classic 8-bit one
# switched on (it runs the first-run setup: Wi-Fi hotspot with a welcome
# screen, then a phone page for the look and the teams) and the HD one from
# HD_SRC (default ../sportsball-hd) next to it, switched off. See image/README.md.
set -e
cd "$(dirname "$0")/.."
OUT=image-out
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift 2 ;;
    *) echo "usage: $0 [--out DIR]"; exit 2 ;;
  esac
done
mkdir -p "$OUT" image-cache
OUT=$(cd "$OUT" && pwd)

# Base image: the latest Raspberry Pi OS Lite (64-bit), verified by its checksum.
URL=$(curl -sIL -o /dev/null -w '%{url_effective}' https://downloads.raspberrypi.com/raspios_lite_arm64_latest)
BASE=$(basename "$URL")
if [ ! -f "image-cache/$BASE" ]; then
  echo "downloading $BASE"
  curl -fSL -o "image-cache/$BASE.part" "$URL" && mv "image-cache/$BASE.part" "image-cache/$BASE"
fi
curl -fsSL "$URL.sha256" > "image-cache/$BASE.sha256"
(cd image-cache && sha256sum -c "$BASE.sha256")

# Both looks as signed release bundles (same thing the Pis update from).
HD_SRC=$(cd "${HD_SRC:-../sportsball-hd}" && pwd)
./release.sh
VER=$(cat dist/latest.json | sed 's/.*"version": "\([^"]*\)".*/\1/')
"$HD_SRC/release.sh"
HD_VER=$(cat "$HD_SRC/dist/latest.json" | sed 's/.*"version": "\([^"]*\)".*/\1/')

docker run --rm --privileged --network host -v /dev:/dev \
  -v "$PWD/image:/build:ro" -v "$PWD/dist:/dist:ro" -v "$HD_SRC/dist:/dist-hd:ro" -v "$PWD/image-cache:/cache:ro" -v "$OUT:/out" \
  -e BASE="$BASE" -e VER="$VER" -e HD_VER="$HD_VER" -e OWNER="$(id -u):$(id -g)" -e DEBIAN_FRONTEND=noninteractive \
  debian:trixie sh /build/build-in-container.sh

echo
echo "Image: $OUT/sportsball-$VER.img.xz"
echo "Write it with Raspberry Pi Imager: Choose OS > Use custom."
