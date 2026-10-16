"""Return the configured payment provider (singleton)."""
from __future__ import annotations

from app.config import settings
from app.payments.base import PaymentProvider
from app.payments.mock import MockPaymentProvider
from app.payments.phonepe import PhonePeProvider

_provider: PaymentProvider | None = None


def get_provider() -> PaymentProvider:
    global _provider
    if _provider is not None:
        return _provider
    if settings.payment_provider == "phonepe" and settings.phonepe_merchant_id:
        _provider = PhonePeProvider()
    else:
        # default / fallback: mock (also used when phonepe creds are missing)
        _provider = MockPaymentProvider()
    return _provider
