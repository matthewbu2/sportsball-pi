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
# What's in it: Raspberry Pi OS Lite + the ticker's packages, the ticker as
# the current signed release (release.sh, not published), its services, and
# the first-run setup (Wi-Fi hotspot with a welcome screen, then a phone page
# for teams). See image/README.md.
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

# The ticker as a signed release bundle (same thing the Pis update from).
./release.sh
VER=$(cat dist/latest.json | sed 's/.*"version": "\([^"]*\)".*/\1/')

docker run --rm --privileged -v /dev:/dev \
  -v "$PWD/image:/build:ro" -v "$PWD/dist:/dist:ro" -v "$PWD/image-cache:/cache:ro" -v "$OUT:/out" \
  -e BASE="$BASE" -e VER="$VER" -e OWNER="$(id -u):$(id -g)" -e DEBIAN_FRONTEND=noninteractive \
  debian:trixie sh /build/build-in-container.sh

echo
echo "Image: $OUT/sportsball-$VER.img.xz"
echo "Write it with Raspberry Pi Imager: Choose OS > Use custom."
