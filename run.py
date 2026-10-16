"""Entry point for the Smart Shopping Trolley app.

Usage:
    python run.py

Reads configuration from .env (copy from .env.example). On a laptop keep
SIMULATION=true; on the Raspberry Pi 5 set SIMULATION=false after wiring the
HX711 and installing requirements-hardware.txt.
"""
from __future__ import annotations

import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
