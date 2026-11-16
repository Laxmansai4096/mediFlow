"""Synchronous service-to-service HTTP calls.

Tests register in-process clients in OVERRIDES so no network is needed.
In Azure, calls travel over the Container Apps environment's internal network
(services are not exposed publicly; only API Management is).
"""
from __future__ import annotations

from typing import Any

import httpx

from . import auth, config, context

OVERRIDES: dict[str, Any] = {}


class ServiceUnavailable(Exception):
    pass


def call(
    service: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    as_service: str | None = None,
    json: Any = None,
    params: dict | None = None,
    timeout: float = 10.0,
) -> httpx.Response:
    """Call another service.

    token       -> forward the end-user's token (target enforces the user's RBAC)
    as_service  -> authenticate as this calling service (internal endpoints)
    """
    headers = {"x-correlation-id": context.correlation_id.get() or ""}
    if token:
        headers["authorization"] = f"Bearer {token}"
    elif as_service:
        headers["authorization"] = f"Bearer {auth.service_token(as_service)}"
    try:
        if service in OVERRIDES:
            return OVERRIDES[service].request(method, path, json=json, params=params, headers=headers)
        with httpx.Client(base_url=config.service_url(service), timeout=timeout) as client:
            return client.request(method, path, json=json, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ServiceUnavailable(f"{service} unavailable: {exc}") from exc
