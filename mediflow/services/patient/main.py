"""Patient service - the Unified Patient Profile.

Owns: patient demographics, the unique patient ID, the care-team relationship
(who may see whose records), and the longitudinal timeline that links
consultations, reports, prescriptions, appointments, diet plans and payments.

Field-level access:
  billing / admin  -> demographics only (never diagnoses or clinical notes)
  doctor           -> clinical data only for patients on their care team
  patient          -> their own record only
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Float, Index, String, Text, UniqueConstraint, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from mediflow.common import audit
from mediflow.common.app_factory import create_service_app
from mediflow.common.auth import Principal, deny, require, require_mfa
from mediflow.common.bus import get_bus
from mediflow.common.db import Database, get_db
from mediflow.common.fhir import patient_resource
from mediflow.common.ids import is_valid_patient_id, new_id, new_patient_id
from mediflow.common.security import clean_text

BREAK_GLASS_HOURS = 4


class Base(DeclarativeBase):
    pass


class Patient(Base):
    __tablename__ = "patients"
    patient_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120), index=True)
    date_of_birth: Mapped[str] = mapped_column(String(10))
    sex: Mapped[str] = mapped_column(String(10), default="unknown")
    phone: Mapped[str] = mapped_column(String(20))
    email: Mapped[str] = mapped_column(String(120))
    preferred_language: Mapped[str] = mapped_column(String(5), default="en")
    whatsapp_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    blood_group: Mapped[str] = mapped_column(String(5), default="")
    allergies: Mapped[str] = mapped_column(Text, default="[]")
    chronic_conditions: Mapped[str] = mapped_column(Text, default="[]")
    diet_plan: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class CareTeam(Base):
    __tablename__ = "care_team"
    __table_args__ = (UniqueConstraint("patient_id", "doctor_id", "source"),)
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(20), index=True)
    doctor_id: Mapped[str] = mapped_column(String(20), index=True)
    source: Mapped[str] = mapped_column(String(20))  # appointment | assignment | breakglass
    reason: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class TimelineEntry(Base):
    __tablename__ = "timeline"
    __table_args__ = (
        UniqueConstraint("source_service", "source_id", "kind", name="uq_timeline_source"),
        Index("ix_timeline_patient", "patient_id", "occurred_at"),
    )
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    source_service: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(80))
    author: Mapped[str] = mapped_column(String(80), default="")
    occurred_at: Mapped[str] = mapped_column(String(30))


# --- schemas -----------------------------------------------------------------------

Lang = Literal["en", "hi", "te"]


class PatientIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    date_of_birth: date
    sex: Literal["female", "male", "other", "unknown"] = "unknown"
    phone: str = Field(pattern=r"^\+?[0-9]{10,15}$")
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    preferred_language: Lang = "en"
    whatsapp_opt_in: bool = False
    blood_group: str = Field(default="", pattern=r"^(|A|B|AB|O)[+-]?$")
    allergies: list[str] = Field(default_factory=list, max_length=30)
    chronic_conditions: list[str] = Field(default_factory=list, max_length=30)


class ContactUpdate(BaseModel):
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9]{10,15}$")
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    preferred_language: Lang | None = None
    whatsapp_opt_in: bool | None = None


class ClinicalUpdate(BaseModel):
    blood_group: str | None = Field(default=None, pattern=r"^(|A|B|AB|O)[+-]?$")
    allergies: list[str] | None = Field(default=None, max_length=30)
    chronic_conditions: list[str] | None = Field(default=None, max_length=30)


class ConsultationIn(BaseModel):
    chief_complaint: str = Field(min_length=2, max_length=500)
    clinical_notes: str = Field(default="", max_length=5000)
    assessment: str = Field(default="", max_length=2000)
    plan: str = Field(default="", max_length=2000)
    follow_up_days: int | None = Field(default=None, ge=0, le=365)


class DietPlanIn(BaseModel):
    plan: str = Field(min_length=2, max_length=4000)


class BreakGlassIn(BaseModel):
    reason: str = Field(min_length=15, max_length=500)


class TimelineIn(BaseModel):
    kind: str = Field(max_length=30)
    title: str = Field(max_length=200)
    detail: dict = Field(default_factory=dict)
    source_service: str = Field(max_length=30)
    source_id: str = Field(max_length=80)
    author: str = ""
    occurred_at: str | None = None


class CareTeamIn(BaseModel):
    patient_id: str
    doctor_id: str
    source: Literal["appointment", "assignment"] = "appointment"


# --- helpers -----------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age(dob: str) -> int:
    d = date.fromisoformat(dob)
    t = date.today()
    return t.year - d.year - ((t.month, t.day) < (d.month, d.day))


def demographics(p: Patient) -> dict:
    return {"patient_id": p.patient_id, "full_name": p.full_name, "date_of_birth": p.date_of_birth,
            "age": _age(p.date_of_birth), "sex": p.sex, "phone": p.phone, "email": p.email,
            "preferred_language": p.preferred_language, "whatsapp_opt_in": p.whatsapp_opt_in}


def full_profile(p: Patient) -> dict:
    return {**demographics(p), "blood_group": p.blood_group, "allergies": json.loads(p.allergies),
            "chronic_conditions": json.loads(p.chronic_conditions), "diet_plan": p.diet_plan,
            "created_at": datetime.fromtimestamp(p.created_at, timezone.utc).isoformat(timespec="seconds")}


def doctor_has_access(s: Session, patient_id: str, doctor_id: str | None) -> bool:
    if not doctor_id:
        return False
    now = time.time()
    return s.scalar(select(CareTeam.id).where(
        CareTeam.patient_id == patient_id, CareTeam.doctor_id == doctor_id,
        or_(CareTeam.expires_at.is_(None), CareTeam.expires_at > now),
    ).limit(1)) is not None


def can_access(s: Session, principal: Principal, patient_id: str, scope: str) -> bool:
    if principal.role == "service":
        return True
    if principal.role == "patient":
        return principal.patient_id == patient_id
    if principal.role == "doctor":
        return doctor_has_access(s, patient_id, principal.doctor_id)
    if principal.role in ("billing", "admin"):
        return scope == "demographics"
    return False


def get_patient_or_404(s: Session, patient_id: str) -> Patient:
    p = s.get(Patient, patient_id)
    if not p:
        raise HTTPException(404, "patient not found")
    return p


def enforce(s: Session, principal: Principal, patient_id: str, scope: str, action: str) -> None:
    if not can_access(s, principal, patient_id, scope):
        raise deny(principal, action, resource_type="patient", resource_id=patient_id, patient_id=patient_id,
                   reason="not authorized for this patient")
    if principal.role != "service":
        audit.record(action, actor=principal.sub, role=principal.role, resource_type="patient",
                     resource_id=patient_id, patient_id=patient_id, details={"scope": scope})


def add_timeline(s: Session, patient_id: str, kind: str, title: str, detail: dict, source_service: str,
                 source_id: str, author: str = "", occurred_at: str | None = None) -> tuple[TimelineEntry, bool]:
    """Idempotent on (source_service, source_id, kind) - replayed events don't duplicate."""
    existing = s.scalar(select(TimelineEntry).where(
        TimelineEntry.source_service == source_service, TimelineEntry.source_id == source_id,
        TimelineEntry.kind == kind))
    if existing:
        return existing, False
    entry = TimelineEntry(id=new_id("TL"), patient_id=patient_id, kind=kind, title=clean_text(title, 200),
                          detail=json.dumps(detail), source_service=source_service, source_id=source_id,
                          author=author, occurred_at=occurred_at or _now_iso())
    s.add(entry)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        return s.scalar(select(TimelineEntry).where(
            TimelineEntry.source_service == source_service, TimelineEntry.source_id == source_id,
            TimelineEntry.kind == kind)), False
    return entry, True


def timeline_dict(e: TimelineEntry) -> dict:
    return {"id": e.id, "kind": e.kind, "title": e.title, "detail": json.loads(e.detail),
            "source_service": e.source_service, "source_id": e.source_id, "author": e.author,
            "occurred_at": e.occurred_at}


def create_patient_record(s: Session, body: PatientIn) -> Patient:
    for _ in range(5):
        pid = new_patient_id()
        if not s.get(Patient, pid):
            break
    p = Patient(patient_id=pid, full_name=clean_text(body.full_name, 120), date_of_birth=body.date_of_birth.isoformat(),
                sex=body.sex, phone=body.phone, email=body.email.lower(), preferred_language=body.preferred_language,
                whatsapp_opt_in=body.whatsapp_opt_in, blood_group=body.blood_group,
                allergies=json.dumps([clean_text(a, 80) for a in body.allergies]),
                chronic_conditions=json.dumps([clean_text(c, 120) for c in body.chronic_conditions]))
    s.add(p)
    s.commit()
    add_timeline(s, pid, "registration", "Patient registered", {}, "patient", pid)
    get_bus().publish("patient-events", "patient.registered", {"patient_id": pid},
                      message_id=f"patient.registered:{pid}")
    return p


# --- app ---------------------------------------------------------------------------

def create_app():
    app = create_service_app("patient", "Patient Service")
    app.state.db = Database("patient", Base)

    # Registration ------------------------------------------------------------------
    @app.post("/patients", status_code=201, tags=["patients"])
    def register_patient(body: PatientIn, p: Principal = Depends(require("profile:create")),
                         s: Session = Depends(get_db)):
        patient = create_patient_record(s, body)
        audit.record("patient.create", actor=p.sub, role=p.role, resource_type="patient",
                     resource_id=patient.patient_id, patient_id=patient.patient_id)
        return demographics(patient)

    # Read --------------------------------------------------------------------------
    @app.get("/me", tags=["patients"])
    def my_profile(p: Principal = Depends(require("profile:read:self")), s: Session = Depends(get_db)):
        patient = get_patient_or_404(s, p.patient_id or "")
        audit.record("patient.read", actor=p.sub, role=p.role, resource_type="patient",
                     resource_id=patient.patient_id, patient_id=patient.patient_id)
        return full_profile(patient)

    @app.get("/patients", tags=["patients"])
    def search(q: str = Query("", max_length=80),
               p: Principal = Depends(require("profile:read:care", "profile:read:demographics")),
               s: Session = Depends(get_db)):
        stmt = select(Patient)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Patient.full_name.ilike(like), Patient.patient_id == q.upper()))
        if p.role == "doctor":
            now = time.time()
            stmt = stmt.where(Patient.patient_id.in_(select(CareTeam.patient_id).where(
                CareTeam.doctor_id == p.doctor_id, or_(CareTeam.expires_at.is_(None), CareTeam.expires_at > now))))
        rows = s.scalars(stmt.order_by(Patient.full_name).limit(25)).all()
        audit.record("patient.search", actor=p.sub, role=p.role, details={"results": len(rows)})
        return [demographics(r) for r in rows]

    @app.get("/patients/{patient_id}", tags=["patients"])
    def get_patient(patient_id: str, p: Principal = Depends(require(
            "profile:read:self", "profile:read:care", "profile:read:demographics")),
            s: Session = Depends(get_db)):
        patient = get_patient_or_404(s, patient_id)
        if can_access(s, p, patient_id, "clinical"):
            enforce(s, p, patient_id, "clinical", "patient.read")
            return full_profile(patient)
        enforce(s, p, patient_id, "demographics", "patient.read.demographics")
        return demographics(patient)

    @app.get("/patients/{patient_id}/timeline", tags=["clinical"])
    def timeline(patient_id: str, kind: str | None = None, limit: int = Query(100, le=500),
                 p: Principal = Depends(require("clinical:read:self", "clinical:read:care")),
                 s: Session = Depends(get_db)):
        get_patient_or_404(s, patient_id)
        enforce(s, p, patient_id, "clinical", "timeline.read")
        stmt = select(TimelineEntry).where(TimelineEntry.patient_id == patient_id)
        if kind:
            stmt = stmt.where(TimelineEntry.kind == kind)
        rows = s.scalars(stmt.order_by(TimelineEntry.occurred_at.desc()).limit(limit)).all()
        return [timeline_dict(e) for e in rows]

    # Clinical writes ---------------------------------------------------------------
    @app.post("/patients/{patient_id}/consultations", status_code=201, tags=["clinical"])
    def add_consultation(patient_id: str, body: ConsultationIn,
                         p: Principal = Depends(require("clinical:write:care")), s: Session = Depends(get_db)):
        get_patient_or_404(s, patient_id)
        enforce(s, p, patient_id, "clinical", "consultation.create")
        cid = new_id("CN")
        entry, _ = add_timeline(s, patient_id, "consultation", f"Consultation: {body.chief_complaint}",
                                {k: clean_text(v, 5000) if isinstance(v, str) else v
                                 for k, v in body.model_dump().items()} | {"doctor_id": p.doctor_id},
                                "patient", cid, author=p.name)
        return timeline_dict(entry)

    @app.put("/patients/{patient_id}/diet-plan", tags=["clinical"])
    def set_diet_plan(patient_id: str, body: DietPlanIn, p: Principal = Depends(require("clinical:write:care")),
                      s: Session = Depends(get_db)):
        patient = get_patient_or_404(s, patient_id)
        enforce(s, p, patient_id, "clinical", "dietplan.update")
        patient.diet_plan = clean_text(body.plan, 4000)
        s.commit()
        add_timeline(s, patient_id, "diet_plan", "Dietary plan updated", {"plan": patient.diet_plan},
                     "patient", new_id("DP"), author=p.name)
        return {"ok": True}

    @app.patch("/patients/{patient_id}/clinical", tags=["clinical"])
    def update_clinical(patient_id: str, body: ClinicalUpdate, p: Principal = Depends(require("clinical:write:care")),
                        s: Session = Depends(get_db)):
        patient = get_patient_or_404(s, patient_id)
        enforce(s, p, patient_id, "clinical", "clinical.update")
        if body.blood_group is not None:
            patient.blood_group = body.blood_group
        if body.allergies is not None:
            patient.allergies = json.dumps([clean_text(a, 80) for a in body.allergies])
        if body.chronic_conditions is not None:
            patient.chronic_conditions = json.dumps([clean_text(c, 120) for c in body.chronic_conditions])
        s.commit()
        return full_profile(patient)

    @app.patch("/patients/{patient_id}/contact", tags=["patients"])
    def update_contact(patient_id: str, body: ContactUpdate,
                       p: Principal = Depends(require("profile:update:self", "profile:update:any")),
                       s: Session = Depends(get_db)):
        patient = get_patient_or_404(s, patient_id)
        if p.role == "patient" and p.patient_id != patient_id:
            raise deny(p, "patient.contact.update", patient_id=patient_id)
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(patient, field, value)
        s.commit()
        audit.record("patient.contact.update", actor=p.sub, role=p.role, resource_type="patient",
                     resource_id=patient_id, patient_id=patient_id, details={"fields": list(body.model_dump(exclude_none=True))})
        return demographics(patient)

    @app.post("/patients/{patient_id}/break-glass", tags=["clinical"])
    def break_glass(patient_id: str, body: BreakGlassIn, p: Principal = Depends(require("breakglass")),
                    s: Session = Depends(get_db)):
        """Emergency access for a doctor not on the care team. Time-boxed, justified,
        and raised as a critical audit event for compliance review."""
        require_mfa(p)
        get_patient_or_404(s, patient_id)
        expires = time.time() + BREAK_GLASS_HOURS * 3600
        existing = s.scalar(select(CareTeam).where(CareTeam.patient_id == patient_id,
                                                   CareTeam.doctor_id == p.doctor_id, CareTeam.source == "breakglass"))
        if existing:
            existing.expires_at, existing.reason = expires, clean_text(body.reason, 500)
        else:
            s.add(CareTeam(id=new_id("CT"), patient_id=patient_id, doctor_id=p.doctor_id or "", source="breakglass",
                           reason=clean_text(body.reason, 500), expires_at=expires))
        s.commit()
        audit.record("patient.breakglass", actor=p.sub, role=p.role, resource_type="patient", resource_id=patient_id,
                     patient_id=patient_id, severity="critical", details={"reason": clean_text(body.reason, 500),
                                                                         "hours": BREAK_GLASS_HOURS})
        return {"granted": True, "expires_in_hours": BREAK_GLASS_HOURS}

    # FHIR ----------------------------------------------------------------------------
    @app.get("/fhir/Patient/{patient_id}", tags=["fhir"])
    def fhir_patient(patient_id: str, p: Principal = Depends(require(
            "profile:read:self", "profile:read:care")), s: Session = Depends(get_db)):
        patient = get_patient_or_404(s, patient_id)
        enforce(s, p, patient_id, "clinical", "fhir.patient.read")
        return patient_resource(patient)

    # Internal (service-to-service only) ----------------------------------------------
    internal = require("*")

    @app.post("/internal/patients", status_code=201, tags=["internal"])
    def internal_create(body: PatientIn, p: Principal = Depends(internal), s: Session = Depends(get_db)):
        return demographics(create_patient_record(s, body))

    @app.get("/internal/patients/{patient_id}/contact", tags=["internal"])
    def internal_contact(patient_id: str, p: Principal = Depends(internal), s: Session = Depends(get_db)):
        patient = get_patient_or_404(s, patient_id)
        return {"patient_id": patient_id, "full_name": patient.full_name, "phone": patient.phone,
                "email": patient.email, "preferred_language": patient.preferred_language,
                "whatsapp_opt_in": patient.whatsapp_opt_in}

    @app.get("/internal/patients/{patient_id}/clinical-flags", tags=["internal"])
    def internal_flags(patient_id: str, p: Principal = Depends(internal), s: Session = Depends(get_db)):
        patient = get_patient_or_404(s, patient_id)
        return {"allergies": json.loads(patient.allergies), "chronic_conditions": json.loads(patient.chronic_conditions)}

    @app.get("/internal/access-check", tags=["internal"])
    def access_check(patient_id: str, doctor_id: str, p: Principal = Depends(internal), s: Session = Depends(get_db)):
        return {"allowed": doctor_has_access(s, patient_id, doctor_id)}

    @app.post("/internal/care-team", tags=["internal"])
    def add_care_team(body: CareTeamIn, p: Principal = Depends(internal), s: Session = Depends(get_db)):
        if not s.get(Patient, body.patient_id):
            raise HTTPException(404, "patient not found")
        exists = s.scalar(select(CareTeam).where(CareTeam.patient_id == body.patient_id,
                                                 CareTeam.doctor_id == body.doctor_id, CareTeam.source == body.source))
        if not exists:
            s.add(CareTeam(id=new_id("CT"), patient_id=body.patient_id, doctor_id=body.doctor_id, source=body.source))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
        return {"ok": True}

    @app.post("/internal/patients/{patient_id}/timeline", tags=["internal"])
    def internal_timeline(patient_id: str, body: TimelineIn, p: Principal = Depends(internal),
                          s: Session = Depends(get_db)):
        if not is_valid_patient_id(patient_id) or not s.get(Patient, patient_id):
            raise HTTPException(404, "patient not found")
        entry, created = add_timeline(s, patient_id, body.kind, body.title, body.detail, body.source_service,
                                      body.source_id, body.author, body.occurred_at)
        return {"id": entry.id, "created": created}

    return app
