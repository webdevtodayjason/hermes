#!/usr/bin/env bash
# Install the Hermes Familiar as a native Hermes gateway plugin.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VENV_PIP="$HERMES_HOME/hermes-agent/venv/bin/pip"

echo "== Hermes Familiar plugin install =="

if [ -x "$VENV_PIP" ]; then
  "$VENV_PIP" install -q pyserial
  echo "✓ pyserial in gateway venv"
else
  echo "! gateway venv pip not found at $VENV_PIP — install pyserial into your Hermes python manually"
fi

# local install: file:// git clone of this repo, plugin/ subdir (#fragment)
hermes plugins install "file://$HERE#plugin" --force --enable

echo
echo "Done. Restart the gateway to activate:  hermes gateway restart"
echo "Then plug in the Familiar (any /dev/cu.usbmodem*) — it hot-connects."
echo "In any session, /familiar shows the link status."
