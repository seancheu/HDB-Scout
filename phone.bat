@echo off
REM Double-click this to run HDB Scout and expose it to your phone
REM (even on mobile data, if 'cloudflared' is installed) — see phone.py.
REM
REM Leave the window open while you use the app. Close it (or Ctrl+C) to stop.

cd /d "%~dp0"

if not exist venv\Scripts\activate.bat (
  echo venv not found — run: python -m venv venv  ^&^&  pip install -r requirements.txt
  pause
  exit /b 1
)

call venv\Scripts\activate.bat
python phone.py
pause
