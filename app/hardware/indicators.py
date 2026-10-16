"""Buzzer + LED feedback (scan OK / weight mismatch).

Real driver uses lgpio on the Pi 5; simulator just logs. Non-blocking: every
signal runs on a short-lived thread so the UI/scan loop is never delayed.
"""
from __future__ import annotations

import threading
import time

from app.config import settings


class BaseIndicators:
    def _pulse(self, on) -> None:
        raise NotImplementedError

    def signal_ok(self) -> None:
        threading.Thread(target=self._ok, daemon=True).start()

    def signal_alert(self) -> None:
        threading.Thread(target=self._alert, daemon=True).start()

    def _ok(self) -> None:
        pass

    def _alert(self) -> None:
        pass

    def close(self) -> None:
        pass


class SimulatedIndicators(BaseIndicators):
    def _ok(self) -> None:
        print("[buzzer] beep (scan OK)  [led] green")

    def _alert(self) -> None:
        print("[buzzer] BEEP-BEEP (weight mismatch!)  [led] red")


class GpioIndicators(BaseIndicators):
    def __init__(self) -> None:
        import lgpio

        self._lgpio = lgpio
        self._chip = lgpio.gpiochip_open(0)
        self._buzzer = settings.buzzer_pin
        self._led_ok = settings.led_ok_pin
        self._led_alert = settings.led_alert_pin
        for pin in (self._buzzer, self._led_ok, self._led_alert):
            lgpio.gpio_claim_output(self._chip, pin, 0)

    def _set(self, pin: int, value: int) -> None:
        self._lgpio.gpio_write(self._chip, pin, value)

    def _ok(self) -> None:
        self._set(self._led_ok, 1)
        self._set(self._buzzer, 1)
        time.sleep(0.08)
        self._set(self._buzzer, 0)
        time.sleep(0.6)
        self._set(self._led_ok, 0)

    def _alert(self) -> None:
        for _ in range(3):
            self._set(self._led_alert, 1)
            self._set(self._buzzer, 1)
            time.sleep(0.12)
            self._set(self._buzzer, 0)
            self._set(self._led_alert, 0)
            time.sleep(0.12)

    def close(self) -> None:
        try:
            for pin in (self._buzzer, self._led_ok, self._led_alert):
                self._lgpio.gpio_write(self._chip, pin, 0)
            self._lgpio.gpiochip_close(self._chip)
        except Exception:
            pass


_indicators: BaseIndicators | None = None


def get_indicators() -> BaseIndicators:
    global _indicators
    if _indicators is not None:
        return _indicators
    if settings.simulation:
        _indicators = SimulatedIndicators()
        return _indicators
    try:
        _indicators = GpioIndicators()
    except Exception as exc:
        print(f"[indicators] Falling back to simulator: {exc}")
        _indicators = SimulatedIndicators()
    return _indicators
