"""Cross-cutting API protections: rate limiting, headers, input hygiene."""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline';"
                               " script-src 'self'; frame-ancestors 'none'; base-uri 'none'",
}


def clean_text(value: str | None, max_len: int = 2000) -> str:
    """Strip control characters and bound length (defence in depth beyond pydantic)."""
    if not value:
        return ""
    return _CONTROL_CHARS.sub("", value).strip()[:max_len]


def mask_phone(phone: str | None) -> str:
    if not phone or len(phone) < 4:
        return ""
    return "*" * (len(phone) - 4) + phone[-4:]


class SlidingWindowRateLimiter:
    """In-memory limiter (per replica). In Azure, APIM's rate-limit-by-key policy
    enforces limits globally before traffic reaches the services."""

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = int(self.window - (now - hits[0])) + 1
                return False, retry_after
            hits.append(now)
            return True, 0
