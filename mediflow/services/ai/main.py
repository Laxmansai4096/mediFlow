"""AI service - clinical history summarization and the multilingual assistant.

All patient data is fetched by forwarding the caller's own token to the owning
services, so the AI layer can never see more than the caller is allowed to.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Float, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from mediflow.common import ai, audit, config, service_client
from mediflow.common.app_factory import create_service_app
from mediflow.common.auth import Principal, deny, optional_principal, require
from mediflow.common.db import Database, get_db
from mediflow.common.ids import new_id
from mediflow.common.security import SlidingWindowRateLimiter, clean_text


class Base(DeclarativeBase):
    pass


class Summary(Base):
    __tablename__ = "summaries"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(20), index=True)
    requested_by: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(60))
    content: Mapped[str] = mapped_column(Text)
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft|approved|rejected
    reviewed_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    reviewed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    notes: str = Field(default="", max_length=2000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    language: Literal["auto", "en", "hi", "te"] = "auto"


def summary_dict(s: Summary) -> dict:
    return {"id": s.id, "patient_id": s.patient_id, "model": s.model, "status": s.status,
            "content": json.loads(s.content), "disclaimer": ai.DISCLAIMER, "requested_by": s.requested_by,
            "reviewed_by": s.reviewed_by, "review_notes": s.review_notes, "created_at": s.created_at,
            "reviewed_at": s.reviewed_at}


def create_app():
    app = create_service_app("ai", "AI Service")
    app.state.db = Database("ai", Base)
    chat_limiter = SlidingWindowRateLimiter(limit=20, window_seconds=60)

    def fetch(service: str, path: str, token: str, params: dict | None = None):
        try:
            r = service_client.call(service, "GET", path, token=token, params=params)
        except service_client.ServiceUnavailable:
            raise HTTPException(503, f"{service} service unavailable")
        if r.status_code in (401, 403):
            raise HTTPException(r.status_code, "not authorized for this patient")
        if r.status_code != 200:
            raise HTTPException(502, f"{service} returned {r.status_code}")
        return r.json()

    # Summaries -------------------------------------------------------------------------
    @app.post("/ai/patients/{patient_id}/summary", status_code=201, tags=["summaries"])
    def summarize(patient_id: str, p: Principal = Depends(require("clinical:summarize")), s: Session = Depends(get_db)):
        patient = fetch("patient", f"/patients/{patient_id}", p.token)
        if "allergies" not in patient:  # demographics-only view => no clinical access
            raise deny(p, "ai.summary", patient_id=patient_id)
        timeline = fetch("patient", f"/patients/{patient_id}/timeline", p.token)
        prescriptions = fetch("prescription", f"/patients/{patient_id}/prescriptions", p.token)
        documents = fetch("document", f"/patients/{patient_id}/documents", p.token)
        visits = fetch("appointment", f"/patients/{patient_id}/visit-history", p.token)
        for d in documents:
            d["created_at_iso"] = ai.iso(d["created_at"])

        allowed = {("patient", patient_id)} | {("timeline", e["id"]) for e in timeline} | \
                  {("prescription", r["id"]) for r in prescriptions} | {("document", d["id"]) for d in documents} | \
                  {("appointment", v["id"]) for v in visits}
        if config.mode("ai") == "azure":  # pragma: no cover
            content = ai.AzureOpenAISummarizer().summarize({"patient": patient, "timeline": timeline,
                                                            "prescriptions": prescriptions, "documents": documents,
                                                            "visits": visits})
        else:
            content = ai.build_local_summary(patient, timeline, prescriptions, documents, visits)
        content, dropped = ai.validate_grounding(content, allowed)
        content["grounding"] = {"sources_available": len(allowed), "ungrounded_bullets_dropped": dropped}
        fingerprint = hashlib.sha256(json.dumps(sorted(allowed)).encode()).hexdigest()
        summary = Summary(id=new_id("SUM"), patient_id=patient_id, requested_by=p.sub, model=ai.model_name(),
                          content=json.dumps(content), source_fingerprint=fingerprint)
        s.add(summary)
        s.commit()
        audit.record("ai.summary.generate", actor=p.sub, role=p.role, resource_type="summary",
                     resource_id=summary.id, patient_id=patient_id, details={"model": summary.model})
        return summary_dict(summary)

    @app.get("/ai/patients/{patient_id}/summaries", tags=["summaries"])
    def list_summaries(patient_id: str, p: Principal = Depends(require("clinical:summarize")),
                       s: Session = Depends(get_db)):
        fetch("patient", f"/patients/{patient_id}/timeline", p.token, {"limit": 1})  # access check via owner
        rows = s.scalars(select(Summary).where(Summary.patient_id == patient_id)
                         .order_by(Summary.created_at.desc()).limit(20)).all()
        return [summary_dict(r) for r in rows]

    @app.post("/ai/summaries/{summary_id}/review", tags=["summaries"])
    def review(summary_id: str, body: ReviewIn, p: Principal = Depends(require("clinical:summarize")),
               s: Session = Depends(get_db)):
        summary = s.get(Summary, summary_id)
        if not summary:
            raise HTTPException(404, "summary not found")
        fetch("patient", f"/patients/{summary.patient_id}/timeline", p.token, {"limit": 1})
        if summary.status != "draft":
            raise HTTPException(409, f"summary already {summary.status}")
        summary.status = "approved" if body.decision == "approve" else "rejected"
        summary.reviewed_by, summary.reviewed_at = p.sub, time.time()
        summary.review_notes = clean_text(body.notes, 2000)
        s.commit()
        audit.record(f"ai.summary.{summary.status}", actor=p.sub, role=p.role, resource_type="summary",
                     resource_id=summary.id, patient_id=summary.patient_id)
        return summary_dict(summary)

    # Chat ------------------------------------------------------------------------------
    @app.post("/ai/chat", tags=["assistant"])
    def chat(body: ChatIn, request: Request, p: Principal | None = Depends(optional_principal)):
        key = p.sub if p else (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "anon"))
        allowed, retry = chat_limiter.allow(key)
        if not allowed:
            raise HTTPException(429, "too many messages, slow down", headers={"Retry-After": str(retry)})
        message = clean_text(body.message, 1000)
        lang = ai.detect_language(message) if body.language == "auto" else body.language
        actor = p.sub if p else "anonymous"

        safety = ai.safety_check(message, lang)
        if not safety and p and p.role == "patient":
            ids = set(ai.PATIENT_ID_IN_TEXT.findall(message))
            if ids - {p.patient_id}:
                safety = {"reply": ai.msg("blocked", lang), "intent": "blocked", "safety": "cross_patient_request"}
        if safety:
            if safety["safety"] in ("prompt_injection_blocked", "cross_patient_request"):
                audit.record("ai.chat.blocked", actor=actor, role=p.role if p else "", outcome="denied",
                             severity="warning", details={"reason": safety["safety"]})
            return {**safety, "language": lang, "sources": []}

        intent, personal = ai.classify_chat(message)
        if intent != "general" and personal:
            if not p or p.role != "patient":
                return {"reply": ai.msg("login_required", lang, topic=ai.TOPIC_NAMES[intent][lang]),
                        "intent": intent, "language": lang, "requires_auth": True, "sources": []}
            if intent == "appointments":
                data = fetch("appointment", "/appointments", p.token)
            elif intent == "bills":
                data = fetch("billing", "/invoices", p.token)
            else:
                data = fetch("prescription", f"/patients/{p.patient_id}/prescriptions", p.token)
            audit.record("ai.chat.personal_lookup", actor=p.sub, role=p.role, patient_id=p.patient_id or "",
                         details={"intent": intent})
            return {"reply": ai.format_personal(intent, lang, data), "intent": intent, "language": lang,
                    "sources": [{"id": f"{intent}-service", "title": "Your records"}]}

        result = ai.answer_general(message, lang)
        if config.mode("ai") == "azure" and result["sources"]:  # pragma: no cover
            hits = ai.knowledge_index().search(message, lang=lang, top=3)
            result["reply"] = ai.AzureOpenAIChat().generate(message, lang, hits, None)
        return {**result, "language": lang}

    return app
