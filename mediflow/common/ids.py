"""Identifier generation.

Patient IDs are random (not sequential) so they cannot be enumerated, and they
carry a Luhn check digit so typos at the front desk are caught immediately.
Format: MF-XXXX-XXXX-C  (8 random digits + 1 check digit)
"""
from __future__ import annotations

import re
import secrets
import uuid

PATIENT_ID_RE = re.compile(r"^MF-(\d{4})-(\d{4})-(\d)$")


def luhn_check_digit(digits: str) -> int:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return (10 - total % 10) % 10


def new_patient_id() -> str:
    body = f"{secrets.randbelow(10**8):08d}"
    return f"MF-{body[:4]}-{body[4:]}-{luhn_check_digit(body)}"


def is_valid_patient_id(value: str) -> bool:
    m = PATIENT_ID_RE.match(value or "")
    if not m:
        return False
    body = m.group(1) + m.group(2)
    return luhn_check_digit(body) == int(m.group(3))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"
