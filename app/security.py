"""Auth for the staff settings menu and the Pi device bridge.

Two very different things are protected here:

* **Staff / admin menu** — product registration, lookup and scale calibration.
  Guarded by a PIN (ADMIN_PIN). On success the browser gets an HMAC-signed,
  HttpOnly cookie carrying an expiry; nothing sensitive is stored client-side
  and the cookie cannot be forged without SECRET_KEY.

* **Pi agent** — presents DEVICE_TOKEN as a bearer token (or `?token=`
  for the WebSocket, since browsers/agents can't always set WS headers).
  Compared with `hmac.compare_digest` to avoid timing leaks.

This is deliberately dependency-free (stdlib hmac/hashlib/base64) so the
prototype has no extra install. It is PIN-grade security: good enough for a
staff terminal in a store, not a replacement for real user accounts. Anything
public-facing should sit behind HTTPS — see DEPLOY.md.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException, Request, status

from app.config import settings

ADMIN_COOKIE = "admin_session"


# ---------------------------------------------------------------------------
# Signed token helpers
# ---------------------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(payload: str) -> str:
    mac = hmac.new(
        settings.signing_key.encode(), payload.encode(), hashlib.sha256
    ).digest()
    return _b64e(mac)


def issue_admin_token(hours: int | None = None) -> str:
    """Return `expiry.signature`; the expiry is readable but not forgeable."""
    ttl = (hours if hours is not None else settings.admin_session_hours) * 3600
    expires = int(time.time()) + ttl
    payload = str(expires)
    return f"{payload}.{_sign(payload)}"


def verify_admin_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Admin PIN
# ---------------------------------------------------------------------------
def admin_pin_is_default() -> bool:
    return settings.admin_pin == "1234"


def check_admin_pin(pin: str) -> bool:
    expected = (settings.admin_pin or "").strip()
    if not expected:
        # An empty ADMIN_PIN would otherwise let anyone in.
        return False
    return hmac.compare_digest((pin or "").strip(), expected)


def is_admin(request: Request) -> bool:
    return verify_admin_token(request.cookies.get(ADMIN_COOKIE))


def require_admin(request: Request) -> bool:
    """FastAPI dependency: 401 unless a valid admin cookie is present."""
    if not is_admin(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin_required",
        )
    return True


def set_admin_cookie(response, token: str) -> None:
    """Attach the admin session cookie.

    `secure` is left off so the cookie also works on plain-HTTP LAN kiosks
    (http://<pi-ip>:8000). Behind HTTPS the browser upgrades it anyway; set
    SECURE_COOKIES=true style hardening when you put this on a public domain.
    """
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.admin_session_hours * 3600,
        path="/",
    )


def clear_admin_cookie(response) -> None:
    response.delete_cookie(ADMIN_COOKIE, path="/")


# ---------------------------------------------------------------------------
# Device token (Pi agent)
# ---------------------------------------------------------------------------
def device_token_configured() -> bool:
    return bool((settings.device_token or "").strip())


def check_device_token(presented: str | None) -> bool:
    expected = (settings.device_token or "").strip()
    if not expected:
        # No token configured. Fine on a private LAN / laptop, but refuse to
        # pretend it is safe in the cloud.
        return not settings.is_cloud
    return hmac.compare_digest((presented or "").strip(), expected)


def extract_device_token(request: Request) -> str | None:
    """Read the token from Authorization: Bearer, X-Device-Token or ?token=."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.headers.get("x-device-token")
    if header:
        return header.strip()
    return request.query_params.get("token")


def require_device(request: Request) -> str:
    """FastAPI dependency for device endpoints. Returns the device id."""
    if not check_device_token(extract_device_token(request)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="bad_device_token"
        )
    return request.headers.get("x-device-id") or settings.device_id


def startup_warnings() -> list[str]:
    """Configuration problems worth shouting about at boot."""
    warnings: list[str] = []
    if settings.is_cloud and not settings.secret_key:
        warnings.append(
            "SECRET_KEY is not set: admin sessions reset on every restart."
        )
    if settings.is_cloud and not device_token_configured():
        warnings.append(
            "DEVICE_TOKEN is not set: the cloud will REJECT the Pi agent. "
            "Generate one with `openssl rand -hex 32` and set it on both sides."
        )
    if admin_pin_is_default():
        warnings.append(
            "ADMIN_PIN is still the default 1234: change it before the store demo."
        )
    return warnings
