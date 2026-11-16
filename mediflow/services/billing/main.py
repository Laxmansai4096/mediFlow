"""Billing service - invoices, payments and verified payment events.

Financial safety:
* Idempotency-Key header on /pay: retries from a flaky mobile network return
  the original payment instead of charging twice.
* Payment success is accepted only from an HMAC-verified gateway callback.
* Replayed callbacks are no-ops; payment.succeeded is published with a
  deterministic message_id so Service Bus duplicate detection drops repeats,
  and downstream consumers are idempotent as a third safeguard.
* Billing staff see amounts and status - never diagnoses or clinical notes.
"""
from __future__ import annotations

import time
from typing import Literal

from fastapi import Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Float, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from mediflow.common import audit
from mediflow.common.access import enforce_patient_access
from mediflow.common.app_factory import create_service_app
from mediflow.common.auth import Principal, deny, require
from mediflow.common.bus import get_bus
from mediflow.common.db import Database, get_db
from mediflow.common.ids import is_valid_patient_id, new_id
from mediflow.common.security import clean_text

from . import gateway


class Base(DeclarativeBase):
    pass


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("source_type", "source_id", name="uq_invoice_source"),)
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(20), index=True)
    description: Mapped[str] = mapped_column(String(200))
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[str] = mapped_column(String(10), default="open")  # open|paid|void
    source_type: Mapped[str] = mapped_column(String(20), default="manual")
    source_id: Mapped[str] = mapped_column(String(40))
    created_by: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    paid_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(20), index=True)
    patient_id: Mapped[str] = mapped_column(String(20))
    amount_paise: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending|succeeded|failed
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    provider_ref: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    verified_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class InvoiceIn(BaseModel):
    patient_id: str
    description: str = Field(min_length=2, max_length=200)
    amount_paise: int = Field(gt=0, le=100_000_000)


class InternalInvoiceIn(InvoiceIn):
    source_type: str = Field(max_length=20)
    source_id: str = Field(max_length=40)


class PayIn(BaseModel):
    method: Literal["upi", "card", "netbanking", "cash", "test_decline"] = "upi"


class WebhookIn(BaseModel):
    payment_id: str
    provider_ref: str
    status: Literal["succeeded", "failed"]
    amount_paise: int
    signature: str


def invoice_dict(i: Invoice) -> dict:
    return {"id": i.id, "patient_id": i.patient_id, "description": i.description, "amount_paise": i.amount_paise,
            "amount": f"{i.amount_paise / 100:.2f}", "currency": i.currency, "status": i.status,
            "source_type": i.source_type, "created_at": i.created_at, "paid_at": i.paid_at}


def payment_dict(p: Payment) -> dict:
    return {"id": p.id, "invoice_id": p.invoice_id, "amount_paise": p.amount_paise, "method": p.method,
            "status": p.status, "provider_ref": p.provider_ref, "failure_reason": p.failure_reason,
            "created_at": p.created_at, "verified_at": p.verified_at}


def create_app():
    app = create_service_app("billing", "Billing Service")
    app.state.db = Database("billing", Base)

    def process_gateway_result(s: Session, body: WebhookIn) -> tuple[Payment, bool]:
        """Shared by the synchronous pay flow and the async webhook. Returns (payment, changed)."""
        if not gateway.verify(body.payment_id, body.provider_ref, body.status, body.amount_paise, body.signature):
            audit.record("payment.webhook.invalid_signature", actor="payment-gateway", outcome="denied",
                         severity="high", resource_type="payment", resource_id=body.payment_id)
            raise HTTPException(401, "invalid signature")
        payment = s.get(Payment, body.payment_id)
        if not payment or payment.amount_paise != body.amount_paise:
            raise HTTPException(404, "payment not found")
        if payment.status != "pending":
            return payment, False  # replayed callback: idempotent no-op
        invoice = s.get(Invoice, payment.invoice_id)
        payment.provider_ref, payment.verified_at = body.provider_ref, time.time()
        if body.status == "succeeded":
            payment.status = "succeeded"
            invoice.status, invoice.paid_at = "paid", time.time()
        else:
            payment.status, payment.failure_reason = "failed", "declined by provider"
        s.commit()
        audit.record(f"payment.{payment.status}", actor="payment-gateway", resource_type="payment",
                     resource_id=payment.id, patient_id=payment.patient_id,
                     severity="info" if payment.status == "succeeded" else "warning")
        if payment.status == "succeeded":
            get_bus().publish("payment-events", "payment.succeeded", {
                "payment_id": payment.id, "invoice_id": invoice.id, "patient_id": payment.patient_id,
                "amount_paise": payment.amount_paise, "currency": invoice.currency, "method": payment.method,
                "description": invoice.description, "provider_ref": payment.provider_ref,
            }, message_id=f"payment.succeeded:{payment.id}")
        return payment, True

    # Invoices --------------------------------------------------------------------------
    @app.post("/invoices", status_code=201, tags=["invoices"])
    def create_invoice(body: InvoiceIn, p: Principal = Depends(require("billing:write")), s: Session = Depends(get_db)):
        if not is_valid_patient_id(body.patient_id):
            raise HTTPException(422, "invalid patient id")
        inv = Invoice(id=new_id("INV"), patient_id=body.patient_id, description=clean_text(body.description, 200),
                      amount_paise=body.amount_paise, source_type="manual", source_id=new_id("MAN"), created_by=p.sub)
        s.add(inv)
        s.commit()
        audit.record("invoice.create", actor=p.sub, role=p.role, resource_type="invoice", resource_id=inv.id,
                     patient_id=inv.patient_id)
        return invoice_dict(inv)

    @app.get("/invoices", tags=["invoices"])
    def list_invoices(status: str | None = None, patient_id: str | None = None,
                      p: Principal = Depends(require("billing:read:self", "billing:read:any")),
                      s: Session = Depends(get_db)):
        stmt = select(Invoice)
        if p.role == "patient":
            stmt = stmt.where(Invoice.patient_id == p.patient_id)
        elif patient_id:
            stmt = stmt.where(Invoice.patient_id == patient_id)
        if status:
            stmt = stmt.where(Invoice.status == status)
        return [invoice_dict(i) for i in s.scalars(stmt.order_by(Invoice.created_at.desc()).limit(300)).all()]

    @app.get("/invoices/{invoice_id}", tags=["invoices"])
    def get_invoice(invoice_id: str, p: Principal = Depends(require("billing:read:self", "billing:read:any")),
                    s: Session = Depends(get_db)):
        inv = s.get(Invoice, invoice_id)
        if not inv:
            raise HTTPException(404, "invoice not found")
        enforce_patient_access(p, inv.patient_id, scope="billing", action="invoice.read", resource_type="invoice",
                               resource_id=invoice_id)
        payments = s.scalars(select(Payment).where(Payment.invoice_id == invoice_id)).all()
        return invoice_dict(inv) | {"payments": [payment_dict(x) for x in payments]}

    @app.post("/invoices/{invoice_id}/void", tags=["invoices"])
    def void_invoice(invoice_id: str, p: Principal = Depends(require("billing:write")), s: Session = Depends(get_db)):
        inv = s.get(Invoice, invoice_id)
        if not inv:
            raise HTTPException(404, "invoice not found")
        if inv.status != "open":
            raise HTTPException(409, f"cannot void a {inv.status} invoice")
        inv.status = "void"
        s.commit()
        audit.record("invoice.void", actor=p.sub, role=p.role, resource_type="invoice", resource_id=inv.id,
                     patient_id=inv.patient_id, severity="warning")
        return invoice_dict(inv)

    # Payments --------------------------------------------------------------------------
    @app.post("/invoices/{invoice_id}/pay", tags=["payments"])
    def pay(invoice_id: str, body: PayIn,
            idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=80),
            p: Principal = Depends(require("billing:pay:self", "billing:write")), s: Session = Depends(get_db)):
        existing = s.scalar(select(Payment).where(Payment.idempotency_key == idempotency_key))
        if existing:
            if existing.invoice_id != invoice_id:
                raise HTTPException(409, "idempotency key reused for a different invoice")
            return payment_dict(existing) | {"idempotent_replay": True}
        inv = s.get(Invoice, invoice_id)
        if not inv:
            raise HTTPException(404, "invoice not found")
        if p.role == "patient" and inv.patient_id != p.patient_id:
            raise deny(p, "invoice.pay", resource_type="invoice", resource_id=invoice_id, patient_id=inv.patient_id)
        if inv.status != "open":
            raise HTTPException(409, f"invoice is {inv.status}")
        if body.method == "cash" and p.role != "billing":
            raise HTTPException(422, "cash payments are recorded by the billing desk")
        payment = Payment(id=new_id("PAY"), invoice_id=inv.id, patient_id=inv.patient_id, amount_paise=inv.amount_paise,
                          method=body.method, idempotency_key=idempotency_key, created_by=p.sub)
        s.add(payment)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            existing = s.scalar(select(Payment).where(Payment.idempotency_key == idempotency_key))
            return payment_dict(existing) | {"idempotent_replay": True}
        result = gateway.charge(payment.id, payment.amount_paise, body.method)
        payment, _ = process_gateway_result(s, WebhookIn(**result))
        return payment_dict(payment) | {"idempotent_replay": False}

    @app.post("/payments/webhook", tags=["payments"])
    def webhook(body: WebhookIn, s: Session = Depends(get_db)):
        """Gateway callback - authenticated by HMAC signature, not by user token."""
        payment, changed = process_gateway_result(s, body)
        return {"payment_id": payment.id, "status": payment.status, "duplicate": not changed}

    @app.get("/billing/summary", tags=["reports"])
    def summary(p: Principal = Depends(require("billing:read:any")), s: Session = Depends(get_db)):
        rows = s.execute(select(Invoice.status, func.count(), func.coalesce(func.sum(Invoice.amount_paise), 0))
                         .group_by(Invoice.status)).all()
        pay = s.execute(select(Payment.status, func.count()).group_by(Payment.status)).all()
        return {"invoices": {st: {"count": c, "amount_paise": a} for st, c, a in rows},
                "payments": {st: c for st, c in pay}}

    # Internal --------------------------------------------------------------------------
    @app.post("/internal/invoices", tags=["internal"])
    def internal_invoice(body: InternalInvoiceIn, p: Principal = Depends(require("*")), s: Session = Depends(get_db)):
        existing = s.scalar(select(Invoice).where(Invoice.source_type == body.source_type,
                                                  Invoice.source_id == body.source_id))
        if existing:
            return invoice_dict(existing) | {"created": False}
        inv = Invoice(id=new_id("INV"), patient_id=body.patient_id, description=clean_text(body.description, 200),
                      amount_paise=body.amount_paise, source_type=body.source_type, source_id=body.source_id,
                      created_by=p.sub)
        s.add(inv)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return invoice_dict(s.scalar(select(Invoice).where(Invoice.source_type == body.source_type,
                                                               Invoice.source_id == body.source_id))) | {"created": False}
        return invoice_dict(inv) | {"created": True}

    @app.post("/internal/invoices/void-by-source", tags=["internal"])
    def internal_void(source_type: str = Query(), source_id: str = Query(), p: Principal = Depends(require("*")),
                      s: Session = Depends(get_db)):
        inv = s.scalar(select(Invoice).where(Invoice.source_type == source_type, Invoice.source_id == source_id))
        if inv and inv.status == "open":
            inv.status = "void"
            s.commit()
        return {"ok": True}

    return app
