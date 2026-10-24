#!/bin/bash
# macOS/Linux counterpart of run_portal.ps1. Offline: installs from vendor/wheels-macos (Apple Silicon, Python 3.14).
#   ./run_portal.sh [--port 8420] [--host 0.0.0.0] [--no-browser]
#   --host 0.0.0.0 (or PORTAL_HOST) makes the portal reachable from other machines on the LAN; default is this Mac only.
set -euo pipefail
cd "$(dirname "$0")"
PORT=8420; HOST="${PORTAL_HOST:-127.0.0.1}"; OPEN=--open; SETUP=1
while [ $# -gt 0 ]; do case "$1" in --port) PORT="$2"; shift;; --host) HOST="$2"; shift;; --no-browser) OPEN="";; --skip-setup) SETUP=0;; esac; shift; done
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment from the bundled wheels (no internet needed)..."
  python3.14 -m venv .venv
  .venv/bin/python -m pip install --quiet --no-index --find-links vendor/wheels-macos -r requirements.txt
fi
[ "$SETUP" = 1 ] && .venv/bin/python db/setup.py
exec .venv/bin/python serve.py --host "$HOST" --port "$PORT" $OPEN
