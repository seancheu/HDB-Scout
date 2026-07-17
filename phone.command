#!/bin/zsh
# Double-click this to run PropertyGuru Extractor and expose it to your phone
# (even on mobile data) via a free Cloudflare quick-tunnel.
#
# Leave the window open while you use the app. Close it (or Ctrl+C) to stop.

cd "$(dirname "$0")" || exit 1
source venv/bin/activate || { echo "venv not found — run: python3 -m venv venv && pip install -r requirements.txt"; exit 1; }

PORT=5001

# Free the port if a previous run is still holding it.
pkill -f "app.py" 2>/dev/null
lsof -ti tcp:$PORT | xargs -r kill -9 2>/dev/null
sleep 1

echo "Starting the app…"
python app.py > /tmp/pg_app.log 2>&1 &
APP_PID=$!

# Wait for the server to come up.
for i in {1..20}; do
  curl -s "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 0.5
done

LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)

echo ""
echo "============================================================"
echo "  PropertyGuru Extractor is running."
echo "  On this Mac:                http://127.0.0.1:$PORT"
[ -n "$LAN_IP" ] && echo "  On your phone (same Wi-Fi): http://$LAN_IP:$PORT"
echo ""
echo "  Opening a public link for MOBILE DATA below."
echo "  Look for the https://<something>.trycloudflare.com line —"
echo "  open THAT on your phone. (It changes each time you start.)"
echo "============================================================"
echo ""

# Stop the app when this window/script is closed.
trap "echo 'Stopping…'; kill $APP_PID 2>/dev/null; pkill -f 'app.py' 2>/dev/null; exit 0" INT TERM EXIT

# Start the tunnel in the foreground so its URL + logs stay visible.
cloudflared tunnel --url "http://localhost:$PORT"
