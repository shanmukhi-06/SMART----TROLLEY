"""Persisted runtime settings (key/value) backed by the database.

Why this exists
---------------
Load-cell calibration used to live only in `.env`, which means calibrating
required editing a file and restarting. Once the app runs in the cloud that is
a redeploy. Calibration captured from the settings UI (or pushed by
`calibrate_scale.py`) is stored here instead, and `.env` becomes the fallback
default.

Values are stored as strings and cached in-process; `load_scale_calibration()`
is called at startup and after every calibration change.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import AppSetting

KEY_SCALE_REFERENCE_UNIT = "scale.reference_unit"
KEY_SCALE_OFFSET = "scale.offset"
KEY_SCALE_CALIBRATED_AT = "scale.calibrated_at"
KEY_SCALE_CALIBRATION_NOTE = "scale.calibration_note"

KEY_PRINTER_TRANSPORT = "printer.transport"
KEY_PRINTER_DEVICE = "printer.device"
KEY_PRINTER_DRIVER = "printer.driver"
KEY_PRINTER_AUTO_PRINT = "printer.auto_print"


def get(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row else default


def set_value(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()


def get_float(db: Session, key: str, default: float) -> float:
    raw = get(db, key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def all_settings(db: Session) -> dict[str, str]:
    return {row.key: row.value for row in db.scalars(select(AppSetting))}


# ---------------------------------------------------------------------------
# Scale calibration helpers
# ---------------------------------------------------------------------------
def read_scale_calibration() -> dict:
    """Return the effective calibration, DB first then .env defaults."""
    db = SessionLocal()
    try:
        return {
            "reference_unit": get_float(
                db, KEY_SCALE_REFERENCE_UNIT, settings.scale_reference_unit or 1.0
            ),
            "offset": get_float(db, KEY_SCALE_OFFSET, settings.scale_offset),
            "calibrated_at": get(db, KEY_SCALE_CALIBRATED_AT, "") or "",
            "note": get(db, KEY_SCALE_CALIBRATION_NOTE, "") or "",
            "source": "database" if get(db, KEY_SCALE_REFERENCE_UNIT) else "env",
        }
    finally:
        db.close()


def write_scale_calibration(
    reference_unit: float, offset: float, note: str = ""
) -> dict:
    """Persist calibration and return the stored values."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = SessionLocal()
    try:
        set_value(db, KEY_SCALE_REFERENCE_UNIT, f"{reference_unit:.10g}")
        set_value(db, KEY_SCALE_OFFSET, f"{offset:.10g}")
        set_value(db, KEY_SCALE_CALIBRATED_AT, stamp)
        set_value(db, KEY_SCALE_CALIBRATION_NOTE, note)
    finally:
        db.close()
    return {
        "reference_unit": reference_unit,
        "offset": offset,
        "calibrated_at": stamp,
        "note": note,
        "source": "database",
    }


# ---------------------------------------------------------------------------
# Printer configuration helpers
# ---------------------------------------------------------------------------
def read_printer_settings() -> dict:
    """Return effective printer settings, DB first then .env defaults."""
    db = SessionLocal()
    try:
        raw_auto = get(db, KEY_PRINTER_AUTO_PRINT)
        auto_print = (
            raw_auto.lower() in ("true", "1", "yes")
            if raw_auto is not None
            else getattr(settings, "printer_auto_print_receipts", False)
        )
        return {
            "transport": get(db, KEY_PRINTER_TRANSPORT, getattr(settings, "printer_transport", "auto")),
            "device": get(db, KEY_PRINTER_DEVICE, getattr(settings, "printer_usb_device", "")),
            "driver": get(db, KEY_PRINTER_DRIVER, getattr(settings, "printer_usb_driver", "auto")),
            "auto_print": auto_print,
            "source": "database" if get(db, KEY_PRINTER_TRANSPORT) else "env",
        }
    finally:
        db.close()


def write_printer_settings(
    transport: str, device: str = "", driver: str = "", auto_print: bool | None = None
) -> dict:
    """Persist printer transport & device preferences into the database."""
    db = SessionLocal()
    try:
        if transport:
            set_value(db, KEY_PRINTER_TRANSPORT, transport)
        if device is not None:
            set_value(db, KEY_PRINTER_DEVICE, device)
        if driver is not None:
            set_value(db, KEY_PRINTER_DRIVER, driver)
        if auto_print is not None:
            set_value(db, KEY_PRINTER_AUTO_PRINT, "true" if auto_print else "false")
    finally:
        db.close()
    return read_printer_settings()

