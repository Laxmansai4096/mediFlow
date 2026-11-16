"""Appointment service - doctors, availability, slots and bookings.

Double-booking is prevented at two levels:
  1. application check (friendly 409 message)
  2. a filtered UNIQUE index on (doctor_id, start_at) WHERE status='booked',
     so two concurrent requests for the same slot cannot both succeed - the
     database is the final arbiter even with many API replicas.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Float, Index, Integer, String, Text, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from mediflow.common import audit
from mediflow.common.access import enforce_patient_access
from mediflow.common.app_factory import create_service_app
from mediflow.common.auth import Principal, deny, get_principal, require
from mediflow.common.bus import get_bus
from mediflow.common.db import Database, get_db
from mediflow.common.ids import is_valid_patient_id, new_id
from mediflow.common.security import clean_text

TIME_FMT = "%Y-%m-%dT%H:%M"


class Base(DeclarativeBase):
    pass


class Doctor(Base):
    __tablename__ = "doctors"
    doctor_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    specialty: Mapped[str] = mapped_column(String(60), index=True)
    qualifications: Mapped[str] = mapped_column(String(200), default="")
    languages: Mapped[str] = mapped_column(String(60), default="en")
    slot_minutes: Mapped[int] = mapped_column(Integer, default=20)
    fee_paise: Mapped[int] = mapped_column(Integer, default=50000)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Availability(Base):
    __tablename__ = "availability"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doctor_id: Mapped[str] = mapped_column(String(20), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    start: Mapped[str] = mapped_column(String(5))  # "09:00"
    end: Mapped[str] = mapped_column(String(5))


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        Index("uq_doctor_slot_booked", "doctor_id", "start_at", unique=True,
              sqlite_where=text("status = 'booked'"), mssql_where=text("status = 'booked'")),
        Index("ix_appt_patient", "patient_id", "start_at"),
    )
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(20))
    doctor_id: Mapped[str] = mapped_column(String(20))
    start_at: Mapped[str] = mapped_column(String(16))
    end_at: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12), default="booked")  # booked|completed|cancelled|no_show
    reason: Mapped[str] = mapped_column(Text, default="")
    visit_notes: Mapped[str] = mapped_column(Text, default="")
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    booked_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class DoctorIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    specialty: str = Field(min_length=2, max_length=60)
    qualifications: str = Field(default="", max_length=200)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    slot_minutes: int = Field(default=20, ge=10, le=60)
    fee_paise: int = Field(default=50000, ge=0, le=10_000_000)


class AvailabilityIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class BookIn(BaseModel):
    doctor_id: str = Field(max_length=20)
    start_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
    reason: str = Field(default="", max_length=500)
    patient_id: str | None = None  # front-desk (admin) booking on behalf of a patient


class CancelIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class CompleteIn(BaseModel):
    visit_notes: str = Field(default="", max_length=4000)


def doctor_dict(d: Doctor) -> dict:
    return {"doctor_id": d.doctor_id, "name": d.name, "specialty": d.specialty, "qualifications": d.qualifications,
            "languages": d.languages.split(","), "slot_minutes": d.slot_minutes, "fee_paise": d.fee_paise,
            "active": d.active}


def appt_dict(a: Appointment, doctors: dict[str, Doctor] | None = None) -> dict:
    d = (doctors or {}).get(a.doctor_id)
    return {"id": a.id, "patient_id": a.patient_id, "doctor_id": a.doctor_id,
            "doctor_name": d.name if d else None, "specialty": d.specialty if d else None,
            "start_at": a.start_at, "end_at": a.end_at, "status": a.status, "reason": a.reason,
            "visit_notes": a.visit_notes, "cancel_reason": a.cancel_reason}


def generate_slots(s: Session, doctor: Doctor, day: date) -> list[str]:
    windows = s.scalars(select(Availability).where(Availability.doctor_id == doctor.doctor_id,
                                                   Availability.weekday == day.weekday())).all()
    slots = []
    for w in windows:
        cur = datetime.combine(day, datetime.strptime(w.start, "%H:%M").time())
        end = datetime.combine(day, datetime.strptime(w.end, "%H:%M").time())
        while cur + timedelta(minutes=doctor.slot_minutes) <= end:
            slots.append(cur.strftime(TIME_FMT))
            cur += timedelta(minutes=doctor.slot_minutes)
    return sorted(slots)


def create_app():
    app = create_service_app("appointment", "Appointment Service")
    app.state.db = Database("appointment", Base)

    def doctors_map(s: Session) -> dict[str, Doctor]:
        return {d.doctor_id: d for d in s.scalars(select(Doctor)).all()}

    # Doctors ---------------------------------------------------------------------------
    @app.get("/doctors", tags=["doctors"])
    def list_doctors(specialty: str | None = None, s: Session = Depends(get_db),
                     p: Principal = Depends(get_principal)):
        stmt = select(Doctor).where(Doctor.active.is_(True))
        if specialty:
            stmt = stmt.where(Doctor.specialty == specialty)
        return [doctor_dict(d) for d in s.scalars(stmt.order_by(Doctor.specialty, Doctor.name)).all()]

    @app.get("/specialties", tags=["doctors"])
    def specialties(s: Session = Depends(get_db), p: Principal = Depends(get_principal)):
        return sorted({d.specialty for d in s.scalars(select(Doctor).where(Doctor.active.is_(True))).all()})

    @app.post("/doctors", status_code=201, tags=["doctors"])
    def create_doctor(body: DoctorIn, p: Principal = Depends(require("doctor:manage")), s: Session = Depends(get_db)):
        d = Doctor(doctor_id=new_id("DR")[:15], name=clean_text(body.name, 120), specialty=clean_text(body.specialty, 60),
                   qualifications=clean_text(body.qualifications, 200), languages=",".join(body.languages),
                   slot_minutes=body.slot_minutes, fee_paise=body.fee_paise)
        s.add(d)
        s.commit()
        audit.record("doctor.create", actor=p.sub, role=p.role, resource_type="doctor", resource_id=d.doctor_id)
        return doctor_dict(d)

    @app.put("/doctors/{doctor_id}/availability", tags=["doctors"])
    def set_availability(doctor_id: str, body: list[AvailabilityIn], p: Principal = Depends(require("doctor:manage")),
                         s: Session = Depends(get_db)):
        if not s.get(Doctor, doctor_id):
            raise HTTPException(404, "doctor not found")
        for w in body:
            if w.start >= w.end:
                raise HTTPException(422, "availability start must be before end")
        for old in s.scalars(select(Availability).where(Availability.doctor_id == doctor_id)).all():
            s.delete(old)
        for w in body:
            s.add(Availability(doctor_id=doctor_id, **w.model_dump()))
        s.commit()
        audit.record("doctor.availability.update", actor=p.sub, role=p.role, resource_type="doctor", resource_id=doctor_id)
        return {"ok": True, "windows": len(body)}

    @app.get("/doctors/{doctor_id}/availability", tags=["doctors"])
    def get_availability(doctor_id: str, s: Session = Depends(get_db), p: Principal = Depends(get_principal)):
        rows = s.scalars(select(Availability).where(Availability.doctor_id == doctor_id)
                         .order_by(Availability.weekday, Availability.start)).all()
        return [{"weekday": r.weekday, "start": r.start, "end": r.end} for r in rows]

    @app.get("/doctors/{doctor_id}/slots", tags=["doctors"])
    def free_slots(doctor_id: str, day: date = Query(alias="date"), s: Session = Depends(get_db),
                   p: Principal = Depends(get_principal)):
        doctor = s.get(Doctor, doctor_id)
        if not doctor or not doctor.active:
            raise HTTPException(404, "doctor not found")
        booked = set(s.scalars(select(Appointment.start_at).where(
            Appointment.doctor_id == doctor_id, Appointment.status == "booked",
            Appointment.start_at.like(f"{day.isoformat()}%"))).all())
        now = datetime.now().strftime(TIME_FMT)
        return {"doctor_id": doctor_id, "date": day.isoformat(),
                "slots": [x for x in generate_slots(s, doctor, day) if x not in booked and x > now]}

    # Appointments ----------------------------------------------------------------------
    @app.post("/appointments", status_code=201, tags=["appointments"])
    def book(body: BookIn, p: Principal = Depends(require("appointment:book:self", "appointment:book:any")),
             s: Session = Depends(get_db)):
        if p.role == "patient":
            patient_id = p.patient_id or ""
        else:
            patient_id = body.patient_id or ""
            if not is_valid_patient_id(patient_id):
                raise HTTPException(422, "valid patient_id required")
        doctor = s.get(Doctor, body.doctor_id)
        if not doctor or not doctor.active:
            raise HTTPException(404, "doctor not found")
        start = datetime.strptime(body.start_at, TIME_FMT)
        if start <= datetime.now():
            raise HTTPException(422, "appointment must be in the future")
        if body.start_at not in generate_slots(s, doctor, start.date()):
            raise HTTPException(422, "requested time is not an available slot for this doctor")
        end_at = (start + timedelta(minutes=doctor.slot_minutes)).strftime(TIME_FMT)
        clash = s.scalar(select(Appointment.id).where(
            Appointment.patient_id == patient_id, Appointment.status == "booked",
            Appointment.start_at < end_at, Appointment.end_at > body.start_at))
        if clash:
            raise HTTPException(409, "you already have an appointment at this time")
        appt = Appointment(id=new_id("AP"), patient_id=patient_id, doctor_id=doctor.doctor_id, start_at=body.start_at,
                           end_at=end_at, reason=clean_text(body.reason, 500), booked_by=p.sub)
        s.add(appt)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise HTTPException(409, "this slot has just been booked; please choose another")
        audit.record("appointment.book", actor=p.sub, role=p.role, resource_type="appointment",
                     resource_id=appt.id, patient_id=patient_id)
        get_bus().publish("appointment-events", "appointment.booked", {
            "appointment_id": appt.id, "patient_id": patient_id, "doctor_id": doctor.doctor_id,
            "doctor_name": doctor.name, "specialty": doctor.specialty, "start_at": appt.start_at,
            "fee_paise": doctor.fee_paise,
        }, message_id=f"appointment.booked:{appt.id}")
        return appt_dict(appt, {doctor.doctor_id: doctor})

    @app.get("/appointments", tags=["appointments"])
    def list_appointments(status: str | None = None, day: date | None = Query(None, alias="date"),
                          p: Principal = Depends(require("appointment:read:self", "appointment:read:own",
                                                         "appointment:read:any")),
                          s: Session = Depends(get_db)):
        stmt = select(Appointment)
        if p.role == "patient":
            stmt = stmt.where(Appointment.patient_id == p.patient_id)
        elif p.role == "doctor":
            stmt = stmt.where(Appointment.doctor_id == p.doctor_id)
        if status:
            stmt = stmt.where(Appointment.status == status)
        if day:
            stmt = stmt.where(Appointment.start_at.like(f"{day.isoformat()}%"))
        rows = s.scalars(stmt.order_by(Appointment.start_at.desc()).limit(200)).all()
        return [appt_dict(a, doctors_map(s)) for a in rows]

    @app.get("/patients/{patient_id}/visit-history", tags=["appointments"])
    def visit_history(patient_id: str, p: Principal = Depends(require("appointment:read:self", "clinical:read:care")),
                      s: Session = Depends(get_db)):
        enforce_patient_access(p, patient_id, scope="clinical", action="visits.read", resource_type="appointment")
        rows = s.scalars(select(Appointment).where(Appointment.patient_id == patient_id)
                         .order_by(Appointment.start_at.desc())).all()
        return [appt_dict(a, doctors_map(s)) for a in rows]

    def _get_owned(s: Session, appt_id: str, p: Principal) -> Appointment:
        appt = s.get(Appointment, appt_id)
        if not appt:
            raise HTTPException(404, "appointment not found")
        owner = (p.role == "patient" and appt.patient_id == p.patient_id) or \
                (p.role == "doctor" and appt.doctor_id == p.doctor_id) or p.role in ("admin", "service")
        if not owner:
            raise deny(p, "appointment.modify", resource_type="appointment", resource_id=appt_id,
                       patient_id=appt.patient_id)
        return appt

    @app.post("/appointments/{appt_id}/cancel", tags=["appointments"])
    def cancel(appt_id: str, body: CancelIn, p: Principal = Depends(get_principal), s: Session = Depends(get_db)):
        appt = _get_owned(s, appt_id, p)
        if appt.status != "booked":
            raise HTTPException(409, f"cannot cancel an appointment that is {appt.status}")
        appt.status, appt.cancel_reason, appt.updated_at = "cancelled", clean_text(body.reason, 500), time.time()
        s.commit()
        audit.record("appointment.cancel", actor=p.sub, role=p.role, resource_type="appointment",
                     resource_id=appt.id, patient_id=appt.patient_id)
        d = s.get(Doctor, appt.doctor_id)
        get_bus().publish("appointment-events", "appointment.cancelled", {
            "appointment_id": appt.id, "patient_id": appt.patient_id, "doctor_id": appt.doctor_id,
            "doctor_name": d.name if d else "", "start_at": appt.start_at, "cancelled_by": p.role,
        }, message_id=f"appointment.cancelled:{appt.id}")
        return appt_dict(appt, doctors_map(s))

    def _doctor_transition(appt_id: str, p: Principal, s: Session, new_status: str, notes: str = "") -> dict:
        if p.role != "doctor":
            raise deny(p, f"appointment.{new_status}", resource_type="appointment", resource_id=appt_id)
        appt = _get_owned(s, appt_id, p)
        if appt.status != "booked":
            raise HTTPException(409, f"appointment is already {appt.status}")
        appt.status, appt.visit_notes, appt.updated_at = new_status, clean_text(notes, 4000), time.time()
        s.commit()
        audit.record(f"appointment.{new_status}", actor=p.sub, role=p.role, resource_type="appointment",
                     resource_id=appt.id, patient_id=appt.patient_id)
        d = s.get(Doctor, appt.doctor_id)
        get_bus().publish("appointment-events", f"appointment.{new_status}", {
            "appointment_id": appt.id, "patient_id": appt.patient_id, "doctor_id": appt.doctor_id,
            "doctor_name": d.name if d else "", "specialty": d.specialty if d else "",
            "start_at": appt.start_at, "visit_notes": appt.visit_notes,
        }, message_id=f"appointment.{new_status}:{appt.id}")
        return appt_dict(appt, doctors_map(s))

    @app.post("/appointments/{appt_id}/complete", tags=["appointments"])
    def complete(appt_id: str, body: CompleteIn, p: Principal = Depends(require("appointment:manage:own")),
                 s: Session = Depends(get_db)):
        return _doctor_transition(appt_id, p, s, "completed", body.visit_notes)

    @app.post("/appointments/{appt_id}/no-show", tags=["appointments"])
    def no_show(appt_id: str, p: Principal = Depends(require("appointment:manage:own")), s: Session = Depends(get_db)):
        return _doctor_transition(appt_id, p, s, "no_show")

    # Internal ----------------------------------------------------------------------------
    @app.get("/internal/appointments/upcoming", tags=["internal"])
    def upcoming(hours: int = Query(24, le=168), p: Principal = Depends(require("*")), s: Session = Depends(get_db)):
        now = datetime.now()
        rows = s.scalars(select(Appointment).where(
            Appointment.status == "booked", Appointment.start_at >= now.strftime(TIME_FMT),
            Appointment.start_at <= (now + timedelta(hours=hours)).strftime(TIME_FMT))).all()
        return [appt_dict(a, doctors_map(s)) for a in rows]

    return app
