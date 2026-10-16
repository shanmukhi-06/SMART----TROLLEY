"""Mock / simulation payment provider.

Generates a real, scannable UPI QR (upi://pay?...) so the screen looks exactly
like production, then auto-confirms the payment a few seconds later to mimic the
shopper completing it in their UPI app. A manual confirm endpoint also exists
for demos. No money moves.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from app.config import settings
from app.payments.base import PaymentIntent, PaymentProvider, make_qr_data_uri

# auto-confirm this many seconds after the QR is shown (simulates paying)
AUTO_CONFIRM_AFTER_S = 6.0


class MockPaymentProvider(PaymentProvider):
    name = "mock"

    def __init__(self) -> None:
        self._created_at: dict[str, float] = {}
        self._forced_paid: set[str] = set()

    def create_payment(self, order_ref: str, amount: float, note: str = "") -> PaymentIntent:
        vpa = settings.mock_upi_vpa
        payee = quote(settings.store_name)
        tn = quote(note or f"Order {order_ref}")
        payload = (
            f"upi://pay?pa={vpa}&pn={payee}"
            f"&am={amount:.2f}&cu=INR&tn={tn}&tr={order_ref}"
        )
        self._created_at[order_ref] = time.time()
        return PaymentIntent(
            order_ref=order_ref,
            amount=amount,
            provider=self.name,
            payment_ref=f"MOCK-{order_ref}",
            qr_payload=payload,
            qr_data_uri=make_qr_data_uri(payload),
            status="pending",
            meta={"simulated": True},
        )

    def mark_paid(self, order_ref: str) -> None:
        self._forced_paid.add(order_ref)

    def check_status(self, intent: PaymentIntent) -> str:
        if intent.order_ref in self._forced_paid:
            return "paid"
        started = self._created_at.get(intent.order_ref)
        if started and (time.time() - started) >= AUTO_CONFIRM_AFTER_S:
            return "paid"
        return "pending"
