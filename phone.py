#!/usr/bin/env python3
"""Cross-platform phone-access launcher for HDB Scout.

Runs app.py in the background and — if `cloudflared` is installed — opens a
free public tunnel too, so you can browse the app from your phone on the same
Wi-Fi or on mobile data. Works on Windows, macOS and Linux; on macOS/Windows
you'd normally double-click phone.command / phone.bat instead of running this
directly, but `python phone.py` works everywhere those wrappers don't apply
(e.g. Linux).

Ctrl+C stops both the app and the tunnel.
"""

import http.client
import os
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 5001


def _lan_ip():
    """Best-effort LAN IP without shelling out — works on every OS."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _wait_for_server(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1)
            conn.request("GET", "/")
            conn.getresponse()
            return True
        except OSError:
            time.sleep(0.5)
    return False


def _free_port():
    """Best-effort: kill whatever's already holding PORT from a previous run."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                  text=True, check=False).stdout
            for line in out.splitlines():
                if f":{PORT}" in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                    capture_output=True, check=False)
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{PORT}"],
                                  capture_output=True, text=True, check=False).stdout
            for pid in out.split():
                subprocess.run(["kill", "-9", pid], capture_output=True, check=False)
    except FileNotFoundError:
        pass          # lsof/netstat unavailable — fine, app.py will just fail loudly
    time.sleep(0.5)


def main():
    os.chdir(ROOT)
    _free_port()

    print("Starting the app…")
    app_proc = subprocess.Popen([sys.executable, "app.py"])

    if not _wait_for_server():
        print("The app didn't start in time — check for errors above.")
        app_proc.terminate()
        sys.exit(1)

    ip = _lan_ip()
    print()
    print("=" * 60)
    print("  HDB Scout is running.")
    print(f"  On this computer:           http://127.0.0.1:{PORT}")
    if ip:
        print(f"  On your phone (same Wi-Fi): http://{ip}:{PORT}")

    tunnel_proc = None
    cloudflared = shutil.which("cloudflared")
    if cloudflared:
        print()
        print("  Opening a public link for MOBILE DATA below.")
        print("  Look for the https://<something>.trycloudflare.com line —")
        print("  open THAT on your phone. (It changes each time you start.)")
        print("=" * 60)
        print()
        tunnel_proc = subprocess.Popen(
            [cloudflared, "tunnel", "--url", f"http://localhost:{PORT}"])
    else:
        print()
        print("  (Install 'cloudflared' to also get a link that works on")
        print("   mobile data, not just the same Wi-Fi.)")
        print("=" * 60)
        print()
        print("Press Ctrl+C to stop.")

    try:
        (tunnel_proc or app_proc).wait()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping…")
        for proc in (tunnel_proc, app_proc):
            if proc and proc.poll() is None:
                proc.terminate()
        for proc in (tunnel_proc, app_proc):
            if proc:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    main()
