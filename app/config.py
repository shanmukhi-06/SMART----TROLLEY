"""Central configuration, loaded from environment / .env file.

Deployment topologies
---------------------
The same codebase runs in three shapes, selected by HARDWARE_MODE:

  simulation  laptop demo, no hardware at all (software scale)
  local       full app + hardware on the same Raspberry Pi (the old way)
  cloud       app runs on AWS; a thin agent on the Pi (pi_agent.py) streams
              load-cell readings up and receives tare/calibrate commands

HARDWARE_MODE=auto keeps backwards compatibility with the original
SIMULATION flag: SIMULATION=true -> simulation, SIMULATION=false -> local.
"""
from __future__ import annotations

import secrets
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- Runtime ---------------------------------------------------------
    # Legacy master switch, still honoured when hardware_mode == "auto".
    simulation: bool = True
    # auto | simulation | local | cloud
    hardware_mode: str = "auto"
    store_name: str = "Smart Mart"
    store_tagline: str = "Scan. Pack. Walk out."

    # ---- Server ----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    # Signs the admin cookie. Generate one for production: `openssl rand -hex 32`
    secret_key: str = ""
    # Comma separated list, or "*" for any. Only needed if the kiosk page and
    # the API live on different origins.
    cors_origins: str = ""

    # ---- Database --------------------------------------------------------
    database_url: str = "sqlite:///./smart_trolley.db"

    # ---- Cloud <-> Pi device bridge --------------------------------------
    # Shared secret the Pi agent presents to the cloud. MUST be set when
    # HARDWARE_MODE=cloud and the app is reachable from the internet.
    device_token: str = ""
    device_id: str = "trolley-1"
    # Agent side only: where the cloud app lives, e.g. https://trolley.example.com
    cloud_url: str = "http://localhost:8000"
    # A weight reading older than this is treated as "device offline".
    device_stale_after_s: float = 6.0
    # If the trolley device is offline, fall back to the software scale so the
    # cloud app is still demoable. Anti-theft is degraded, so the UI says so.
    allow_sim_fallback: bool = True

    # ---- HX711 load cell (4 x 50 kg cells -> one full bridge) -------------
    hx711_dt_pin: int = 5
    hx711_sck_pin: int = 6
    # 32 = Channel B, 128 = Channel A (Gain 128), 64 = Channel A (Gain 64)
    hx711_gain: int = 32
    scale_reference_unit: float = 1.0
    scale_offset: float = 0.0
    weight_tolerance_g: float = 25.0
    # Total rated capacity, used for sanity warnings during calibration.
    scale_capacity_g: float = 200_000.0
    # How often the Pi agent pushes a reading (seconds)
    scale_stream_interval_s: float = 0.25

    # ---- Feedback --------------------------------------------------------
    # No buzzer or LEDs are wired on this trolley: scan feedback is visual
    # (colour flash / toast) plus an optional beep played by the browser.
    ui_beep: bool = True

    # ---- Camera ----------------------------------------------------------
    camera_enabled: bool = True
    camera_index: int = 0

    # ---- Admin / settings menu -------------------------------------------
    # PIN that unlocks the in-UI settings menu (product registration etc.).
    admin_pin: str = "1234"
    admin_session_hours: int = 8

    # ---- Kiosk display ---------------------------------------------------
    # Ask the browser to go fullscreen automatically on the shop screen.
    kiosk_mode: bool = False
    # URL the Pi's browser opens. Defaults to cloud_url when blank.
    kiosk_url: str = ""

    # ---- Payment ---------------------------------------------------------
    payment_provider: str = "mock"
    mock_upi_vpa: str = "smartmart@upi"
    phonepe_merchant_id: str = ""
    phonepe_salt_key: str = ""
    phonepe_salt_index: int = 1
    phonepe_env: str = "sandbox"
    phonepe_callback_url: str = "http://localhost:8000/api/payments/phonepe/callback"

    # ---- Email -----------------------------------------------------------
    email_enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "Smart Mart <noreply@smartmart.local>"

    # ---- SMS -------------------------------------------------------------
    sms_enabled: bool = False

    # ------------------------------------------------------------------
    # Thermal printer (HAL: app/hardware/printer.py)
    # One of: auto | simulation | usb | bluetooth | network | remote
    # ------------------------------------------------------------------
    printer_transport: str = "auto"
    printer_paper_width_chars: int = 48
    printer_charset: str = "cp437"
    # auto-print every paid receipt on the connected printer
    printer_auto_print_receipts: bool = False

    # USB transport
    printer_usb_device: str = ""        # e.g. /dev/usb/lp0, COM3
    printer_usb_driver: str = "auto"    # auto | usb | serial | cups
    printer_usb_vendor: str = ""        # hex VID, e.g. "04b8"
    printer_usb_product: str = ""       # hex PID, e.g. "0202"

    # Bluetooth transport
    printer_bt_address: str = ""       # MAC for RFCOMM/BLE
    printer_bt_name: str = ""          # substring of advertised name
    printer_bt_channel: int = 1        # RFCOMM channel (almost always 1)
    printer_bt_transport: str = "rfcomm"  # rfcomm | ble

    # Network transport
    printer_net_host: str = ""
    printer_net_port: int = 9100

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def mode(self) -> str:
        """Resolved hardware mode: 'simulation' | 'local' | 'cloud'."""
        m = (self.hardware_mode or "auto").strip().lower()
        if m in {"simulation", "sim"}:
            return "simulation"
        if m in {"local", "pi", "onboard"}:
            return "local"
        if m in {"cloud", "remote", "aws"}:
            return "cloud"
        # auto -> derive from the legacy SIMULATION flag
        return "simulation" if self.simulation else "local"

    @property
    def is_cloud(self) -> bool:
        return self.mode == "cloud"

    @property
    def is_simulated(self) -> bool:
        return self.mode == "simulation"

    @property
    def uses_local_gpio(self) -> bool:
        """True when this process should drive the GPIO pins directly."""
        return self.mode == "local"

    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "").strip()
        if not raw:
            return []
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def effective_kiosk_url(self) -> str:
        return (self.kiosk_url or self.cloud_url or "http://localhost:8000").rstrip("/")

    @property
    def signing_key(self) -> str:
        """Key used for the admin cookie. Falls back to an ephemeral one."""
        if self.secret_key:
            return self.secret_key
        # No SECRET_KEY set: admin sessions simply do not survive a restart.
        return _EPHEMERAL_KEY


# Generated once per process so a missing SECRET_KEY degrades gracefully
# (admins have to re-enter the PIN after a restart) instead of being insecure.
_EPHEMERAL_KEY = secrets.token_hex(32)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
