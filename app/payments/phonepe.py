"""PhonePe Payment Gateway (Standard Checkout) provider.

Ready for the test/sandbox credentials you will provide. It implements the two
calls the trolley needs:

  * pay     -> POST /pg/v1/pay        (creates the transaction, returns a
               redirect/QR URL the screen shows)
  * status  -> GET  /pg/v1/status/{mid}/{txnId}  (polled until 'paid')

PhonePe signs every request with X-VERIFY:
    sha256(base64Payload + endpoint + saltKey) + "###" + saltIndex

While PAYMENT_PROVIDER=mock this file is never called; switch to
PAYMENT_PROVIDER=phonepe and fill PHONEPE_* in .env to go live on sandbox.
"""
from __future__ import annotations

import base64
import hashlib
import json

import requests

from app.config import settings
from app.payments.base import PaymentIntent, PaymentProvider, make_qr_data_uri

_HOSTS = {
    "sandbox": "https://api-preprod.phonepe.com/apis/pg-sandbox",
    "production": "https://api.phonepe.com/apis/hermes",
}


class PhonePeProvider(PaymentProvider):
    name = "phonepe"

    def __init__(self) -> None:
        self.mid = settings.phonepe_merchant_id
        self.salt_key = settings.phonepe_salt_key
        self.salt_index = settings.phonepe_salt_index
        self.base = _HOSTS.get(settings.phonepe_env, _HOSTS["sandbox"])
        self.callback = settings.phonepe_callback_url

    def _sign(self, b64_payload: str, endpoint: str) -> str:
        digest = hashlib.sha256(
            (b64_payload + endpoint + self.salt_key).encode()
        ).hexdigest()
        return f"{digest}###{self.salt_index}"

    def create_payment(self, order_ref: str, amount: float, note: str = "") -> PaymentIntent:
        endpoint = "/pg/v1/pay"
        payload = {
            "merchantId": self.mid,
            "merchantTransactionId": order_ref,
            "amount": int(round(amount * 100)),  # paise
            "merchantUserId": "trolley",
            "redirectUrl": self.callback,
            "redirectMode": "POST",
            "callbackUrl": self.callback,
            "paymentInstrument": {"type": "PAY_PAGE"},
        }
        b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        headers = {
            "Content-Type": "application/json",
            "X-VERIFY": self._sign(b64, endpoint),
        }
        intent = PaymentIntent(
            order_ref=order_ref, amount=amount, provider=self.name,
            payment_ref=order_ref, status="pending",
        )
        try:
            resp = requests.post(
                self.base + endpoint, json={"request": b64}, headers=headers, timeout=15
            )
            data = resp.json()
            url = (
                data.get("data", {})
                .get("instrumentResponse", {})
                .get("redirectInfo", {})
                .get("url", "")
            )
            if url:
                # show the pay-page URL as a QR the shopper opens on their phone
                intent.qr_payload = url
                intent.qr_data_uri = make_qr_data_uri(url)
            intent.meta = {"raw": data}
        except Exception as exc:
            intent.status = "failed"
            intent.meta = {"error": str(exc)}
        return intent

    def check_status(self, intent: PaymentIntent) -> str:
        endpoint = f"/pg/v1/status/{self.mid}/{intent.order_ref}"
        digest = hashlib.sha256(
            (endpoint + self.salt_key).encode()
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-VERIFY": f"{digest}###{self.salt_index}",
            "X-MERCHANT-ID": self.mid,
        }
        try:
            resp = requests.get(self.base + endpoint, headers=headers, timeout=15)
            data = resp.json()
            code = data.get("code", "")
            if code == "PAYMENT_SUCCESS":
                return "paid"
            if code in ("PAYMENT_ERROR", "PAYMENT_DECLINED", "TIMED_OUT"):
                return "failed"
            return "pending"
        except Exception:
            return "pending"
