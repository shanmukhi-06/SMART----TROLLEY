"""Thermal printer hardware abstraction layer.

Three transport backends, one interface
---------------------------------------
SimulatedPrinter   software stub for laptop development (no printer needed).
USBPrinter         direct USB or USB-serial device (CH340, CP2102, etc.).
BluetoothPrinter   classic Bluetooth SPP or BLE thermal printer.
NetworkPrinter     LAN/Wi-Fi printer reachable on port 9100 (many 58 mm /
                   80 mm models have an Ethernet or AP mode).
RemotePrinter      used when the app runs in the CLOUD. Print commands are
                   forwarded to the Pi agent through `app.devices.bridge`.

ESC/POS reference
-----------------
Every implementation talks the same ESC/POS command set so receipt formatting
is identical regardless of transport.  The module handles:

  * bold / double-height / alignment
  * QR-code rendering (for UPI payment)
  * paper-feed + cut
  * status/health checks where the transport supports it

Discovery
---------
`discover_printers()` scans for every transport the host can reach and returns
a list of `PrinterInfo` namedtuples.  The factory `get_printer()` picks the
first available printer when no explicit transport is configured.

Adding a new transport (e.g. serial-over-USB at /dev/ttyUSB0) is a single
class that implements `BasePrinter` — nothing in the rest of the app changes.
"""
from __future__ import annotations

import ipaddress
import os
import platform
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.config import settings


# ---------------------------------------------------------------------------
# Data class used by the discovery API
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PrinterInfo:
    """One discovered printer, ready for `get_printer(transport=...)`."""

    kind: str          # "usb" | "bluetooth" | "network" | "cups"
    name: str          # human-readable label
    address: str = ""  # device path, BT addr, or IP:port
    detail: str = ""   # extra info (driver, model, etc.)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------
class BasePrinter:
    """Common interface shared by every printer implementation."""

    kind = "base"
    real_hardware = False

    # -- lifecycle -----------------------------------------------------------
    def __init__(self) -> None:
        self._width_chars: int = 48   # 80 mm paper @ 8 cpi ≈ 48 chars
        self._charset: str = "cp437"

    # -- low-level raw ESC/POS -----------------------------------------------
    def _write(self, data: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def _esc(self, *bytes_: int) -> None:
        self._write(bytes(bytes_))

    def init(self) -> None:
        """Reset printer to default state."""
        self._esc(0x1B, 0x40)

    def bold(self, on: bool = True) -> None:
        self._esc(0x1B, 0x45, 1 if on else 0)

    def double_height(self, on: bool = True) -> None:
        self._esc(0x1B, 0x21, 0x10 if on else 0x00)

    def align(self, position: str = "left") -> None:
        mapping = {"left": 0, "center": 1, "right": 2}
        self._esc(0x1B, 0x61, mapping.get(position, 0))

    def feed(self, lines: int = 3) -> None:
        self._write(b"\n" * max(1, lines))

    def cut(self) -> None:
        """Full cut (if supported by the printer)."""
        self._esc(0x1D, 0x56, 0x00)

    def print_text(self, text: str) -> None:
        """Print a raw string, wrapping at paper width."""
        for line in text.split("\n"):
            self._write(line[: self._width_chars].encode(self._charset, errors="replace") + b"\n")

    def print_receipt(self, order) -> None:  # noqa: ANN001
        """Render and print a full shopping receipt for *order*.

        ``order`` is a SQLAlchemy model instance with at least:
          order_ref, paid_at, mobile, email, payment_provider,
          payment_ref, items (relationship), subtotal, tax, total
        """
        from app.services.receipts import render_html  # local import avoids cycle

        html = render_html(order)
        self._print_from_html(html)

    def _print_from_html(self, html: str) -> None:
        """Minimal HTML → ESC/POS converter.

        Handles the subset of tags our receipt template produces: <b>, <br>,
        headings, tables, and <hr>.  Everything else is stripped.
        """
        import html as html_mod
        import re

        text = html_mod.unescape(re.sub(r"<[^>]+>", "", html))
        self.print_text(text)
        self.feed(2)
        self.cut()

    def qr(self, data: str, size: int = 6) -> None:
        """Print a QR code (model 2, micro QR not supported by all printers)."""
        self._esc(0x1D, 0x28, 0x6B, 0x04, 0x00, 0x31, 0x41, 0x03, 0x00)  # model 2
        self._esc(0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x43, size)
        length = len(data.encode("utf-8")) + 3
        self._esc(0x1D, 0x28, 0x6B, length & 0xFF, (length >> 8) & 0xFF, 0x31, 0x50, 0x30)
        self._write(data.encode("utf-8"))
        self._esc(0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x51, 0x30)

    # -- health --------------------------------------------------------------
    def health(self) -> dict:
        return {
            "kind": self.kind,
            "real_hardware": self.real_hardware,
            "online": True,
            "detail": "",
        }

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Simulated printer
# ---------------------------------------------------------------------------
class SimulatedPrinter(BasePrinter):
    """No-op printer for development and testing."""

    kind = "simulated"
    real_hardware = False

    def __init__(self, detail: str = "") -> None:
        super().__init__()
        self._lines: list[str] = []
        self._detail = detail or "software printer"

    def _write(self, data: bytes) -> None:
        try:
            self._lines.append(data.decode(self._charset, errors="replace"))
        except Exception:
            self._lines.append(repr(data))

    def print_text(self, text: str) -> None:
        for line in text.split("\n"):
            self._lines.append(line[: self._width_chars])

    def get_output(self) -> str:
        return "\n".join(self._lines)

    def health(self) -> dict:
        return {**super().health(), "detail": self._detail}


# ---------------------------------------------------------------------------
# Raw winspool helper (Windows-only, talks to the print spooler directly)
# ---------------------------------------------------------------------------
def _print_via_winspool(printer_name: str, data: bytes) -> bool:
    """Send raw bytes to a Windows print queue via winspool.drv.

    Uses StartDocPrinterW with DATATYPE=RAW so the printer driver doesn't
    mangle the ESC/POS stream — essential for thermal receipt printers.
    """
    if platform.system() != "Windows":
        return False
    if not printer_name:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

        class DOC_INFO_1(ctypes.Structure):
            _fields_ = [
                ("pDocName", wintypes.LPWSTR),
                ("pOutputFile", wintypes.LPWSTR),
                ("pDatatype", wintypes.LPWSTR),
            ]

        winspool.OpenPrinterW.restype = wintypes.BOOL
        winspool.OpenPrinterW.argtypes = [
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.c_void_p,
        ]
        winspool.StartDocPrinterW.restype = wintypes.DWORD
        winspool.StartDocPrinterW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(DOC_INFO_1),
        ]
        winspool.WritePrinter.restype = wintypes.BOOL
        winspool.WritePrinter.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]

        h = wintypes.HANDLE()
        if not winspool.OpenPrinterW(printer_name, ctypes.byref(h), None):
            print(f"[printer] OpenPrinter({printer_name!r}) failed: "
                  f"err={ctypes.get_last_error()}")
            return False
        try:
            doc = DOC_INFO_1(pDocName="receipt", pOutputFile=None, pDatatype="RAW")
            job_id = winspool.StartDocPrinterW(h, 1, ctypes.byref(doc))
            if not job_id:
                print(f"[printer] StartDocPrinter failed: "
                      f"err={ctypes.get_last_error()}")
                return False
            winspool.StartPagePrinter(h)
            written = wintypes.DWORD(0)
            ok = winspool.WritePrinter(
                h, ctypes.c_char_p(data), len(data), ctypes.byref(written)
            )
            winspool.EndPagePrinter(h)
            winspool.EndDocPrinter(h)
            return bool(ok and written.value == len(data))
        finally:
            winspool.ClosePrinter(h)
    except Exception as exc:
        print(f"[printer] winspool print failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# USB printer
# ---------------------------------------------------------------------------
class USBPrinter(BasePrinter):
    """Direct USB or USB-serial thermal printer.

    Three transports are supported:

      1. Windows print spooler (default on Windows when ``device`` is the
         name of a Windows-installed printer, e.g. ``POS58 Printer``).
         Bytes are written as a RAW print job via winspool.drv so the
         ESC/POS stream is not mangled by the driver.
      2. ``python-escpos`` Usb driver (Linux/macOS direct USB, requires
         pyusb + libusb backend).
      3. Raw file write (any platform, when ``device`` is a serial port
         like ``/dev/usb/lp0`` or ``COM3``).

    Config (from ``PRINTER_*`` env vars / .env):
      PRINTER_USB_DEVICE   Windows printer name, /dev/ttyUSB0, COM3, etc.
      PRINTER_USB_DRIVER   ``auto`` (default), ``winspool``, ``escpos``,
                            ``raw``
    """

    kind = "usb"
    real_hardware = True

    def __init__(self, device: str = "", driver: str = "auto") -> None:
        super().__init__()
        self._device = device or getattr(settings, "printer_usb_device", "")
        self._driver = driver or getattr(settings, "printer_usb_driver", "auto")
        self._escpos = None
        self._lock = threading.Lock()
        if self._driver in ("auto", "winspool") and platform.system() == "Windows":
            # winspool: nothing to open ahead of time — handle per print job
            self._driver = "winspool"
            return
        if self._driver in ("auto", "escpos"):
            self._escpos = self._build_escpos()
            if self._escpos is not None:
                self._driver = "escpos"
                return
        # raw fallback — just write to a device path
        self._driver = "raw"

    def _build_escpos(self):
        try:
            from escpos.printer import Usb as _Usb  # type: ignore[import-not-found,import-untyped]
            vid_hex = getattr(settings, "printer_usb_vendor", "") or ""
            pid_hex = getattr(settings, "printer_usb_product", "") or ""
            if vid_hex and pid_hex:
                return _Usb(int(vid_hex, 16), int(pid_hex, 16))
            from escpos.printer import File as _File  # type: ignore[import-not-found,import-untyped]
            dev = (self._device or "").strip()
            # Only use File printer if it's an actual device path, not a printer queue name
            if dev and (dev.startswith("/dev/") or dev.upper().startswith("COM") or dev.startswith("\\\\.\\")):
                return _File(dev)
        except Exception:
            return None
        return None

    def _write(self, data: bytes) -> None:
        with self._lock:
            if self._driver == "winspool":
                ok = _print_via_winspool(self._device, data)
                if not ok:
                    print(f"[printer] winspool failed for {self._device!r}")
                return
            if self._escpos is not None:
                try:
                    self._escpos._raw(data)  # type: ignore[attr-defined]
                    return
                except Exception:
                    pass
            self._raw_write(data)

    def _raw_write(self, data: bytes) -> None:
        if not self._device:
            return
        dev = self._device.strip()
        # Strictly ensure this is a device path (serial port / unix node), not a printer name
        is_device_path = (
            dev.startswith("/dev/")
            or dev.upper().startswith("COM")
            or dev.startswith("\\\\.\\")
        )
        if not is_device_path:
            print(f"[printer] {dev!r} is not a serial or OS device path; skipping file write.")
            return
        try:
            with open(dev, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            print(f"[printer] raw write to {dev} failed: {exc}")

    def close(self) -> None:
        if self._escpos is not None:
            try:
                self._escpos.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    def health(self) -> dict:
        info = super().health()
        info["device"] = self._device or "auto"
        info["driver"] = self._driver
        if self._driver == "winspool" and platform.system() == "Windows":
            try:
                import ctypes
                from ctypes import wintypes
                winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
                winspool.OpenPrinterW.restype = wintypes.BOOL
                winspool.OpenPrinterW.argtypes = [
                    wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p
                ]
                winspool.ClosePrinter.argtypes = [wintypes.HANDLE]

                h = wintypes.HANDLE()
                if winspool.OpenPrinterW(self._device, ctypes.byref(h), None):
                    info["online"] = True
                    info["detail"] = "winspool: print queue reachable"
                    winspool.ClosePrinter(h)
                else:
                    err = ctypes.get_last_error()
                    info["online"] = False
                    if err == 1722:
                        info["detail"] = "Windows Print Spooler service is stopped"
                    else:
                        info["detail"] = f"winspool error code {err} (printer not found or offline)"
            except Exception as exc:
                info["online"] = False
                info["detail"] = f"winspool error: {exc}"
        return info


# ---------------------------------------------------------------------------
# Bluetooth printer
# ---------------------------------------------------------------------------
class BluetoothPrinter(BasePrinter):
    """Bluetooth thermal printer.

    Two sub-transports, selected by ``PRINTER_BT_TRANSPORT``:
      ``rfcomm``   classic Bluetooth SPP via a dedicated RFCOMM device
                   (``/dev/rfcomm0`` on Linux, ``COMx`` on Windows).
      ``ble``      Bluetooth Low Energy via `` bleak ``.

    Config:
      PRINTER_BT_ADDRESS    MAC / BLE address (required when not using auto-scan)
      PRINTER_BT_NAME       advertised device name fragment (used during scan)
      PRINTER_BT_CHANNEL    RFCOMM channel (default 1 — most printers use 1)
      PRINTER_BT_TRANSPORT  ``rfcomm`` | ``ble``
    """

    kind = "bluetooth"
    real_hardware = True

    def __init__(
        self,
        address: str = "",
        name: str = "",
        channel: int = 1,
        transport: str = "rfcomm",
    ) -> None:
        super().__init__()
        self._address = address or getattr(settings, "printer_bt_address", "")
        self._name = name or getattr(settings, "printer_bt_name", "")
        self._channel = channel or int(getattr(settings, "printer_bt_channel", 1))
        self._transport = transport or getattr(settings, "printer_bt_transport", "rfcomm")
        self._rfcomm_dev = ""
        self._ble_client = None
        self._lock = threading.Lock()

        if self._transport == "ble":
            self._connect_ble()
        else:
            self._connect_rfcomm()

    # -- RFCOMM (classic Bluetooth SPP) --------------------------------------
    def _connect_rfcomm(self) -> None:
        system = platform.system()
        if system == "Linux":
            self._rfcomm_dev = self._bind_rfcomm_linux()
        elif system == "Windows":
            # On Windows, the BT stack creates a virtual COM port; use it directly
            self._rfcomm_dev = self._address or self._find_com_port()
        elif system == "Darwin":
            # macOS: use a serial port path if the BT device is paired
            self._rfcomm_dev = self._find_mac_serial()

    def _bind_rfcomm_linux(self) -> str:
        """Bind the remote BT addr to /dev/rfcommN if not already bound."""
        dev = f"/dev/rfcomm0"
        if not self._address:
            return dev
        try:
            subprocess.run(
                ["sudo", "rfcomm", "bind", "rfcomm0", self._address, str(self._channel)],
                check=False, capture_output=True, timeout=10,
            )
            time.sleep(1)
        except Exception:
            pass
        return dev

    def _find_com_port(self) -> str:
        """Best-effort COM port discovery on Windows."""
        try:
            import serial.tools.list_ports  # type: ignore[import-untyped]

            for p in serial.tools.list_ports.comports():
                if "bluetooth" in (p.description or "").lower() or "standard" in (p.description or "").lower():
                    return p.device
        except Exception:
            pass
        return "COM3"  # common default — user should override

    def _find_mac_serial(self) -> str:
        try:
            import serial.tools.list_ports  # type: ignore[import-untyped]

            for p in serial.tools.list_ports.comports():
                if self._name.lower() in (p.description or "").lower():
                    return p.device
        except Exception:
            pass
        return "/dev/tty.Bluetooth-Incoming-Port"

    def _open_serial(self):
        import serial  # type: ignore[import-untyped]

        return serial.Serial(
            port=self._rfcomm_dev,
            baudrate=9600,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=5,
        )

    # -- BLE ------------------------------------------------------------------
    def _connect_ble(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner  # type: ignore[import-untyped]

            addr = self._address or self._scan_ble()
            if not addr:
                return
            self._ble_client = BleakClient(addr)
        except Exception:
            self._ble_client = None

    def _scan_ble(self) -> str:
        """Scan for the first BLE device whose name matches."""
        try:
            from bleak import BleakScanner  # type: ignore[import-untyped]

            async def _scan():
                devices = await BleakScanner.discover(timeout=5.0)
                for d in devices:
                    name = (d.name or "").lower()
                    if self._name.lower() in name and "printer" in name:
                        return d.address
                return None

            import asyncio
            return asyncio.run(_scan()) or ""
        except Exception:
            return ""

    # -- BasePrinter interface -----------------------------------------------
    def _write(self, data: bytes) -> None:
        with self._lock:
            try:
                if self._transport == "ble" and self._ble_client is not None:
                    import asyncio

                    async def _send():
                        await self._ble_client.write_gatt_char(
                            self._find_write_char(), data, response=False
                        )

                    asyncio.run(_send())
                    return
            except Exception:
                pass
            # RFCOMM / fallback: write to the serial device
            try:
                ser = self._open_serial()
                ser.write(data)
                ser.close()
            except Exception:
                pass

    def _find_write_char(self):
        if self._ble_client is None:
            return None
        for s in self._ble_client.services:
            for c in s.characteristics:
                if "write" in c.properties:
                    return c.uuid
        return None

    def close(self) -> None:
        if self._ble_client is not None:
            try:
                import asyncio

                asyncio.run(self._ble_client.disconnect())
            except Exception:
                pass
        if self._rfcomm_dev and platform.system() == "Linux":
            try:
                subprocess.run(
                    ["sudo", "rfcomm", "unbind", "rfcomm0"],
                    check=False, capture_output=True, timeout=5,
                )
            except Exception:
                pass

    def health(self) -> dict:
        info = super().health()
        info["address"] = self._address
        info["channel"] = self._channel
        info["transport"] = self._transport
        info["rfcomm_dev"] = self._rfcomm_dev
        if self._transport == "ble":
            info["ble_connected"] = bool(self._ble_client)
        return info


# ---------------------------------------------------------------------------
# Network printer
# ---------------------------------------------------------------------------
class NetworkPrinter(BasePrinter):
    """LAN / Wi-Fi thermal printer on TCP port 9100 (the de-facto standard).

    Config:
      PRINTER_NET_HOST    IP address or hostname
      PRINTER_NET_PORT    TCP port (default 9100)
    """

    kind = "network"
    real_hardware = True

    def __init__(self, host: str = "", port: int = 9100) -> None:
        super().__init__()
        self._host = host or getattr(settings, "printer_net_host", "")
        self._port = int(getattr(settings, "printer_net_port", 9100))
        self._lock = threading.Lock()

    def _write(self, data: bytes) -> None:
        with self._lock:
            try:
                with socket.create_connection((self._host, self._port), timeout=5) as sock:
                    sock.sendall(data)
                    time.sleep(0.15)  # give the printer time to finish
            except Exception:
                pass

    def health(self) -> dict:
        info = super().health()
        info["host"] = self._host
        info["port"] = self._port
        try:
            with socket.create_connection((self._host, self._port), timeout=3) as sock:
                info["online"] = True
                info["detail"] = "TCP connection OK"
        except Exception as exc:
            info["online"] = False
            info["detail"] = str(exc)
        return info


# ---------------------------------------------------------------------------
# Remote printer (cloud mode)
# ---------------------------------------------------------------------------
class RemotePrinter(BasePrinter):
    """Print commands are forwarded to the Pi agent over the device bridge.

    The agent does the actual ESC/POS writes; here we just serialise the
    command and drop it on the bridge queue.  The agent's ``handle_command``
    loop picks it up.
    """

    kind = "remote"
    real_hardware = True

    def __init__(self) -> None:
        super().__init__()
        from app.devices import bridge

        self._bridge = bridge

    def _write(self, data: bytes) -> None:
        self._bridge.send_command(
            "print",
            data=data.hex(),  # JSON-safe; agent decodes back to bytes
            charset=self._charset,
            width_chars=self._width_chars,
        )

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Module-level singleton factory
# ---------------------------------------------------------------------------
_printer: BasePrinter | None = None
_init_error: str = ""


def _build_printer() -> BasePrinter:
    global _init_error
    try:
        from app.services import settings_store
        persisted = settings_store.read_printer_settings()
        transport = persisted.get("transport") or getattr(settings, "printer_transport", "auto")
        device = persisted.get("device") or getattr(settings, "printer_usb_device", "")
        driver = persisted.get("driver") or getattr(settings, "printer_usb_driver", "auto")
    except Exception:
        transport = getattr(settings, "printer_transport", "auto")
        device = getattr(settings, "printer_usb_device", "")
        driver = getattr(settings, "printer_usb_driver", "auto")

    transport = (transport or "auto").strip().lower()

    # Remote printer for cloud mode
    if transport == "remote" or (transport == "auto" and settings.is_cloud):
        return RemotePrinter()

    # Simulation mode
    if transport in ("simulation", "sim"):
        return SimulatedPrinter(detail="simulation configured")

    # Explicit transport selection
    if transport == "usb":
        try:
            return USBPrinter(device=device, driver=driver)
        except Exception as exc:
            _init_error = str(exc)
            print(f"[printer] USB unavailable: {exc}")
    if transport == "bluetooth":
        try:
            return BluetoothPrinter()
        except Exception as exc:
            _init_error = str(exc)
            print(f"[printer] Bluetooth unavailable: {exc}")
    if transport == "network":
        try:
            return NetworkPrinter()
        except Exception as exc:
            _init_error = str(exc)
            print(f"[printer] Network unavailable: {exc}")

    # auto: discovery
    discovered = discover_printers()
    candidates = [
        d for d in discovered
        if not d.name.startswith("Print Spooler (Service Stopped)")
        and not d.detail.startswith("Windows Print Spooler service is stopped")
    ]
    if candidates:
        info = candidates[0]
        try:
            if info.kind == "usb":
                return USBPrinter(device=info.name if platform.system() == "Windows" else info.address)
            if info.kind == "bluetooth":
                return BluetoothPrinter(address=info.address, name=info.name)
            if info.kind == "network":
                host, _, port_str = info.address.rpartition(":")
                return NetworkPrinter(host=host, port=int(port_str or 9100))
            if info.kind == "cups":
                return USBPrinter(driver="cups")
        except Exception as exc:
            _init_error = str(exc)
            print(f"[printer] Could not open {info}: {exc}")

    # Fall back to simulation
    print("[printer] No hardware printer found — using software printer.")
    return SimulatedPrinter(detail="no hardware detected")


def get_printer() -> BasePrinter:
    """Return the current printer singleton."""
    global _printer
    if _printer is None:
        _printer = _build_printer()
    return _printer


def reset_printer() -> None:
    """Drop the singleton (tests / mode changes)."""
    global _printer
    if _printer is not None:
        _printer.close()
    _printer = None


def printer_health() -> dict:
    p = get_printer()
    info = p.health()
    if _init_error:
        info["init_error"] = _init_error
    return info


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------
def discover_printers() -> list[PrinterInfo]:
    """Scan for every printer transport the host can reach.

    Returns a list sorted by reliability (USB > network > Bluetooth).
    """
    found: list[PrinterInfo] = []
    if platform.system() == "Windows":
        found.extend(_discover_winspool())
    found.extend(_discover_cups())
    found.extend(_discover_usb_serial())
    found.extend(_discover_network())
    found.extend(_discover_bluetooth())
    return found


def _discover_winspool() -> list[PrinterInfo]:
    """Discover Windows printer queues using winspool.drv."""
    if platform.system() != "Windows":
        return []
    found: list[PrinterInfo] = []
    try:
        import ctypes
        from ctypes import wintypes

        winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

        class PRINTER_INFO_2(ctypes.Structure):
            _fields_ = [
                ("pServerName", wintypes.LPWSTR),
                ("pPrinterName", wintypes.LPWSTR),
                ("pShareName", wintypes.LPWSTR),
                ("pPortName", wintypes.LPWSTR),
                ("pDriverName", wintypes.LPWSTR),
                ("pComment", wintypes.LPWSTR),
                ("pLocation", wintypes.LPWSTR),
                ("pDevMode", ctypes.c_void_p),
                ("pSepFile", wintypes.LPWSTR),
                ("pPrintProcessor", wintypes.LPWSTR),
                ("pDatatype", wintypes.LPWSTR),
                ("pParameters", wintypes.LPWSTR),
                ("pSecurityDescriptor", ctypes.c_void_p),
                ("Attributes", wintypes.DWORD),
                ("Priority", wintypes.DWORD),
                ("DefaultPriority", wintypes.DWORD),
                ("StartTime", wintypes.DWORD),
                ("UntilTime", wintypes.DWORD),
                ("Status", wintypes.DWORD),
                ("cJobs", wintypes.DWORD),
                ("AveragePPM", wintypes.DWORD),
            ]

        flags = 0x00000002 | 0x00000004  # LOCAL | CONNECTIONS
        needed = wintypes.DWORD(0)
        returned = wintypes.DWORD(0)

        winspool.EnumPrintersW(flags, None, 2, None, 0, ctypes.byref(needed), ctypes.byref(returned))
        err = ctypes.get_last_error()
        if err == 1722:  # RPC_S_SERVER_UNAVAILABLE
            return [
                PrinterInfo(
                    kind="usb",
                    name="Print Spooler (Service Stopped)",
                    address="",
                    detail="Windows Print Spooler service is stopped",
                )
            ]

        if needed.value > 0:
            buf = ctypes.create_string_buffer(needed.value)
            ok = winspool.EnumPrintersW(
                flags, None, 2, buf, needed.value, ctypes.byref(needed), ctypes.byref(returned)
            )
            if ok and returned.value > 0:
                printers = ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_2))
                for i in range(returned.value):
                    p = printers[i]
                    p_name = p.pPrinterName or "Unknown"
                    found.append(
                        PrinterInfo(
                            kind="usb",
                            name=p_name,
                            address=p.pPortName or "",
                            detail=f"Driver: {p.pDriverName or ''} (Windows Queue)",
                        )
                    )
    except Exception as exc:
        print(f"[printer] winspool discovery error: {exc}")
    return found


def _discover_cups() -> list[PrinterInfo]:
    try:
        result = subprocess.run(
            ["lpstat", "-p"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "printer" in line.lower():
                name = line.split()[1] if len(line.split()) > 1 else "unknown"
                return [PrinterInfo(kind="cups", name=name, detail="CUPS queue")]
    except Exception:
        pass
    return []


def _discover_usb_serial() -> list[PrinterInfo]:
    found: list[PrinterInfo] = []
    system = platform.system()
    patterns: list[str] = []

    if system == "Linux":
        patterns = ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/usb/lp*"]
    elif system == "Windows":
        # COM ports — handled via serial.tools below
        try:
            import serial.tools.list_ports  # type: ignore[import-untyped]

            for p in serial.tools.list_ports.comports():
                if p.vid is not None and p.pid is not None:
                    found.append(PrinterInfo(
                        kind="usb",
                        name=p.description or p.device,
                        address=p.device,
                        detail=f"VID={p.vid:04X} PID={p.pid:04X}",
                    ))
            return found
        except Exception:
            return []
    elif system == "Darwin":
        patterns = ["/dev/tty.usbserial-*", "/dev/tty.usbmodem*"]

    import glob

    for pattern in patterns:
        for path in glob.glob(pattern):
            found.append(PrinterInfo(kind="usb", name=os.path.basename(path), address=path))
    return found


def _discover_network() -> list[PrinterInfo]:
    """Best-effort scan of the local subnet for port 9100."""
    found: list[PrinterInfo] = []
    try:
        # Try common local subnets — quick, not exhaustive
        candidates = _local_candidates()
        for host in candidates[:20]:  # cap to avoid long scans
            try:
                with socket.create_connection((host, 9100), timeout=0.5):
                    found.append(PrinterInfo(
                        kind="network", name=f"Printer@{host}", address=f"{host}:9100"
                    ))
            except Exception:
                continue
    except Exception:
        pass
    return found


def _local_candidates() -> list[str]:
    """Derive local /24 subnets from active interfaces."""
    candidates: list[str] = []
    try:
        try:
            import netifaces2 as netifaces  # type: ignore[import-untyped]
        except ImportError:
            try:
                import netifaces  # type: ignore[import-untyped]
            except ImportError:
                netifaces = None

        if netifaces is None:
            raise ImportError("netifaces not available")

        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
            for addr in addrs:
                ip = addr.get("addr", "")
                if ip and not ip.startswith("127.") and not ip.startswith("169.254"):
                    try:
                        net = ipaddress.ip_network(f"{ip}/24", strict=False)
                        candidates.extend([str(net.network_address + i) for i in range(1, 254)])
                    except ValueError:
                        continue
    except Exception:
        # Fallback: common home / lab subnets
        for octet in (1, 2, 100, 101, 192, 200, 254):
            candidates.append(f"192.168.{octet}.1")
            candidates.append(f"192.168.{octet}.100")
        for octet in (1, 2, 100):
            candidates.append(f"10.0.{octet}.1")
    return candidates


def _discover_bluetooth() -> list[PrinterInfo]:
    found: list[PrinterInfo] = []
    try:
        from bleak import BleakScanner  # type: ignore[import-untyped]

        import asyncio

        async def _scan():
            devices = await BleakScanner.discover(timeout=5.0)
            for d in devices:
                name = (d.name or "").lower()
                if "printer" in name or "pos" in name or "58mm" in name or "80mm" in name:
                    found.append(PrinterInfo(
                        kind="bluetooth", name=d.name or d.address, address=d.address
                    ))

        asyncio.run(_scan())
    except Exception:
        pass
    # Classic BT: try sdptool on Linux
    if platform.system() == "Linux":
        try:
            result = subprocess.run(
                ["sdptool", "browse", "local"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if "Serial Port" in line:
                    found.append(PrinterInfo(kind="bluetooth", name="BT-SPP", detail="Serial Port"))
                    break
        except Exception:
            pass
    return found
