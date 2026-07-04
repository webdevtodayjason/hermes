#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="/Users/sem/.local/pipx/venvs/platformio/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi
if [[ $# -eq 0 ]]; then
  exec "$PY" scripts/hermes_serial_bridge.py --port /dev/cu.usbmodem101 --interval 1
else
  exec "$PY" scripts/hermes_serial_bridge.py "$@"
fi
