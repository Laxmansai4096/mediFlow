"""Mock payment gateway (free stand-in for Razorpay / Stripe / PayU).

The real integration pattern is preserved: the gateway confirms payments with
an HMAC-signed callback, and MediFlow only treats a payment as successful
after verifying that signature (never trusting the browser's word).
"""
from __future__ import annotations

import hashlib
import hmac
import uuid

from mediflow.common import config


def _secret() -> bytes:
    return config.secret("payment_webhook_secret").encode()


def sign(payment_id: str, provider_ref: str, status: str, amount_paise: int) -> str:
    msg = f"{payment_id}|{provider_ref}|{status}|{amount_paise}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def verify(payment_id: str, provider_ref: str, status: str, amount_paise: int, signature: str) -> bool:
    return hmac.compare_digest(sign(payment_id, provider_ref, status, amount_paise), signature or "")


def charge(payment_id: str, amount_paise: int, method: str) -> dict:
    """Simulate the provider. Method 'test_decline' always fails."""
    status = "failed" if method == "test_decline" else "succeeded"
    provider_ref = "pg_" + uuid.uuid4().hex[:16]
    return {"payment_id": payment_id, "provider_ref": provider_ref, "status": status,
            "amount_paise": amount_paise, "signature": sign(payment_id, provider_ref, status, amount_paise)}
