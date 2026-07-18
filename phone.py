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
import re
import shutil
import socket
import subprocess
import sys
import threading
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


def _open_in_browser(url):
    """Open the public link, preferring Google Chrome when installed."""
    try:
        if sys.platform == "darwin":
            if subprocess.run(["open", "-a", "Google Chrome", url],
                              capture_output=True).returncode == 0:
                return
        elif sys.platform == "win32":
            if subprocess.run(["cmd", "/c", "start", "", "chrome", url],
                              capture_output=True).returncode == 0:
                return
        else:
            for exe in ("google-chrome", "google-chrome-stable", "chromium",
                        "chromium-browser"):
                if shutil.which(exe):
                    subprocess.Popen([exe, url])
                    return
    except OSError:
        pass
    import webbrowser                  # Chrome not found — default browser
    webbrowser.open(url)


def _fetch(url, headers=None, timeout=6):
    """(status, body) for a GET. Prefers `requests` (bundles its own CA
    certs — python.org installs on macOS often ship without any, which
    makes every stdlib HTTPS call fail); falls back to urllib without
    verification, acceptable for a reachability probe."""
    try:
        import requests
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        return r.status_code, r.text
    except ImportError:
        import ssl
        import urllib.request
        req = urllib.request.Request(url, headers=headers or {})
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "replace")


def _wait_until_live(url, timeout=45):
    """Block until the tunnel URL actually serves the app from THIS machine.
    cloudflared prints the URL before its DNS propagates and the tunnel
    connects, so opening it immediately lands on a dead page."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _fetch(url)[0] == 200:
                return True
        except Exception:
            pass          # DNS not propagated / edge not connected yet
        time.sleep(2)
    return False


def _doh_resolves(host):
    """Does the hostname exist globally (checked over DNS-over-HTTPS)?
    Distinguishes 'tunnel not up yet' from 'my router/ISP DNS is broken'."""
    import json
    try:
        status, body = _fetch(
            f"https://cloudflare-dns.com/dns-query?name={host}&type=A",
            headers={"Accept": "application/dns-json"}, timeout=8)
        return status == 200 and bool(json.loads(body).get("Answer"))
    except Exception:
        return False


def _watch_tunnel(proc):
    """Echo cloudflared's output; when the public URL appears, wait for it
    to go live, then open it (each start gets a fresh trycloudflare URL)."""
    opened = False
    for line in proc.stdout:
        print(line, end="", flush=True)
        if not opened:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                opened = True
                url = m.group(0)
                print(f"\n>>> Public link for your phone: {url}", flush=True)
                print(">>> Checking the link is live (~15–30 s)…", flush=True)
                def globally_up(host, timeout=60):
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        if _doh_resolves(host):
                            return True
                        time.sleep(3)
                    return False

                if _wait_until_live(url):
                    print(">>> Link is live — opening it in Google Chrome…\n",
                          flush=True)
                    _open_in_browser(url)
                elif globally_up(url.split("//")[1]) and _wait_until_live(url, timeout=10):
                    # Local DNS was just slow — it caught up.
                    print(">>> Link is live — opening it in Google Chrome…\n",
                          flush=True)
                    _open_in_browser(url)
                elif _doh_resolves(url.split("//")[1]):
                    # The tunnel IS up worldwide — only this network's DNS
                    # can't see fresh trycloudflare names.
                    print(">>> The link is LIVE, but your router/ISP DNS "
                          "can't resolve new trycloudflare.com names,",
                          flush=True)
                    print(">>> so it won't open on this computer. Your phone "
                          "on MOBILE DATA will open it fine.", flush=True)
                    print(">>> To open it on this computer too: Chrome → "
                          "Settings → Privacy and security → Security →",
                          flush=True)
                    print(">>> turn on 'Use secure DNS' (Cloudflare 1.1.1.1), "
                          "or set your Mac's DNS to 1.1.1.1.", flush=True)
                    print(">>> Opening the app locally in Chrome instead…\n",
                          flush=True)
                    _open_in_browser(f"http://127.0.0.1:{PORT}")
                else:
                    print(">>> The tunnel didn't come up — opening the app "
                          "locally in Chrome instead.", flush=True)
                    print(">>> (Your phone can still use the same-Wi-Fi "
                          "address printed above.)\n", flush=True)
                    _open_in_browser(f"http://127.0.0.1:{PORT}")


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
        print("  Opening a public link for MOBILE DATA — it will pop up in")
        print("  Google Chrome automatically. Send that link to your phone.")
        print("  (It changes each time you start.)")
        print("=" * 60)
        print()
        # cloudflared logs (incl. the URL) go to stderr — merge and watch.
        tunnel_proc = subprocess.Popen(
            [cloudflared, "tunnel", "--url", f"http://localhost:{PORT}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        threading.Thread(target=_watch_tunnel, args=(tunnel_proc,),
                         daemon=True).start()
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
