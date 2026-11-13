#!/bin/sh
# Runs tools/fleet.py in the checker's venv (adds cryptography to it once).
PY=$HOME/.cache/sportsball-check/venv/bin/python
[ -x "$PY" ] || python3 -m venv "$HOME/.cache/sportsball-check/venv"
"$PY" -c "import cryptography" 2>/dev/null || "$PY" -m pip install -q cryptography
exec "$PY" "$(dirname "$0")/fleet.py" "$@"
