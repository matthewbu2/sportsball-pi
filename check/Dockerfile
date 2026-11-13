# A Pi-like system for check/smoke.py: Debian trixie (what Raspberry Pi OS
# trixie is built on) with the apt packages the ticker image installs
# (image/build-in-container.sh). Built per CPU: linux/arm64 and linux/arm/v7.
FROM debian:trixie
RUN apt-get update -qq && apt-get install -qq -y --no-install-recommends \
      python3-pygame python3-numpy python3-pil python3-requests python3-cryptography \
      ca-certificates >/dev/null && apt-get clean && rm -rf /var/lib/apt/lists/*
