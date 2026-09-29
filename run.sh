#!/usr/bin/env bash
# ===================================================================
#  Pixel RPG launcher for Linux - and for a Chromebook's Linux
#  (Settings > About ChromeOS > Developers > Linux development
#  environment). The twin of run.bat.
#
#      bash run.sh              play
#      bash run.sh --selftest   check the install without opening a window
#
#  The first run sets up a private copy of Python's libraries in .venv
#  next to this file (a minute or two, once). After that it just starts.
#
#  Saves are NOT in this folder - they live in ~/.local/share/Pixel_RPG -
#  so replacing the folder with a newer download keeps every save.
# ===================================================================
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is not installed. Installing it (asks for nothing on a Chromebook)..."
    sudo apt-get update && sudo apt-get install -y python3 python3-venv
fi

# Debian - which is what a Chromebook's Linux is - ships python3 without
# the venv module, and says so only by failing half way through.
if ! python3 -c "import ensurepip, venv" >/dev/null 2>&1; then
    echo "Adding python3-venv (needed once)..."
    sudo apt-get update && sudo apt-get install -y python3-venv
fi

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import pygame" >/dev/null 2>&1; then
    echo "First run: installing pygame into .venv (once)..."
    rm -rf .venv
    python3 -m venv .venv
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -r requirements.txt
fi

exec .venv/bin/python main.py "$@"
