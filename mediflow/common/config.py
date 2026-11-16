"""Central configuration.

Every setting is read from the environment at call time so that tests and
individual processes can override it. Each Azure dependency has a `*_MODE`
switch:

    local  -> free local stand-in (default, zero cost)
    azure  -> real Azure SDK adapter (only when you deploy)
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SERVICE_PORTS = {
    "gateway": 8000,
    "identity": 8001,
    "patient": 8002,
    "appointment": 8003,
    "prescription": 8004,
    "billing": 8005,
    "document": 8006,
    "ai": 8007,
    "audit": 8008,
}


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def data_dir() -> Path:
    path = Path(env("MEDIFLOW_DATA_DIR", str(ROOT / "data")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def environment() -> str:
    return env("MEDIFLOW_ENV", "local")


def mode(component: str) -> str:
    """component in {auth, bus, storage, ai, extraction, search}."""
    return env(f"MEDIFLOW_{component.upper()}_MODE", "local").lower()


def service_url(service: str) -> str:
    return env(f"MEDIFLOW_{service.upper()}_URL", f"http://127.0.0.1:{SERVICE_PORTS[service]}")


def _local_secret(name: str, nbytes: int = 48) -> str:
    """Generate-once secret persisted in the data dir (local stand-in for Key Vault).

    In Azure these values come from Key Vault references injected as env vars
    through the Container App's managed identity - never from source code.
    """
    path = data_dir() / f".{name}"
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(nbytes)
    try:
        with open(path, "x") as fh:  # atomic create; another process may win the race
            fh.write(value)
    except FileExistsError:
        return path.read_text().strip()
    return value


def secret(name: str) -> str:
    """Resolve a secret: env var first (Key Vault reference in Azure), else local file."""
    value = env(f"MEDIFLOW_{name.upper()}")
    return value or _local_secret(name.lower())


def jwt_secret() -> str:
    return secret("jwt_secret")


JWT_ISSUER = "mediflow-identity"
JWT_AUDIENCE = "mediflow-api"
ACCESS_TOKEN_TTL_SECONDS = 3600
