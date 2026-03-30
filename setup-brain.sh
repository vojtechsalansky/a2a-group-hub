#!/bin/bash
# Setup script for brAIn dashboard integration with a2a-group-hub.
# Creates a Python venv and installs the hub package in editable mode.

set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating Python venv..."
    python3 -m venv .venv
fi

echo "Activating venv and installing dependencies..."
source .venv/bin/activate
pip install -e "." --quiet

echo "Hub venv ready. Start with: npm run dev (from brAIn repo)"
