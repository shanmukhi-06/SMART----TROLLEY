#!/usr/bin/env python3
"""Smart Trolley device agent — runs on the Raspberry Pi 5.

This is the ONLY thing that runs on the trolley once the web app lives in the
cloud. It is deliberately thin: no database, no business logic, no UI.

    HX711 load cell  ──┐
                       ├──►  pi_agent.py  ──WebSocket──►  cloud app (AWS)
    camera scanner   ──┘                  ◄──commands──

What it does
------------
1. Reads the 4 x 50 kg load-cell bridge through the HX711 and streams **raw
   counts** to the cloud a few times a second. Raw, not grams: the cloud owns
   the calibration, so staff can re-calibrate from the settings menu without
   touching the Pi.
2. Decodes barcodes/QR codes from the camera and forwards them.
3. Obeys commands sent back: `tare`, `reset`, `ping`.
4. Optionally launches Chromium in kiosk fullscreen pointing at the cloud URL.

The USB barcode gun is NOT handled here — it behaves as a keyboard, so its
keystrokes go straight into the kiosk browser and up to the cloud with the page.

Usage
-----
    python pi_agent.py                     # stream + serve commands
    python pi_agent.py --kiosk             # also launch the browser fullscreen
    python pi_agent.py --once              # print one reading and exit
    python pi_agent.py --no-camera         # load cell only
    python pi_agent.py --http              # use HTTP polling instead of WebSocket

Configure CLOUD_URL and DEVICE_TOKEN in .env (they must match the cloud side).
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

AGENT_VERSION = "1.1"

# Reading the settings gives us pin numbers / URLs / token from the same .env
from app.config import settings  # noqa: E402

_stop = threading.Event()


# ---------------------------------------------------------------------------
# Load cell
# ---------------------------------------------------------------------------
class LoadCell:
    """Median-filtered HX711 reader with a clear failure story."""

    def __init__(self, dt: int, sck: int, gain: int, samples: int = 7) -> None:
        from app.hardware.scale import HX711Scale

        self._scale = HX711Scale(dt_pin=dt, sck_pin=sck, gain=gain)
        self._samples = samples
        self.present = True

    def read(self) -> float:
        vals = []
        for _ in range(max(7, self._samples)):
            r = self._scale.read_raw()
            if r is not None and abs(r) < 8388000:
                vals.append(r)
            time.sleep(0.003)
        if not vals:
            return 0.0
        vals.sort()
        trim = len(vals) // 5
        trimmed = vals[trim:-trim] if trim > 0 and len(vals) > 2 * trim else vals
        return statistics.median(trimmed)

    def tare_hint(self) -> None:
        """The cloud holds the real zero; this just clears a stuck conversion."""
        try:
            self._scale.reset()
        except Exception:
            pass

    def reset(self) -> None:
        self._scale.reset()

    def close(self) -> None:
        self._scale.close()


def open_load_cell(args) -> tuple[LoadCell | None, str]:
    try:
        cell = LoadCell(args.dt, args.sck, args.gain)
        return cell, ""
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Camera scanner
# ---------------------------------------------------------------------------
def start_camera(scan_queue: queue.Queue, index: int):
    from app.hardware.scanner import CameraScanner

    scanner = CameraScanner(on_scan=lambda code: scan_queue.put(code),
                            camera_index=index)
    scanner.start(force=True)   # the agent is always on real hardware
    return scanner


# ---------------------------------------------------------------------------
# Transport: WebSocket (preferred) and HTTP (fallback)
# ---------------------------------------------------------------------------
def ws_url(base: str, token: str, device_id: str) -> str:
    url = base.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return (f"{url}/ws/device?token={token}&device_id={device_id}"
            f"&v={AGENT_VERSION}")


def run_websocket(cell, scan_queue, args, hello: dict) -> None:
    """Stream readings over a WebSocket, reconnecting forever."""
    try:
        from websockets.sync.client import connect
    except Exception as exc:
        print(f"[agent] websockets library unavailable ({exc}); "
              "falling back to HTTP. pip install websockets")
        run_http(cell, scan_queue, args, hello)
        return

    url = ws_url(args.cloud, args.token, args.device_id)
    backoff = 1.0

    while not _stop.is_set():
        try:
            print(f"[agent] connecting to {args.cloud} …")
            with connect(url, open_timeout=10, close_timeout=5) as ws:
                print("[agent] connected")
                backoff = 1.0
                ws.send(json.dumps({"type": "hello", **hello}))

                last_send = 0.0
                while not _stop.is_set():
                    # 1. push a reading on schedule
                    now = time.time()
                    if now - last_send >= args.interval:
                        last_send = now
                        _send_reading(ws, cell)

                    # 2. push any barcodes the camera decoded
                    while True:
                        try:
                            code = scan_queue.get_nowait()
                        except queue.Empty:
                            break
                        ws.send(json.dumps({"type": "scan", "barcode": code}))
                        print(f"[agent] scan -> {code}")

                    # 3. drain commands from the cloud
                    try:
                        raw = ws.recv(timeout=0.05)
                        handle_command(cell, raw)
                    except TimeoutError:
                        pass
        except KeyboardInterrupt:
            break
        except Exception as exc:
            if _stop.is_set():
                break
            print(f"[agent] connection lost ({exc}); retrying in {backoff:.0f}s")
            _stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)


def _send_reading(ws, cell) -> None:
    if cell is None:
        ws.send(json.dumps({"type": "error",
                            "message": "load cell unavailable on the Pi"}))
        return
    try:
        raw = cell.read()
        if abs(raw) >= 8388600:
            ws.send(json.dumps({
                "type": "error",
                "message": f"Load-cell bridge open/saturated ({raw:,.0f} counts). Check E+/E-/A+/A- wiring."
            }))
            return
    except Exception as exc:
        ws.send(json.dumps({"type": "error", "message": f"HX711 read failed: {exc}"}))
        return
    ws.send(json.dumps({"type": "reading", "raw": raw}))


def handle_command(cell, raw: str) -> None:
    try:
        cmd = json.loads(raw)
    except json.JSONDecodeError:
        return
    kind = cmd.get("type")
    print(f"[agent] command: {kind}")
    if kind == "tare" and cell is not None:
        cell.tare_hint()
    elif kind == "reset" and cell is not None:
        try:
            cell.reset()
        except Exception as exc:
            print(f"[agent] reset failed: {exc}")


def run_http(cell, scan_queue, args, hello: dict) -> None:
    """Fallback transport: POST readings, get commands in the response."""
    url = args.cloud.rstrip("/") + "/api/device/report"
    first = True

    while not _stop.is_set():
        payload: dict = {}
        if first:
            payload.update(hello)
            payload["hello"] = True
            first = False

        if cell is not None:
            try:
                raw = cell.read()
                if abs(raw) >= 8388600:
                    payload["error"] = f"Load-cell bridge open/saturated ({raw:,.0f} counts). Check E+/E-/A+/A- wiring."
                else:
                    payload["raw"] = raw
            except Exception as exc:
                payload["error"] = f"HX711 read failed: {exc}"
        else:
            payload["error"] = "load cell unavailable on the Pi"

        scans = []
        while True:
            try:
                scans.append(scan_queue.get_nowait())
            except queue.Empty:
                break
        if scans:
            payload["scans"] = scans

        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {args.token}",
                         "X-Device-Id": args.device_id},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
            for cmd in data.get("commands", []):
                handle_command(cell, json.dumps(cmd))
        except urllib.error.HTTPError as exc:
            print(f"[agent] cloud rejected the report: {exc.code} "
                  f"({'check DEVICE_TOKEN' if exc.code == 401 else exc.reason})")
            _stop.wait(5)
            first = True
        except Exception as exc:
            print(f"[agent] report failed: {exc}")
            _stop.wait(3)
            first = True

        _stop.wait(args.interval)


# ---------------------------------------------------------------------------
# Kiosk browser
# ---------------------------------------------------------------------------
def launch_kiosk(url: str) -> subprocess.Popen | None:
    """Open the cloud UI fullscreen with no browser chrome."""
    browser = next((b for b in ("chromium-browser", "chromium", "chromium-browser-v7",
                                "google-chrome", "chrome")
                    if shutil.which(b)), None)
    if not browser:
        print("[kiosk] no Chromium found. Install it with:")
        print("[kiosk]   sudo apt install -y chromium-browser")
        return None

    kiosk_url = url if "kiosk=" in url else (
        url + ("&" if "?" in url else "?") + "kiosk=1")

    cmd = [
        browser,
        "--kiosk", f"--app={kiosk_url}",
        "--start-fullscreen",
        "--noerrdialogs",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--disable-features=TranslateUI,Translate",
        "--no-first-run",
        "--autoplay-policy=no-user-gesture-required",  # lets the scan beep play
        "--check-for-update-interval=31536000",
        "--overscroll-history-navigation=0",
        "--disable-pinch",
    ]
    print(f"[kiosk] launching {browser} at {kiosk_url}")
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"[kiosk] could not start the browser: {exc}")
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Smart Trolley Pi agent: streams the load cell and camera "
                    "to the cloud app.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Wiring: HX711 VCC->3V3, GND->GND, DT->GPIO5 (pin 29), "
               "SCK->GPIO6 (pin 31).",
    )
    p.add_argument("--cloud", default=settings.cloud_url,
                   help="cloud app base URL (default from CLOUD_URL)")
    p.add_argument("--token", default=settings.device_token,
                   help="shared device token (default from DEVICE_TOKEN)")
    p.add_argument("--device-id", default=settings.device_id)
    p.add_argument("--dt", type=int, default=settings.hx711_dt_pin,
                   help="HX711 DOUT BCM pin")
    p.add_argument("--sck", type=int, default=settings.hx711_sck_pin,
                   help="HX711 PD_SCK BCM pin")
    p.add_argument("--gain", type=int, default=settings.hx711_gain,
                   choices=[128, 64, 32])
    p.add_argument("--interval", type=float, default=settings.scale_stream_interval_s,
                   help="seconds between readings")
    p.add_argument("--camera-index", type=int, default=settings.camera_index)
    p.add_argument("--no-camera", action="store_true",
                   help="skip the camera scanner (USB gun still works)")
    p.add_argument("--kiosk", action="store_true",
                   help="also launch Chromium fullscreen at the cloud URL")
    p.add_argument("--kiosk-url", default="",
                   help="override the URL opened in the browser")
    p.add_argument("--http", action="store_true",
                   help="use HTTP polling instead of a WebSocket")
    p.add_argument("--once", action="store_true",
                   help="print a single load-cell reading and exit")
    return p


def main() -> int:
    args = build_parser().parse_args()

    print("=" * 66)
    print(f" Smart Trolley Pi agent v{AGENT_VERSION}")
    print(f" cloud      : {args.cloud}")
    print(f" device id  : {args.device_id}")
    print(f" HX711      : DT=GPIO{args.dt}  SCK=GPIO{args.sck}  gain={args.gain}")
    print("=" * 66)

    cell, cell_error = open_load_cell(args)
    if cell is None:
        print(f"[agent] LOAD CELL UNAVAILABLE: {cell_error}")
        print("[agent] The cloud will show the trolley as offline and refuse to")
        print("[agent] weight-verify scans. Diagnose with:")
        print("[agent]   python calibrate_scale.py diagnose")
        if args.once:
            return 1
    else:
        print("[agent] HX711 ready")

    if args.once:
        raw = cell.read()
        print(f"raw counts: {raw:.0f}")
        cell.close()
        return 0

    if not args.token:
        print("[agent] WARNING: no DEVICE_TOKEN set. A cloud app will reject "
              "this agent; only a LAN/localhost app will accept it.")

    scan_queue: queue.Queue = queue.Queue()
    scanner = None
    if not args.no_camera and settings.camera_enabled:
        scanner = start_camera(scan_queue, args.camera_index)

    browser = None
    if args.kiosk:
        browser = launch_kiosk(args.kiosk_url or args.cloud)

    def shutdown(*_):
        print("\n[agent] shutting down…")
        _stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    hello = {
        "agent_version": AGENT_VERSION,
        "scale_present": cell is not None,
        "scanner": "camera" if scanner and scanner.running else "usb-only",
        "error": cell_error,
    }

    try:
        if args.http:
            run_http(cell, scan_queue, args, hello)
        else:
            run_websocket(cell, scan_queue, args, hello)
    finally:
        if scanner:
            scanner.stop()
        if cell:
            cell.close()
        if browser:
            browser.terminate()
        print("[agent] stopped")
    return 0


if __name__ == "__main__":
    # allow running from any directory
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
