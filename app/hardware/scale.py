"""Load cell weight reader — 4 x 50 kg cells -> HX711 -> Raspberry Pi 5.

Three implementations, one interface
-----------------------------------
HX711Scale     bit-bangs the HX711 protocol over `lgpio` (Pi 5 native).
RemoteScale    used when the app runs in the CLOUD. Raw counts arrive from
               `pi_agent.py` through `app.devices.bridge`; all calibration
               maths still happens here, so calibrating from the settings UI
               takes effect immediately.
SimulatedScale pure software scale for laptop development / demos.

Wiring (four 50 kg half-bridge cells combined into one full Wheatstone bridge)
-----------------------------------------------------------------------------
Each 50 kg cell has 3 wires. Combine the four cells so opposite corners form
the bridge (a combinator board does this for you), then:

    Bridge  ->  HX711 : E+  E-  A+  A-
    HX711 VCC         -> Pi 3V3 (pin 1) or 5V (pin 2)   see note below
    HX711 GND         -> Pi GND (pin 6)
    HX711 DT  (DOUT)  -> BCM GPIO 5  (pin 29)   HX711_DT_PIN
    HX711 SCK (PD_SCK)-> BCM GPIO 6  (pin 31)   HX711_SCK_PIN

Note on VCC: the Pi's GPIO pins are 3.3 V. Powering the HX711 from 5 V makes
its DOUT swing to ~5 V, which is out of spec for the Pi. Either power the HX711
from 3V3 (simplest, slightly noisier) or keep 5 V and put a level shifter (or a
1k/2k divider on DOUT) between DOUT and GPIO 5. Never feed 5 V logic straight
into a Pi input.

No buzzer or LEDs are used on this trolley — scan feedback is on screen.
"""
from __future__ import annotations

import statistics
import threading
import time

from app.config import settings


class BaseScale:
    """Common calibration maths shared by every scale implementation."""

    #: shown in the UI / diagnostics
    kind = "base"
    #: does this reading path involve a real load cell?
    real_hardware = False

    def __init__(self) -> None:
        self._reference_unit = settings.scale_reference_unit or 1.0
        self._offset = settings.scale_offset
        self._calibration_source = "env"
        self._ema_grams: float | None = None
        self._last_valid_raw: float = 0.0
        # Fault tracking: a rail-saturated / glitched conversion is discarded and
        # replaced by _last_valid_raw so the filters stay smooth. Remember that it
        # happened, otherwise the cached value silently masks an open bridge and
        # health() cheerfully reports a perfectly plausible fake weight.
        self._last_reject_raw: float | None = None
        self._consecutive_rejects: int = 0

    # -- calibration -------------------------------------------------------
    def set_calibration(
        self, reference_unit: float, offset: float, source: str = "runtime"
    ) -> None:
        self._reference_unit = reference_unit or 1.0
        self._offset = offset
        self._calibration_source = source
        self._ema_grams = None

    @property
    def calibration(self) -> dict:
        return {
            "reference_unit": self._reference_unit,
            "offset": self._offset,
            "source": self._calibration_source,
        }

    # -- reading -----------------------------------------------------------
    def read_raw(self) -> float:  # pragma: no cover - overridden
        raise NotImplementedError

    def raw_to_grams(self, raw: float) -> float:
        # Use abs() so an accidentally-negative reference_unit doesn't flip weight sign
        ref = abs(self._reference_unit) or 1.0
        return round((raw - self._offset) / ref, 1)

    def get_grams(self, samples: int = 15) -> float:
        """Current basket weight in grams (trimmed median + adaptive EMA filter)."""
        readings = []
        for _ in range(max(3, samples)):
            try:
                r = self.read_raw()
                if r is not None and abs(r) < 8388000:
                    readings.append(r)
            except Exception:
                pass
            time.sleep(0.004)

        if not readings:
            return 0.0 if self._ema_grams is None else round(self._ema_grams, 1)

        readings.sort()
        # Trim outer 20% to eliminate outliers / mechanical vibration spikes
        trim = len(readings) // 5
        trimmed = readings[trim:-trim] if trim > 0 and len(readings) > 2 * trim else readings
        raw_median = statistics.median(trimmed)
        grams_inst = self.raw_to_grams(raw_median)

        # Adaptive Exponential Moving Average (EMA)
        if self._ema_grams is None:
            self._ema_grams = grams_inst
        else:
            delta = abs(grams_inst - self._ema_grams)
            # Fast tracking on big weight changes (adding/removing items), smooth damping when resting
            alpha = 0.85 if delta > 25.0 else (0.40 if delta > 8.0 else 0.15)
            self._ema_grams = alpha * grams_inst + (1.0 - alpha) * self._ema_grams

        # Automatic zero-tracking / deadband snap near zero (±2.5g)
        out_grams = self._ema_grams
        if abs(out_grams) < 2.5:
            out_grams = 0.0

        return round(out_grams, 1)

    def read_stats(self, samples: int = 20) -> dict:
        """Raw + grams with an outlier-filtered noise figure."""
        raws = []
        for _ in range(max(5, samples)):
            try:
                r = self.read_raw()
                if r is not None and abs(r) < 8388000:
                    raws.append(r)
            except Exception:
                pass
            time.sleep(0.004)

        if not raws:
            raws = [self._last_valid_raw]

        raws.sort()
        trim = len(raws) // 5
        trimmed = raws[trim:-trim] if trim > 0 and len(raws) > 2 * trim else raws
        median = statistics.median(trimmed)
        return {
            "raw": round(median, 1),
            "grams": self.raw_to_grams(median),
            "raw_stdev": round(statistics.pstdev(trimmed) if len(trimmed) > 1 else 0.0, 2),
            "raw_min": round(min(trimmed), 1),
            "raw_max": round(max(trimmed), 1),
            "samples": len(trimmed),
        }

    def tare(self, samples: int = 20) -> float:
        """Reset the zero point to the current load. Returns the new offset."""
        readings = []
        for _ in range(max(5, samples)):
            try:
                r = self.read_raw()
                if r is not None and abs(r) < 8388000:
                    readings.append(r)
            except Exception:
                pass
            time.sleep(0.005)

        if readings:
            readings.sort()
            trim = len(readings) // 5
            trimmed = readings[trim:-trim] if trim > 0 and len(readings) > 2 * trim else readings
            self._offset = statistics.median(trimmed)
        else:
            self._offset = self.read_raw()

        self._ema_grams = 0.0
        return self._offset

    # -- health ------------------------------------------------------------
    def health(self) -> dict:
        return {
            "kind": self.kind,
            "real_hardware": self.real_hardware,
            "online": True,
            "detail": "",
            **self.calibration,
        }

    def close(self) -> None:
        pass


class SimulatedScale(BaseScale):
    """Software scale used on a laptop / when no HX711 is present."""

    kind = "simulated"
    real_hardware = False

    def __init__(self, detail: str = "") -> None:
        super().__init__()
        self._reference_unit = 1.0
        self._offset = 0.0
        self._calibration_source = "simulated"
        self._grams = 0.0
        self._detail = detail
        self._lock = threading.Lock()

    def read_raw(self) -> float:
        # raw == grams in the simulator (reference_unit=1, offset=0)
        with self._lock:
            return self._grams

    # calibration is meaningless here; keep the identity transform
    def set_calibration(self, reference_unit: float, offset: float, source: str = "runtime") -> None:
        return

    # --- hooks the app uses to mimic putting items in the basket ---
    def sim_add_weight(self, grams: float) -> None:
        with self._lock:
            self._grams = max(0.0, self._grams + grams)

    def sim_set_weight(self, grams: float) -> None:
        with self._lock:
            self._grams = max(0.0, grams)
            self._offset = 0.0

    def tare(self, samples: int = 15) -> float:
        with self._lock:
            self._grams = 0.0
            self._offset = 0.0
            return 0.0

    def raw_to_grams(self, raw: float) -> float:
        with self._lock:
            return round(max(0.0, raw - self._offset), 1)

    def get_grams(self, samples: int = 8) -> float:
        with self._lock:
            return round(max(0.0, self._grams - self._offset), 1)

    def health(self) -> dict:
        return {**super().health(), "detail": self._detail or "software scale"}


class HX711Scale(BaseScale):
    """Real HX711 driver built on lgpio (Raspberry Pi 5 / RP1 compatible).

    The legacy RPi.GPIO-based HX711 libraries do not work on the Pi 5, so the
    24-bit protocol is bit-banged here directly.
    """

    kind = "hx711"
    real_hardware = True

    #: extra SCK pulses after the 24 data bits select the input & gain
    _GAIN_PULSES = {128: 1, 64: 3, 32: 2}

    def __init__(self, dt_pin: int | None = None, sck_pin: int | None = None,
                 gain: int | None = None) -> None:
        super().__init__()
        import lgpio  # imported lazily so laptops don't need it

        self._lgpio = lgpio
        self._dt = settings.hx711_dt_pin if dt_pin is None else dt_pin
        self._sck = settings.hx711_sck_pin if sck_pin is None else sck_pin
        gain = settings.hx711_gain if gain is None else gain
        self._gain_pulses = self._GAIN_PULSES.get(gain, 1)
        self._gain = gain

        try:
            self._chip = lgpio.gpiochip_open(0)
        except Exception as exc:
            raise RuntimeError(f"cannot open gpiochip0: {exc}") from exc
        try:
            lgpio.gpio_claim_input(self._chip, self._dt)
            lgpio.gpio_claim_output(self._chip, self._sck, 0)
        except Exception as exc:
            lgpio.gpiochip_close(self._chip)
            raise RuntimeError(
                f"GPIO {self._dt}/{self._sck} unavailable ({exc}). "
                "Another process (the app or pi_agent) probably owns the pins."
            ) from exc

        self._lock = threading.Lock()
        # Discard the first N conversions: after power-on / reset the HX711 defaults
        # to Channel A; it takes several cycles for the internal Σ-Δ filter to switch
        # to Channel B (gain=32) and settle. Reading too early captures wild transients.
        self._warmup(cycles=15)

    # -- protocol ----------------------------------------------------------
    def _pulse(self) -> None:
        """One SCK high→low pulse. lgpio on Pi 5 RP1 is fast enough without sleeps."""
        self._lgpio.gpio_write(self._chip, self._sck, 1)
        self._lgpio.gpio_write(self._chip, self._sck, 0)

    def _wait_ready(self, timeout_s: float = 1.5) -> bool:
        """Wait for HX711 DOUT to go low, indicating a new conversion is available."""
        start = time.time()
        while self._lgpio.gpio_read(self._chip, self._dt) == 1:
            if time.time() - start > timeout_s:
                return False
            time.sleep(0.002)
        return True

    def _read_once(self) -> int | None:
        lg = self._lgpio
        if not self._wait_ready():
            return None

        value = 0
        for _ in range(24):
            lg.gpio_write(self._chip, self._sck, 1)
            bit = lg.gpio_read(self._chip, self._dt)
            value = (value << 1) | bit
            lg.gpio_write(self._chip, self._sck, 0)

        # extra pulses select channel / gain for the *next* conversion
        for _ in range(self._gain_pulses):
            self._pulse()

        # 24-bit two's complement -> signed int
        if value & 0x800000:
            value -= 1 << 24

        # Glitch check: reject bit-shift corruptions and rail saturations
        if abs(value) >= 8388000 or value == 1048575 or value == -1:
            # Record *what* was rejected so health() can tell an open bridge
            # (stuck at ±8,388,607) apart from an occasional noise glitch.
            self._last_reject_raw = float(value)
            return None

        return value

    def _warmup(self, cycles: int = 15) -> None:
        """Discard initial channel-switch and power-up settling conversions.

        Do NOT hold self._lock here — this is called from __init__ before any
        public API and also from reset() which already holds the lock internally.
        """
        for _ in range(cycles):
            try:
                val = self._read_once()
                if val is not None:
                    self._last_valid_raw = float(val)
            except Exception:
                pass

    def read_raw(self) -> float:
        with self._lock:
            for _ in range(5):
                val = self._read_once()
                if val is not None:
                    self._last_valid_raw = float(val)
                    self._consecutive_rejects = 0
                    return self._last_valid_raw
                time.sleep(0.005)
            # Every attempt was rejected: keep returning the last good value so
            # the filters do not jump, but remember that this reading is stale.
            self._consecutive_rejects += 1
            return self._last_valid_raw

    def reset(self) -> None:
        """Power the chip down and back up to clear a wedged conversion."""
        with self._lock:
            lg = self._lgpio
            lg.gpio_write(self._chip, self._sck, 1)
            time.sleep(0.001)   # >60 µs powers the chip down
            lg.gpio_write(self._chip, self._sck, 0)
            time.sleep(0.1)     # wait for internal oscillator to restart
            # Re-run warmup WITHOUT re-acquiring the lock (we hold it already)
            for _ in range(12):
                try:
                    val = self._read_once()
                    if val is not None:
                        self._last_valid_raw = float(val)
                except Exception:
                    pass

    # -- health ------------------------------------------------------------
    def health(self) -> dict:
        info = {**super().health(), "dt_pin": self._dt, "sck_pin": self._sck,
                "gain": self._gain}
        try:
            # Probe the ADC directly: read_stats()/read_raw() substitute the last
            # good value for a rejected conversion, so asking them whether the
            # bridge is alive would always answer "yes".
            probe_valid, probe_reject = 0, 0
            with self._lock:
                for _ in range(8):
                    if self._read_once() is None:
                        probe_reject += 1
                    else:
                        probe_valid += 1
                    time.sleep(0.004)

            if probe_valid == 0:
                bad = self._last_reject_raw
                info["online"] = False
                if bad is not None and abs(bad) >= 8388000:
                    info["detail"] = (
                        f"Load-cell bridge open/saturated ({bad:,.0f} counts, "
                        f"channel {'A' if self._gain in (128, 64) else 'B'} @ gain "
                        f"{self._gain}) — check the E+/E-/A+/A- bridge wiring"
                    )
                else:
                    info["detail"] = (
                        f"HX711 returning no usable conversions (last rejected value "
                        f"{bad if bad is None else format(bad, ',.0f')})"
                    )
                info["raw"] = bad
                info["rejected"] = f"{probe_reject}/8"
                return info

            stats = self.read_stats(samples=5)
            info["online"] = True
            info["raw"] = stats["raw"]
            info["raw_stdev"] = stats["raw_stdev"]
            if probe_reject:
                info["detail"] = f"{probe_reject}/8 conversions rejected (noisy)"
        except Exception as exc:
            info["online"] = False
            info["detail"] = str(exc)
        return info

    def close(self) -> None:
        try:
            self._lgpio.gpiochip_close(self._chip)
        except Exception:
            pass


class RemoteScale(BaseScale):
    """Reads raw counts pushed up by the Pi agent (cloud deployment).

    `read_raw()` returns the newest sample the agent sent. If the agent has not
    reported within `DEVICE_STALE_AFTER_S` the scale is "offline": either raise
    (so the caller can surface it) or fall back to a software scale, depending
    on ALLOW_SIM_FALLBACK.
    """

    kind = "remote"
    real_hardware = True

    def __init__(self) -> None:
        super().__init__()
        from app.devices import bridge

        self._bridge = bridge
        # only used when the device is offline and fallback is allowed
        self._fallback = SimulatedScale(detail="device offline fallback")

    @property
    def online(self) -> bool:
        return self._bridge.is_online()

    @property
    def fallback(self) -> SimulatedScale:
        return self._fallback

    @property
    def using_fallback(self) -> bool:
        return not self.online and settings.allow_sim_fallback

    def read_raw(self) -> float:
        raw = self._bridge.latest_raw()
        if raw is not None:
            return raw
        if settings.allow_sim_fallback:
            # software scale keeps grams == raw, and our offset/reference_unit
            # would distort that, so hand back a value that maps to its grams
            return self._fallback._grams * (self._reference_unit or 1.0) + self._offset
        raise RuntimeError(
            "Trolley device offline: no load-cell reading received. "
            "Check that pi_agent.py is running on the Pi."
        )

    def get_grams(self, samples: int = 8) -> float:
        # A remote reading is already median-filtered on the Pi, and repeated
        # reads return the same cached sample, so don't burn 8 x 5 ms here.
        return self.raw_to_grams(self.read_raw())

    def read_stats(self, samples: int = 15) -> dict:
        raw = self.read_raw()
        status = self._bridge.status()
        return {
            "raw": round(raw, 1),
            "grams": self.raw_to_grams(raw),
            "raw_stdev": 0.0,
            "raw_min": round(raw, 1),
            "raw_max": round(raw, 1),
            "samples": 1,
            "reading_age_s": status.get("reading_age_s"),
        }

    def tare(self, samples: int = 15) -> float:
        """Zero the basket using the agent's current reading."""
        if not self.online and settings.allow_sim_fallback:
            return self._fallback.tare(samples)
        self._offset = self.read_raw()
        return self._offset

    def health(self) -> dict:
        status = self._bridge.status()
        online = self.online
        return {
            **super().health(),
            "online": online,
            "detail": (
                "" if online
                else ("device offline — using software fallback"
                      if settings.allow_sim_fallback
                      else "device offline — weight checks unavailable")
            ),
            "device": status,
            "using_fallback": self.using_fallback,
        }


# --- module-level singleton ------------------------------------------------
_scale: BaseScale | None = None
_init_error: str = ""


def _build_scale() -> BaseScale:
    global _init_error
    mode = settings.mode

    if mode == "cloud":
        return RemoteScale()

    if mode == "simulation":
        return SimulatedScale(detail="SIMULATION mode")

    # local: drive the HX711 on this machine
    try:
        return HX711Scale()
    except Exception as exc:
        _init_error = str(exc)
        # Loud, because a silent fallback here would quietly disable the
        # anti-theft weight check on a real trolley.
        print("=" * 72)
        print(f"[scale] HX711 UNAVAILABLE: {exc}")
        print("[scale] Falling back to the SOFTWARE scale. Weight verification")
        print("[scale] is NOT real until this is fixed. Run:")
        print("[scale]     python calibrate_scale.py diagnose")
        print("=" * 72)
        return SimulatedScale(detail=f"HX711 unavailable: {exc}")


def get_scale() -> BaseScale:
    global _scale
    if _scale is None:
        _scale = _build_scale()
        _apply_stored_calibration(_scale)
    return _scale


def _apply_stored_calibration(scale: BaseScale) -> None:
    """Load calibration saved by the settings UI / calibrate_scale.py."""
    if isinstance(scale, SimulatedScale):
        return
    try:
        from app.services import settings_store

        cal = settings_store.read_scale_calibration()
        scale.set_calibration(cal["reference_unit"], cal["offset"], cal["source"])
    except Exception as exc:  # DB not ready yet -> keep .env values
        print(f"[scale] using .env calibration ({exc})")


def reload_calibration() -> dict:
    """Re-read calibration from the store onto the live scale."""
    scale = get_scale()
    _apply_stored_calibration(scale)
    return scale.calibration


def scale_health() -> dict:
    scale = get_scale()
    info = scale.health()
    info["mode"] = settings.mode
    if _init_error:
        info["init_error"] = _init_error
    return info


def reset_scale() -> None:
    """Drop the singleton (used by tests and by mode changes)."""
    global _scale
    if _scale is not None:
        _scale.close()
    _scale = None
