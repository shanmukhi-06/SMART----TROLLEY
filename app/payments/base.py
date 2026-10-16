"""Payment provider contract + shared helpers.

Any provider (mock, PhonePe, later Razorpay/Cashfree) implements this small
interface, so the checkout flow never depends on a specific gateway.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field

import qrcode


@dataclass
class PaymentIntent:
    order_ref: str
    amount: float                     # in INR (rupees)
    provider: str
    payment_ref: str = ""             # gateway transaction id / our ref
    qr_data_uri: str = ""             # PNG data URI to render on screen
    qr_payload: str = ""              # the raw string encoded in the QR
    status: str = "pending"           # pending | paid | failed
    meta: dict = field(default_factory=dict)


def make_qr_data_uri(payload: str) -> str:
    """Render `payload` as a QR PNG and return it as a data: URI for <img>."""
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0b5cff", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


class PaymentProvider:
    name = "base"

    def create_payment(self, order_ref: str, amount: float, note: str = "") -> PaymentIntent:
        raise NotImplementedError

    def check_status(self, intent: PaymentIntent) -> str:
        """Return current status: pending | paid | failed."""
        raise NotImplementedError
