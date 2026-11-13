#!/bin/sh
# Checks to pass before a release (release.sh runs this first):
#   ./check.sh          everything, incl. smoke tests in Pi-like ARM containers
#   ./check.sh --fast   skip the ARM containers (a few seconds instead of minutes)
#   ./check.sh --sync   first copy this project's SHARED files to the other one
# See check/check.py. Same file in both projects.
exec python3 "$(dirname "$0")/check/check.py" "$@"
