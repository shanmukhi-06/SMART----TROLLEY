"""Cloud <-> trolley device bridge.

When the app runs in the cloud it cannot touch the Pi's GPIO pins. Instead the
Pi runs `pi_agent.py`, a thin client that:

  * streams RAW HX711 counts up to the cloud (a few times a second), and
  * drains a command queue (tare / calibrate / ping) sent back down.

Deliberate design choice: the agent sends **raw counts**, not grams. All
calibration maths stays in the cloud (`BaseScale.get_grams`), so calibrating
from the settings UI takes effect instantly with no agent redeploy, and the
calibration has a single source of truth (the `app_settings` table).

Everything here is in-process state. That is fine for a single-trolley
prototype but it means the cloud app must run as ONE worker/replica; see
DEPLOY.md. Moving to many trolleys or many replicas is a Redis/IoT-Core swap
behind this same interface.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import settings


@dataclass
class Reading:
    """One sample pushed up by the agent."""

    raw: float
    grams_local: float | None = None  # agent's own view, for diagnostics only
    ts: float = field(default_factory=time.time)


@dataclass
class DeviceState:
    device_id: str
    connected: bool = False
    connected_at: float = 0.0
    last_seen: float = 0.0
    last_reading: Reading | None = None
    agent_version: str = ""
    scanner: str = ""            # what the agent reports it has ("camera", "none")
    scale_present: bool = False  # agent talked to a real HX711
    error: str = ""
    readings_received: int = 0
    scans_received: int = 0


class DeviceBridge:
    """Thread-safe registry for the single trolley device."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = DeviceState(device_id=settings.device_id)
        self._commands: deque[dict[str, Any]] = deque(maxlen=32)
        self._command_event = threading.Event()
        # main.py registers a coroutine-safe pusher so agent scans reach the UI
        self._scan_sink: Callable[[str], None] | None = None

    # -- agent lifecycle ---------------------------------------------------
    def on_connect(self, device_id: str, info: dict | None = None) -> DeviceState:
        info = info or {}
        with self._lock:
            now = time.time()
            self._state.device_id = device_id or settings.device_id
            self._state.connected = True
            self._state.connected_at = now
            self._state.last_seen = now
            self._state.agent_version = str(info.get("agent_version", ""))
            self._state.scanner = str(info.get("scanner", ""))
            self._state.scale_present = bool(info.get("scale_present", False))
            self._state.error = str(info.get("error", ""))
            return self._state

    def on_disconnect(self) -> None:
        with self._lock:
            self._state.connected = False
            # keep last_reading so the UI can show "last seen" info

    def touch(self) -> None:
        with self._lock:
            self._state.last_seen = time.time()

    # -- inbound data ------------------------------------------------------
    def push_reading(self, raw: float, grams_local: float | None = None) -> None:
        with self._lock:
            self._state.last_reading = Reading(raw=float(raw), grams_local=grams_local)
            self._state.last_seen = time.time()
            self._state.readings_received += 1

    def push_error(self, message: str) -> None:
        with self._lock:
            self._state.error = str(message)[:300]
            self._state.last_seen = time.time()

    def push_scan(self, barcode: str) -> None:
        """Agent decoded a barcode from the camera; hand it to the app."""
        with self._lock:
            self._state.scans_received += 1
            self._state.last_seen = time.time()
            sink = self._scan_sink
        if sink and barcode:
            sink(barcode)

    def set_scan_sink(self, sink: Callable[[str], None] | None) -> None:
        with self._lock:
            self._scan_sink = sink

    # -- reading access ----------------------------------------------------
    def latest_raw(self) -> float | None:
        """Most recent raw count, or None if there is nothing fresh."""
        with self._lock:
            reading = self._state.last_reading
            if reading is None:
                return None
            if time.time() - reading.ts > settings.device_stale_after_s:
                return None
            return reading.raw

    def is_online(self) -> bool:
        return self.latest_raw() is not None

    def age_s(self) -> float | None:
        with self._lock:
            reading = self._state.last_reading
            if reading is None:
                return None
            return round(time.time() - reading.ts, 2)

    # -- outbound commands -------------------------------------------------
    def send_command(self, kind: str, **payload: Any) -> dict:
        cmd = {"type": kind, "id": f"{kind}-{int(time.time() * 1000)}", **payload}
        with self._lock:
            self._commands.append(cmd)
        self._command_event.set()
        return cmd

    def drain_commands(self) -> list[dict]:
        with self._lock:
            cmds = list(self._commands)
            self._commands.clear()
            if not self._commands:
                self._command_event.clear()
        return cmds

    def wait_for_commands(self, timeout: float = 20.0) -> list[dict]:
        """Block until a command is queued (long-poll support)."""
        self._command_event.wait(timeout=timeout)
        return self.drain_commands()

    # -- diagnostics -------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            s = self._state
            reading = s.last_reading
            return {
                "device_id": s.device_id,
                "connected": s.connected,
                "online": self.is_online(),
                "last_seen_age_s": (
                    round(time.time() - s.last_seen, 2) if s.last_seen else None
                ),
                "reading_age_s": self.age_s(),
                "raw": reading.raw if reading else None,
                "grams_local": reading.grams_local if reading else None,
                "agent_version": s.agent_version,
                "scanner": s.scanner,
                "scale_present": s.scale_present,
                "error": s.error,
                "readings_received": s.readings_received,
                "scans_received": s.scans_received,
                "stale_after_s": settings.device_stale_after_s,
            }


bridge = DeviceBridge()
