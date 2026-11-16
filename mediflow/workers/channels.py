"""Notification channels.

Local (free): messages are written to data/outbox/<channel>/ as files you can
open, so you can see exactly what a patient would receive.

Production adapters (not enabled here):
  email     -> Azure Communication Services Email
  whatsapp  -> approved WhatsApp Business Platform templates (via ACS Advanced
               Messaging or Meta Cloud API). Only sent to patients who opted in.
"""
from __future__ import annotations

import json
import random
import time
import uuid
from email.message import EmailMessage

from mediflow.common import config


class TransientDeliveryError(Exception):
    pass


def _failure_injection() -> None:
    """MEDIFLOW_NOTIFY_FAILURE_RATE=0.3 simulates a flaky provider to demo retries/DLQ."""
    rate = float(config.env("MEDIFLOW_NOTIFY_FAILURE_RATE", "0") or 0)
    if rate and random.random() < rate:
        raise TransientDeliveryError("simulated provider timeout")


def send_email(to: str, subject: str, body: str) -> str:
    _failure_injection()
    out = config.data_dir() / "outbox" / "email"
    out.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = "no-reply@mediflow.example"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    ref = f"em_{uuid.uuid4().hex[:12]}"
    (out / f"{time.strftime('%Y%m%d-%H%M%S')}_{ref}.eml").write_bytes(bytes(msg))
    return ref


def send_whatsapp(to: str, template: str, params: dict, lang: str) -> str:
    _failure_injection()
    out = config.data_dir() / "outbox" / "whatsapp"
    out.mkdir(parents=True, exist_ok=True)
    ref = f"wa_{uuid.uuid4().hex[:12]}"
    payload = {  # shape of a WhatsApp Business Platform template message
        "messaging_product": "whatsapp", "to": to, "type": "template",
        "template": {"name": template, "language": {"code": lang},
                     "components": [{"type": "body", "parameters": [{"type": "text", "text": str(v)}
                                                                     for v in params.values()]}]},
    }
    (out / f"{time.strftime('%Y%m%d-%H%M%S')}_{ref}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                                      encoding="utf-8")
    return ref
