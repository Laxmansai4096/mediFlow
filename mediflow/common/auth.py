"""Authentication & role-based access control.

Local mode: HS256 JWTs issued by the identity service (secret from Key Vault
stand-in). Entra mode (MEDIFLOW_AUTH_MODE=entra): RS256 tokens issued by
Microsoft Entra ID, validated against the tenant's JWKS, with app roles mapped
to MediFlow roles. Every microservice validates tokens itself (zero trust) -
the gateway is not the only line of defence.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request, status

from . import audit, config

# Permission model ---------------------------------------------------------------
# ":self"  -> only the caller's own patient record
# ":care"  -> only patients on the doctor's care team (checked per request)
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "patient": {
        "profile:read:self", "profile:update:self", "clinical:read:self",
        "appointment:book:self", "appointment:read:self", "appointment:cancel:self",
        "prescription:read:self", "refill:request:self",
        "billing:read:self", "billing:pay:self",
        "document:upload:self", "document:read:self",
        "chat:use", "accesslog:read:self",
    },
    "doctor": {
        "profile:read:care", "clinical:read:care", "clinical:write:care", "clinical:summarize",
        "appointment:read:own", "appointment:manage:own",
        "prescription:read:care", "prescription:write", "prescription:approve", "refill:decide",
        "document:upload:care", "document:read:care",
        "breakglass", "chat:use", "doctor:read",
    },
    "billing": {
        "profile:read:demographics", "billing:read:any", "billing:write", "appointment:read:any",
    },
    "admin": {
        "profile:create", "profile:read:demographics", "profile:update:any",
        "user:manage", "doctor:manage", "doctor:read",
        "audit:read", "ops:read", "ops:write", "appointment:read:any", "appointment:book:any",
    },
    "service": {"*"},
}

PRIVILEGED_ROLES = {"doctor", "billing", "admin"}


@dataclass
class Principal:
    sub: str
    role: str
    name: str = ""
    patient_id: str | None = None
    doctor_id: str | None = None
    mfa: bool = False
    token: str = field(default="", repr=False)

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def can(self, permission: str) -> bool:
        perms = self.permissions
        return "*" in perms or permission in perms


# Token issue / validation ----------------------------------------------------------

def issue_token(
    sub: str,
    role: str,
    *,
    name: str = "",
    patient_id: str | None = None,
    doctor_id: str | None = None,
    mfa: bool = False,
    ttl: int = config.ACCESS_TOKEN_TTL_SECONDS,
) -> str:
    now = int(time.time())
    claims = {
        "iss": config.JWT_ISSUER,
        "aud": config.JWT_AUDIENCE,
        "sub": sub,
        "role": role,
        "name": name,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "amr": ["pwd", "mfa"] if mfa else ["pwd"],
    }
    if patient_id:
        claims["patient_id"] = patient_id
    if doctor_id:
        claims["doctor_id"] = doctor_id
    return jwt.encode(claims, config.jwt_secret(), algorithm="HS256")


@lru_cache(maxsize=1)
def _entra_jwks_client() -> jwt.PyJWKClient:
    tenant = config.env("MEDIFLOW_ENTRA_TENANT_ID")
    return jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys")


def _decode_entra(token: str) -> dict:
    tenant = config.env("MEDIFLOW_ENTRA_TENANT_ID")
    key = _entra_jwks_client().get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=config.env("MEDIFLOW_ENTRA_API_AUDIENCE"),
        issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
    )
    roles = claims.get("roles") or []
    claims["role"] = next((r.lower() for r in roles if r.lower() in ROLE_PERMISSIONS), "patient")
    claims["patient_id"] = claims.get("extn.patient_id") or claims.get("patient_id")
    claims["doctor_id"] = claims.get("extn.doctor_id") or claims.get("doctor_id")
    return claims


def decode_token(token: str) -> dict:
    # Service-to-service tokens are always locally signed (managed identity in Azure).
    if config.mode("auth") == "entra":
        try:
            return _decode_entra(token)
        except jwt.PyJWTError:
            pass
    return jwt.decode(
        token,
        config.jwt_secret(),
        algorithms=["HS256"],
        audience=config.JWT_AUDIENCE,
        issuer=config.JWT_ISSUER,
        options={"require": ["exp", "sub", "iat"]},
    )


def principal_from_token(token: str) -> Principal:
    claims = decode_token(token)
    return Principal(
        sub=claims["sub"],
        role=claims.get("role", ""),
        name=claims.get("name", ""),
        patient_id=claims.get("patient_id"),
        doctor_id=claims.get("doctor_id"),
        mfa="mfa" in (claims.get("amr") or []),
        token=token,
    )


_service_tokens: dict[str, tuple[str, float]] = {}


def service_token(service: str) -> str:
    """Short-lived token for service-to-service calls (managed identity stand-in)."""
    cached = _service_tokens.get(service)
    if cached and cached[1] - time.time() > 60:
        return cached[0]
    token = issue_token(f"svc:{service}", "service", name=service, mfa=True, ttl=600)
    _service_tokens[service] = (token, time.time() + 600)
    return token


# FastAPI dependencies --------------------------------------------------------------

def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def get_principal(request: Request) -> Principal:
    token = _bearer(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        return principal_from_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})


def optional_principal(request: Request) -> Principal | None:
    token = _bearer(request)
    if not token:
        return None
    try:
        return principal_from_token(token)
    except jwt.PyJWTError:
        return None


def deny(principal: Principal, action: str, *, resource_type: str = "", resource_id: str = "",
         patient_id: str = "", reason: str = "forbidden") -> HTTPException:
    audit.record(
        "access.denied", actor=principal.sub, role=principal.role, resource_type=resource_type,
        resource_id=resource_id, patient_id=patient_id, outcome="denied", severity="warning",
        details={"attempted": action, "reason": reason},
    )
    return HTTPException(status.HTTP_403_FORBIDDEN, reason)


def require(*permissions: str):
    """Dependency: caller must hold at least one of the permissions."""

    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not any(principal.can(p) for p in permissions):
            raise deny(principal, "|".join(permissions), reason="insufficient role permissions")
        return principal

    return dependency


def require_mfa(principal: Principal) -> None:
    if principal.role in PRIVILEGED_ROLES and not principal.mfa:
        raise deny(principal, "mfa", reason="multi-factor authentication required for this action")
