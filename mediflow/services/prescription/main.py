"""Prescription service - digital prescriptions, versioning, approval and refills.

Rules:
* Every change (create / update / renew / discontinue) is a new immutable
  version in `pending` state. Nothing reaches the patient until an authorised
  prescriber (doctor, MFA-verified, on the patient's care team) approves it.
* Full version history is retained; approved versions are never edited.
* Refills: allowed only while active, unexpired and refills remain.
* Allergy check against the patient record; conflicts must be acknowledged.
* Patients can read and request refills, but can never modify prescriptions.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from mediflow.common import audit, service_client
from mediflow.common.access import enforce_patient_access
from mediflow.common.app_factory import create_service_app
from mediflow.common.auth import Principal, deny, get_principal, require, require_mfa
from mediflow.common.bus import get_bus
from mediflow.common.db import Database, get_db
from mediflow.common.fhir import medication_request
from mediflow.common.ids import new_id
from mediflow.common.security import clean_text


class Base(DeclarativeBase):
    pass


class Prescription(Base):
    __tablename__ = "prescriptions"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(20), index=True)
    prescriber_id: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending_approval")
    approved_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=1)
    refills_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class PrescriptionVersion(Base):
    __tablename__ = "prescription_versions"
    __table_args__ = (UniqueConstraint("prescription_id", "version"),)
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    prescription_id: Mapped[str] = mapped_column(String(20), index=True)
    version: Mapped[int] = mapped_column(Integer)
    change_type: Mapped[str] = mapped_column(String(15))  # create|update|renew|discontinue
    medication: Mapped[str] = mapped_column(String(120))
    dosage: Mapped[str] = mapped_column(String(60))
    frequency: Mapped[str] = mapped_column(String(60))
    route: Mapped[str] = mapped_column(String(30), default="oral")
    duration_days: Mapped[int] = mapped_column(Integer)
    refills_allowed: Mapped[int] = mapped_column(Integer, default=0)
    instructions: Mapped[str] = mapped_column(Text, default="")
    start_date: Mapped[str] = mapped_column(String(10))
    expiry_date: Mapped[str] = mapped_column(String(10))
    change_reason: Mapped[str] = mapped_column(Text, default="")
    warnings: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    approval_status: Mapped[str] = mapped_column(String(10), default="pending")  # pending|approved|rejected
    decided_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")


class RefillRequest(Base):
    __tablename__ = "refill_requests"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    prescription_id: Mapped[str] = mapped_column(String(20), index=True)
    patient_id: Mapped[str] = mapped_column(String(20))
    prescriber_id: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(10), default="requested")  # requested|approved|denied
    note: Mapped[str] = mapped_column(Text, default="")
    requested_at: Mapped[float] = mapped_column(Float, default=time.time)
    decided_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class RxIn(BaseModel):
    medication: str = Field(min_length=2, max_length=120)
    dosage: str = Field(min_length=1, max_length=60)
    frequency: str = Field(min_length=1, max_length=60)
    route: Literal["oral", "topical", "inhalation", "injection", "sublingual", "other"] = "oral"
    duration_days: int = Field(ge=1, le=365)
    refills_allowed: int = Field(default=0, ge=0, le=12)
    instructions: str = Field(default="", max_length=1000)
    reason: str = Field(default="", max_length=500)
    acknowledge_warnings: bool = False


class RenewIn(BaseModel):
    duration_days: int = Field(ge=1, le=365)
    refills_allowed: int = Field(default=0, ge=0, le=12)
    reason: str = Field(default="", max_length=500)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=500)


def _today() -> date:
    return date.today()


def version_dict(v: PrescriptionVersion) -> dict:
    return {k: getattr(v, k) for k in (
        "version", "change_type", "medication", "dosage", "frequency", "route", "duration_days",
        "refills_allowed", "instructions", "start_date", "expiry_date", "change_reason", "created_by",
        "approval_status", "decided_by", "decision_note")} | {"warnings": [w for w in v.warnings.split("|") if w]}


def create_app():
    app = create_service_app("prescription", "Prescription Service")
    app.state.db = Database("prescription", Base)

    def get_version(s: Session, rx_id: str, version: int | None) -> PrescriptionVersion | None:
        if version is None:
            return None
        return s.scalar(select(PrescriptionVersion).where(PrescriptionVersion.prescription_id == rx_id,
                                                          PrescriptionVersion.version == version))

    def refresh_expiry(s: Session, rx: Prescription) -> None:
        v = get_version(s, rx.id, rx.approved_version)
        if rx.status == "active" and v and v.expiry_date < _today().isoformat():
            rx.status = "expired"
            s.commit()

    def rx_dict(s: Session, rx: Prescription, include_history: bool = False) -> dict:
        refresh_expiry(s, rx)
        current = get_version(s, rx.id, rx.approved_version)
        latest = get_version(s, rx.id, rx.latest_version)
        eligible, why = refill_eligibility(s, rx, current)
        out = {"id": rx.id, "patient_id": rx.patient_id, "prescriber_id": rx.prescriber_id, "status": rx.status,
               "current": version_dict(current) if current else None,
               "pending_change": version_dict(latest) if latest and latest.approval_status == "pending" else None,
               "refills_used": rx.refills_used,
               "refills_remaining": max((current.refills_allowed if current else 0) - rx.refills_used, 0),
               "refill_eligible": eligible, "refill_ineligible_reason": why}
        if include_history:
            out["history"] = [version_dict(v) for v in s.scalars(select(PrescriptionVersion).where(
                PrescriptionVersion.prescription_id == rx.id).order_by(PrescriptionVersion.version)).all()]
        return out

    def refill_eligibility(s: Session, rx: Prescription, current: PrescriptionVersion | None) -> tuple[bool, str]:
        if rx.status != "active" or not current:
            return False, f"prescription is {rx.status}"
        if current.expiry_date < _today().isoformat():
            return False, "prescription has expired"
        if rx.refills_used >= current.refills_allowed:
            return False, "no refills remaining"
        pending = s.scalar(select(RefillRequest.id).where(RefillRequest.prescription_id == rx.id,
                                                          RefillRequest.status == "requested"))
        if pending:
            return False, "a refill request is already pending"
        return True, ""

    def allergy_warnings(patient_id: str, medication: str) -> list[str]:
        try:
            r = service_client.call("patient", "GET", f"/internal/patients/{patient_id}/clinical-flags",
                                    as_service="prescription")
            allergies = r.json().get("allergies", []) if r.status_code == 200 else []
        except service_client.ServiceUnavailable:
            return ["allergy check unavailable - verify manually"]
        med = medication.lower()
        return [f"patient allergy: {a}" for a in allergies if a and (a.lower() in med or med in a.lower())]

    def doctor_only(p: Principal) -> None:
        if p.role != "doctor":
            raise deny(p, "prescription.write", reason="only an authorized prescriber may change prescriptions")

    def load_rx(s: Session, rx_id: str) -> Prescription:
        rx = s.get(Prescription, rx_id)
        if not rx:
            raise HTTPException(404, "prescription not found")
        return rx

    def new_version(s: Session, rx: Prescription, p: Principal, change_type: str, base: dict, reason: str,
                    warnings: list[str] | None = None) -> PrescriptionVersion:
        pending = s.scalar(select(PrescriptionVersion.id).where(
            PrescriptionVersion.prescription_id == rx.id, PrescriptionVersion.approval_status == "pending"))
        if pending:
            raise HTTPException(409, "a change is already awaiting approval for this prescription")
        start = _today()
        v = PrescriptionVersion(
            id=new_id("RXV"), prescription_id=rx.id, version=rx.latest_version + 1,
            change_type=change_type, start_date=start.isoformat(),
            expiry_date=(start + timedelta(days=base["duration_days"])).isoformat(),
            change_reason=clean_text(reason, 500), warnings="|".join(warnings or []), created_by=p.sub,
            **{k: base[k] for k in ("medication", "dosage", "frequency", "route", "duration_days",
                                    "refills_allowed", "instructions")})
        rx.latest_version = v.version
        rx.updated_at = time.time()
        s.add(v)
        s.commit()
        audit.record(f"prescription.{change_type}", actor=p.sub, role=p.role, resource_type="prescription",
                     resource_id=rx.id, patient_id=rx.patient_id, details={"version": v.version})
        return v

    # Create / change -------------------------------------------------------------------
    @app.post("/patients/{patient_id}/prescriptions", status_code=201, tags=["prescriptions"])
    def create_rx(patient_id: str, body: RxIn, p: Principal = Depends(require("prescription:write")),
                  s: Session = Depends(get_db)):
        doctor_only(p)
        enforce_patient_access(p, patient_id, scope="clinical", action="prescription.create.check",
                               resource_type="prescription")
        warnings = allergy_warnings(patient_id, body.medication)
        if warnings and not body.acknowledge_warnings:
            raise HTTPException(409, {"message": "safety warnings must be acknowledged", "warnings": warnings})
        rx = Prescription(id=new_id("RX"), patient_id=patient_id, prescriber_id=p.doctor_id or "", latest_version=0)
        s.add(rx)
        s.flush()
        base = body.model_dump()
        base["medication"], base["instructions"] = clean_text(body.medication, 120), clean_text(body.instructions, 1000)
        new_version(s, rx, p, "create", base, body.reason, warnings)
        return rx_dict(s, rx, include_history=True)

    @app.put("/prescriptions/{rx_id}", tags=["prescriptions"])
    def update_rx(rx_id: str, body: RxIn, p: Principal = Depends(require("prescription:write")),
                  s: Session = Depends(get_db)):
        doctor_only(p)
        rx = load_rx(s, rx_id)
        enforce_patient_access(p, rx.patient_id, scope="clinical", action="prescription.update.check",
                               resource_type="prescription", resource_id=rx_id)
        if rx.status not in ("active", "pending_approval"):
            raise HTTPException(409, f"cannot update a {rx.status} prescription")
        warnings = allergy_warnings(rx.patient_id, body.medication)
        if warnings and not body.acknowledge_warnings:
            raise HTTPException(409, {"message": "safety warnings must be acknowledged", "warnings": warnings})
        new_version(s, rx, p, "update", body.model_dump(), body.reason, warnings)
        return rx_dict(s, rx, include_history=True)

    @app.post("/prescriptions/{rx_id}/renew", tags=["prescriptions"])
    def renew_rx(rx_id: str, body: RenewIn, p: Principal = Depends(require("prescription:write")),
                 s: Session = Depends(get_db)):
        doctor_only(p)
        rx = load_rx(s, rx_id)
        enforce_patient_access(p, rx.patient_id, scope="clinical", action="prescription.renew.check",
                               resource_type="prescription", resource_id=rx_id)
        refresh_expiry(s, rx)
        current = get_version(s, rx.id, rx.approved_version)
        if not current or rx.status not in ("active", "expired"):
            raise HTTPException(409, "only active or expired prescriptions can be renewed")
        base = version_dict(current) | {"duration_days": body.duration_days, "refills_allowed": body.refills_allowed}
        new_version(s, rx, p, "renew", base, body.reason)
        return rx_dict(s, rx, include_history=True)

    @app.post("/prescriptions/{rx_id}/discontinue", tags=["prescriptions"])
    def discontinue_rx(rx_id: str, body: ReasonIn, p: Principal = Depends(require("prescription:write")),
                       s: Session = Depends(get_db)):
        doctor_only(p)
        rx = load_rx(s, rx_id)
        enforce_patient_access(p, rx.patient_id, scope="clinical", action="prescription.discontinue.check",
                               resource_type="prescription", resource_id=rx_id)
        current = get_version(s, rx.id, rx.approved_version)
        if rx.status != "active" or not current:
            raise HTTPException(409, "only active prescriptions can be discontinued")
        new_version(s, rx, p, "discontinue", version_dict(current), body.reason)
        return rx_dict(s, rx, include_history=True)

    # Approval -------------------------------------------------------------------------
    @app.post("/prescriptions/{rx_id}/versions/{version}/approve", tags=["prescriptions"])
    def approve(rx_id: str, version: int, body: NoteIn, p: Principal = Depends(require("prescription:approve")),
                s: Session = Depends(get_db)):
        doctor_only(p)
        require_mfa(p)
        rx = load_rx(s, rx_id)
        enforce_patient_access(p, rx.patient_id, scope="clinical", action="prescription.approve.check",
                               resource_type="prescription", resource_id=rx_id)
        v = get_version(s, rx_id, version)
        if not v or v.approval_status != "pending":
            raise HTTPException(409, "no pending version to approve")
        v.approval_status, v.decided_by, v.decided_at = "approved", p.sub, time.time()
        v.decision_note = clean_text(body.note, 500)
        rx.approved_version = version
        if v.change_type == "discontinue":
            rx.status = "discontinued"
        else:
            rx.status = "active"
            if v.change_type in ("create", "renew"):
                rx.refills_used = 0
        rx.updated_at = time.time()
        s.commit()
        audit.record("prescription.approve", actor=p.sub, role=p.role, resource_type="prescription",
                     resource_id=rx_id, patient_id=rx.patient_id, details={"version": version})
        event = "prescription.discontinued" if v.change_type == "discontinue" else "prescription.approved"
        get_bus().publish("prescription-events", event, {
            "prescription_id": rx_id, "patient_id": rx.patient_id, "version": version, "change_type": v.change_type,
            "medication": v.medication, "dosage": v.dosage, "frequency": v.frequency, "expiry_date": v.expiry_date,
            "refills_allowed": v.refills_allowed, "prescriber_id": rx.prescriber_id,
        }, message_id=f"{event}:{rx_id}:v{version}")
        return rx_dict(s, rx, include_history=True)

    @app.post("/prescriptions/{rx_id}/versions/{version}/reject", tags=["prescriptions"])
    def reject(rx_id: str, version: int, body: ReasonIn, p: Principal = Depends(require("prescription:approve")),
               s: Session = Depends(get_db)):
        doctor_only(p)
        rx = load_rx(s, rx_id)
        enforce_patient_access(p, rx.patient_id, scope="clinical", action="prescription.reject.check",
                               resource_type="prescription", resource_id=rx_id)
        v = get_version(s, rx_id, version)
        if not v or v.approval_status != "pending":
            raise HTTPException(409, "no pending version to reject")
        v.approval_status, v.decided_by, v.decided_at = "rejected", p.sub, time.time()
        v.decision_note = clean_text(body.reason, 500)
        if rx.approved_version is None:
            rx.status = "rejected"
        s.commit()
        audit.record("prescription.reject", actor=p.sub, role=p.role, resource_type="prescription",
                     resource_id=rx_id, patient_id=rx.patient_id, details={"version": version})
        return rx_dict(s, rx, include_history=True)

    # Read -----------------------------------------------------------------------------
    @app.get("/patients/{patient_id}/prescriptions", tags=["prescriptions"])
    def list_rx(patient_id: str, p: Principal = Depends(require("prescription:read:self", "prescription:read:care")),
                s: Session = Depends(get_db)):
        enforce_patient_access(p, patient_id, scope="clinical", action="prescription.list", resource_type="prescription")
        rows = s.scalars(select(Prescription).where(Prescription.patient_id == patient_id)
                         .order_by(Prescription.created_at.desc())).all()
        out = [rx_dict(s, r) for r in rows]
        if p.role == "patient":  # patients only see medication that a prescriber has approved
            out = [r for r in out if r["current"]]
            for r in out:
                r["pending_change"] = None
        return out

    @app.get("/prescriptions/{rx_id}", tags=["prescriptions"])
    def get_rx(rx_id: str, p: Principal = Depends(require("prescription:read:self", "prescription:read:care")),
               s: Session = Depends(get_db)):
        rx = load_rx(s, rx_id)
        enforce_patient_access(p, rx.patient_id, scope="clinical", action="prescription.read",
                               resource_type="prescription", resource_id=rx_id)
        return rx_dict(s, rx, include_history=p.role != "patient")

    # Refills --------------------------------------------------------------------------
    @app.post("/prescriptions/{rx_id}/refill-requests", status_code=201, tags=["refills"])
    def request_refill(rx_id: str, body: NoteIn, p: Principal = Depends(require("refill:request:self")),
                       s: Session = Depends(get_db)):
        rx = load_rx(s, rx_id)
        if rx.patient_id != p.patient_id:
            raise deny(p, "refill.request", resource_type="prescription", resource_id=rx_id, patient_id=rx.patient_id)
        refresh_expiry(s, rx)
        ok, why = refill_eligibility(s, rx, get_version(s, rx.id, rx.approved_version))
        if not ok:
            raise HTTPException(409, f"refill not available: {why}")
        req = RefillRequest(id=new_id("RF"), prescription_id=rx_id, patient_id=rx.patient_id,
                            prescriber_id=rx.prescriber_id, note=clean_text(body.note, 500))
        s.add(req)
        s.commit()
        audit.record("refill.request", actor=p.sub, role=p.role, resource_type="refill", resource_id=req.id,
                     patient_id=rx.patient_id)
        return {"id": req.id, "status": req.status}

    @app.get("/refill-requests", tags=["refills"])
    def list_refills(status: str = "requested", p: Principal = Depends(get_principal), s: Session = Depends(get_db)):
        stmt = select(RefillRequest).where(RefillRequest.status == status)
        if p.role == "doctor":
            stmt = stmt.where(RefillRequest.prescriber_id == p.doctor_id)
        elif p.role == "patient":
            stmt = select(RefillRequest).where(RefillRequest.patient_id == p.patient_id)
        else:
            raise deny(p, "refill.list")
        out = []
        for r in s.scalars(stmt.order_by(RefillRequest.requested_at.desc())).all():
            rx = s.get(Prescription, r.prescription_id)
            v = get_version(s, rx.id, rx.approved_version) if rx else None
            out.append({"id": r.id, "prescription_id": r.prescription_id, "patient_id": r.patient_id,
                        "medication": v.medication if v else "", "status": r.status, "note": r.note,
                        "requested_at": r.requested_at})
        return out

    @app.post("/refill-requests/{req_id}/{decision}", tags=["refills"])
    def decide_refill(req_id: str, decision: Literal["approve", "deny"], body: NoteIn,
                      p: Principal = Depends(require("refill:decide")), s: Session = Depends(get_db)):
        doctor_only(p)
        req = s.get(RefillRequest, req_id)
        if not req or req.status != "requested":
            raise HTTPException(404, "pending refill request not found")
        rx = load_rx(s, req.prescription_id)
        enforce_patient_access(p, rx.patient_id, scope="clinical", action=f"refill.{decision}",
                               resource_type="refill", resource_id=req_id)
        current = get_version(s, rx.id, rx.approved_version)
        if decision == "approve":
            # re-check eligibility except for the pending request itself
            if rx.status != "active" or not current or rx.refills_used >= current.refills_allowed:
                raise HTTPException(409, "prescription no longer eligible for refill")
            rx.refills_used += 1
        req.status = "approved" if decision == "approve" else "denied"
        req.decided_by, req.decided_at = p.sub, time.time()
        req.note = clean_text(body.note, 500) or req.note
        s.commit()
        get_bus().publish("prescription-events", f"refill.{req.status}", {
            "prescription_id": rx.id, "refill_request_id": req.id, "patient_id": rx.patient_id,
            "medication": current.medication if current else "",
            "refills_remaining": max((current.refills_allowed if current else 0) - rx.refills_used, 0),
        }, message_id=f"refill.{req.status}:{req.id}")
        return {"id": req.id, "status": req.status, "refills_used": rx.refills_used}

    # FHIR / internal -------------------------------------------------------------------
    @app.get("/fhir/MedicationRequest", tags=["fhir"])
    def fhir_meds(patient: str, p: Principal = Depends(require("prescription:read:self", "prescription:read:care")),
                  s: Session = Depends(get_db)):
        enforce_patient_access(p, patient, scope="clinical", action="fhir.medicationrequest.read",
                               resource_type="prescription")
        entries = []
        for rx in s.scalars(select(Prescription).where(Prescription.patient_id == patient)).all():
            v = get_version(s, rx.id, rx.approved_version)
            if v:
                entries.append({"resource": medication_request(rx, v)})
        return {"resourceType": "Bundle", "type": "searchset", "total": len(entries), "entry": entries}

    @app.get("/internal/prescriptions/expiring", tags=["internal"])
    def expiring(days: int = Query(3, le=30), p: Principal = Depends(require("*")), s: Session = Depends(get_db)):
        limit = (_today() + timedelta(days=days)).isoformat()
        out = []
        for rx in s.scalars(select(Prescription).where(Prescription.status == "active")).all():
            v = get_version(s, rx.id, rx.approved_version)
            if v and _today().isoformat() <= v.expiry_date <= limit:
                out.append({"prescription_id": rx.id, "patient_id": rx.patient_id, "medication": v.medication,
                            "expiry_date": v.expiry_date})
        return out

    return app
