#!/bin/zsh
# Double-click this to run HDB Scout and expose it to your phone
# (even on mobile data, if 'cloudflared' is installed) — see phone.py.
#
# Leave the window open while you use the app. Close it (or Ctrl+C) to stop.

cd "$(dirname "$0")" || exit 1
source venv/bin/activate || { echo "venv not found — run: python3 -m venv venv && pip install -r requirements.txt"; exit 1; }

python phone.py
