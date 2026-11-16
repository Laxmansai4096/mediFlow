"""Identity service - local stand-in for Microsoft Entra ID (External ID for patients).

* PBKDF2 password hashing, password policy
* Account lockout after repeated failures (brute-force protection)
* TOTP MFA mandatory for privileged roles (doctor, billing, admin)
* Generic error messages (no username enumeration)
* Patient self-registration creates the unique patient ID via the patient service
"""
from __future__ import annotations

import json
import time
from datetime import date
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Float, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from mediflow.common import audit, config, service_client
from mediflow.common.app_factory import create_service_app
from mediflow.common.auth import PRIVILEGED_ROLES, Principal, get_principal, issue_token, require, require_mfa
from mediflow.common.db import Database, get_db
from mediflow.common.ids import new_id
from mediflow.common.passwords import (
    hash_password, new_totp_secret, password_problems, totp_uri, verify_password, verify_totp,
)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60


def _mfa_secret() -> str:
    """Hosted demo only: MEDIFLOW_DEMO_MFA_SECRET (an App Service setting, never in source)
    gives all demo staff one authenticator entry. Otherwise each user gets a unique secret."""
    return config.env("MEDIFLOW_DEMO_MFA_SECRET") or new_totp_secret()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str] = mapped_column(String(120))
    patient_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    doctor_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    mfa_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class LoginIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    totp: str | None = Field(default=None, max_length=8)


class SignupIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(min_length=2, max_length=120)
    date_of_birth: date
    sex: Literal["female", "male", "other", "unknown"] = "unknown"
    phone: str = Field(pattern=r"^\+?[0-9]{10,15}$")
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    preferred_language: Literal["en", "hi", "te"] = "en"
    whatsapp_opt_in: bool = False


class StaffUserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    password: str = Field(min_length=10, max_length=128)
    role: Literal["doctor", "billing", "admin"]
    display_name: str = Field(min_length=2, max_length=120)
    doctor_id: str | None = Field(default=None, max_length=20)


def _token_response(user: User, mfa: bool) -> dict:
    token = issue_token(user.id, user.role, name=user.display_name, patient_id=user.patient_id,
                        doctor_id=user.doctor_id, mfa=mfa)
    return {"access_token": token, "token_type": "bearer", "expires_in": config.ACCESS_TOKEN_TTL_SECONDS,
            "role": user.role, "name": user.display_name, "patient_id": user.patient_id,
            "doctor_id": user.doctor_id, "mfa": mfa}


def _bootstrap_admin(db: Database, log) -> None:
    """Create the first admin on an empty system. Credentials are written to a
    local file readable only by the operator (Key Vault secret in Azure)."""
    with db.Session() as s:
        if s.scalar(select(User).limit(1)):
            return
        import secrets as _secrets

        password = config.env("MEDIFLOW_BOOTSTRAP_ADMIN_PASSWORD") or _secrets.token_urlsafe(12) + "1a"
        mfa_secret = _mfa_secret()
        s.add(User(id=new_id("USR"), username="admin", password_hash=hash_password(password), role="admin",
                   display_name="Platform Administrator", mfa_secret=mfa_secret))
        s.commit()
        (config.data_dir() / "bootstrap_admin.json").write_text(
            json.dumps({"username": "admin", "password": password, "mfa_secret": mfa_secret}, indent=2)
        )
        log.info("bootstrap admin created; credentials in data/bootstrap_admin.json")


def create_app():
    app = create_service_app("identity", "Identity Service")
    db = Database("identity", Base)
    app.state.db = db
    _bootstrap_admin(db, app.state.log)

    @app.post("/auth/login", tags=["auth"])
    def login(body: LoginIn, s: Session = Depends(get_db)):
        user = s.scalar(select(User).where(User.username == body.username))
        generic = HTTPException(401, "invalid credentials")
        if not user or not user.active:
            verify_password(body.password, hash_password("timing-equaliser"))  # constant-ish timing
            audit.record("auth.login", actor=body.username, outcome="failure", severity="warning",
                         details={"reason": "unknown_user"})
            raise generic
        if user.locked_until > time.time():
            audit.record("auth.login", actor=user.id, role=user.role, outcome="failure", severity="warning",
                         details={"reason": "locked"})
            raise HTTPException(423, "account temporarily locked; try again later")

        def fail(reason: str):
            user.failed_attempts += 1
            sev = "warning"
            if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
                user.locked_until = time.time() + LOCKOUT_SECONDS
                user.failed_attempts = 0
                sev = "high"
                audit.record("auth.lockout", actor=user.id, role=user.role, outcome="failure", severity="high")
            s.commit()
            audit.record("auth.login", actor=user.id, role=user.role, outcome="failure", severity=sev,
                         details={"reason": reason})
            raise generic

        if not verify_password(body.password, user.password_hash):
            fail("bad_password")
        mfa_ok = False
        if user.role in PRIVILEGED_ROLES and user.mfa_secret:
            if not body.totp:
                raise HTTPException(401, "mfa_required")
            if not verify_totp(user.mfa_secret, body.totp):
                fail("bad_totp")
            mfa_ok = True
        user.failed_attempts = 0
        s.commit()
        audit.record("auth.login", actor=user.id, role=user.role, details={"mfa": mfa_ok})
        return _token_response(user, mfa_ok)

    @app.post("/auth/signup", status_code=201, tags=["auth"])
    def signup(body: SignupIn, s: Session = Depends(get_db)):
        problems = password_problems(body.password)
        if problems:
            raise HTTPException(422, "password must contain " + ", ".join(problems))
        if s.scalar(select(User).where(User.username == body.username)):
            raise HTTPException(409, "username not available")
        profile = body.model_dump(exclude={"username", "password"}, mode="json")
        try:
            resp = service_client.call("patient", "POST", "/internal/patients", as_service="identity", json=profile)
        except service_client.ServiceUnavailable:
            raise HTTPException(503, "registration temporarily unavailable")
        if resp.status_code != 201:
            raise HTTPException(resp.status_code, resp.json().get("detail", "registration failed"))
        patient_id = resp.json()["patient_id"]
        user = User(id=new_id("USR"), username=body.username, password_hash=hash_password(body.password),
                    role="patient", display_name=body.full_name, patient_id=patient_id)
        s.add(user)
        s.commit()
        audit.record("auth.signup", actor=user.id, role="patient", patient_id=patient_id)
        return _token_response(user, False)

    @app.get("/auth/me", tags=["auth"])
    def me(p: Principal = Depends(get_principal)):
        return {"sub": p.sub, "role": p.role, "name": p.name, "patient_id": p.patient_id,
                "doctor_id": p.doctor_id, "mfa": p.mfa, "permissions": sorted(p.permissions)}

    @app.post("/admin/users", status_code=201, tags=["admin"])
    def create_staff(body: StaffUserIn, p: Principal = Depends(require("user:manage")),
                     s: Session = Depends(get_db)):
        require_mfa(p)
        problems = password_problems(body.password)
        if problems:
            raise HTTPException(422, "password must contain " + ", ".join(problems))
        if body.role == "doctor" and not body.doctor_id:
            raise HTTPException(422, "doctor_id is required for doctors")
        if s.scalar(select(User).where(User.username == body.username)):
            raise HTTPException(409, "username not available")
        secret = _mfa_secret()
        user = User(id=new_id("USR"), username=body.username, password_hash=hash_password(body.password),
                    role=body.role, display_name=body.display_name, doctor_id=body.doctor_id, mfa_secret=secret)
        s.add(user)
        s.commit()
        audit.record("user.create", actor=p.sub, role=p.role, resource_type="user", resource_id=user.id,
                     details={"new_role": body.role})
        # MFA secret is returned exactly once for enrolment in an authenticator app.
        return {"id": user.id, "username": user.username, "role": user.role,
                "mfa_secret": secret, "mfa_uri": totp_uri(secret, user.username)}

    @app.get("/admin/users", tags=["admin"])
    def list_users(p: Principal = Depends(require("user:manage")), s: Session = Depends(get_db)):
        users = s.scalars(select(User).order_by(User.created_at)).all()
        return [{"id": u.id, "username": u.username, "role": u.role, "display_name": u.display_name,
                 "patient_id": u.patient_id, "doctor_id": u.doctor_id, "active": u.active,
                 "locked": u.locked_until > time.time(), "mfa_enrolled": bool(u.mfa_secret)} for u in users]

    @app.post("/admin/users/{user_id}/unlock", tags=["admin"])
    def unlock(user_id: str, p: Principal = Depends(require("user:manage")), s: Session = Depends(get_db)):
        require_mfa(p)
        user = s.get(User, user_id)
        if not user:
            raise HTTPException(404, "not found")
        user.locked_until, user.failed_attempts = 0, 0
        s.commit()
        audit.record("user.unlock", actor=p.sub, role=p.role, resource_type="user", resource_id=user_id)
        return {"ok": True}

    @app.post("/admin/users/{user_id}/deactivate", tags=["admin"])
    def deactivate(user_id: str, p: Principal = Depends(require("user:manage")), s: Session = Depends(get_db)):
        require_mfa(p)
        user = s.get(User, user_id)
        if not user:
            raise HTTPException(404, "not found")
        if user.id == p.sub:
            raise HTTPException(409, "cannot deactivate yourself")
        user.active = False
        s.commit()
        audit.record("user.deactivate", actor=p.sub, role=p.role, resource_type="user", resource_id=user_id,
                     severity="warning")
        return {"ok": True}

    return app
