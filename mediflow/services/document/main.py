"""Document service - secure upload & storage of medical reports, scans and bills.

Upload path is synchronous only for validation + storage; extraction happens
asynchronously (document.uploaded -> Service Bus -> document worker), so 5,000
simultaneous uploads don't block the API.

Upload hardening: size limit, allow-listed types verified by magic bytes (not
the client-supplied Content-Type), server-generated blob names (no path
traversal), SHA-256 integrity hash, encryption at rest, audited downloads.
"""
from __future__ import annotations

import hashlib
import json
import time

from fastapi import Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import Float, Integer, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from mediflow.common import audit
from mediflow.common.access import enforce_patient_access
from mediflow.common.app_factory import create_service_app
from mediflow.common.auth import Principal, require
from mediflow.common.bus import get_bus
from mediflow.common.db import Database, get_db
from mediflow.common.ids import is_valid_patient_id, new_id
from mediflow.common.security import clean_text
from mediflow.common.storage import get_storage

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
CONTAINER = "medical-documents"
DOC_TYPES = {"lab_report", "prescription", "bill", "discharge_summary", "imaging", "other"}


def sniff(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if b"\x00" not in data[:4096]:
        try:
            data[:4096].decode("utf-8")
            return "text/plain", ".txt"
        except UnicodeDecodeError:
            return None
    return None


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(20), index=True)
    uploaded_by: Mapped[str] = mapped_column(String(40))
    uploader_role: Mapped[str] = mapped_column(String(20))
    filename: Mapped[str] = mapped_column(String(160))
    content_type: Mapped[str] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    blob_path: Mapped[str] = mapped_column(String(200))
    declared_type: Mapped[str] = mapped_column(String(30), default="other")
    detected_type: Mapped[str] = mapped_column(String(30), default="")
    status: Mapped[str] = mapped_column(String(12), default="uploaded")  # uploaded|processed|failed
    extraction: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    processed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class ExtractionIn(BaseModel):
    status: str
    extraction: dict


def doc_dict(d: Document, include_extraction: bool = True) -> dict:
    out = {"id": d.id, "patient_id": d.patient_id, "filename": d.filename, "content_type": d.content_type,
           "size_bytes": d.size_bytes, "sha256": d.sha256, "declared_type": d.declared_type,
           "detected_type": d.detected_type, "status": d.status, "uploaded_by_role": d.uploader_role,
           "created_at": d.created_at, "processed_at": d.processed_at}
    if include_extraction:
        out["extraction"] = json.loads(d.extraction) if d.extraction else None
    return out


def create_app():
    app = create_service_app("document", "Document Service")
    app.state.db = Database("document", Base)

    @app.post("/patients/{patient_id}/documents", status_code=202, tags=["documents"])
    def upload(patient_id: str, file: UploadFile = File(...), doc_type: str = Form("other"),
                     p: Principal = Depends(require("document:upload:self", "document:upload:care")),
                     s: Session = Depends(get_db)):
        if not is_valid_patient_id(patient_id):
            raise HTTPException(422, "invalid patient id")
        enforce_patient_access(p, patient_id, scope="clinical", action="document.upload", resource_type="document")
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file exceeds 5 MB limit")
        if not data:
            raise HTTPException(422, "empty file")
        sniffed = sniff(data)
        if not sniffed:
            audit.record("document.upload.rejected", actor=p.sub, role=p.role, patient_id=patient_id,
                         outcome="denied", severity="warning", details={"reason": "unsupported_type"})
            raise HTTPException(415, "only PDF, PNG, JPEG or plain-text files are accepted")
        content_type, ext = sniffed
        doc_id = new_id("DOC")
        blob_name = f"{patient_id}/{doc_id}{ext}"
        get_storage().upload(CONTAINER, blob_name, data, content_type)
        doc = Document(id=doc_id, patient_id=patient_id, uploaded_by=p.sub, uploader_role=p.role,
                       filename=clean_text(file.filename or "upload", 160).replace("/", "_").replace("\\", "_"),
                       content_type=content_type, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                       blob_path=blob_name, declared_type=doc_type if doc_type in DOC_TYPES else "other")
        s.add(doc)
        s.commit()
        get_bus().publish("document-events", "document.uploaded", {
            "document_id": doc_id, "patient_id": patient_id, "content_type": content_type,
            "declared_type": doc.declared_type,
        }, message_id=f"document.uploaded:{doc_id}")
        return doc_dict(doc)

    @app.get("/patients/{patient_id}/documents", tags=["documents"])
    def list_docs(patient_id: str, p: Principal = Depends(require("document:read:self", "document:read:care")),
                  s: Session = Depends(get_db)):
        enforce_patient_access(p, patient_id, scope="clinical", action="document.list", resource_type="document")
        rows = s.scalars(select(Document).where(Document.patient_id == patient_id)
                         .order_by(Document.created_at.desc())).all()
        return [doc_dict(d) for d in rows]

    @app.get("/documents/{doc_id}", tags=["documents"])
    def get_doc(doc_id: str, p: Principal = Depends(require("document:read:self", "document:read:care")),
                s: Session = Depends(get_db)):
        d = s.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "document not found")
        enforce_patient_access(p, d.patient_id, scope="clinical", action="document.read", resource_type="document",
                               resource_id=doc_id)
        return doc_dict(d)

    @app.get("/documents/{doc_id}/download", tags=["documents"])
    def download(doc_id: str, p: Principal = Depends(require("document:read:self", "document:read:care")),
                 s: Session = Depends(get_db)):
        d = s.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "document not found")
        enforce_patient_access(p, d.patient_id, scope="clinical", action="document.download",
                               resource_type="document", resource_id=doc_id)
        data = get_storage().download(CONTAINER, d.blob_path)
        if hashlib.sha256(data).hexdigest() != d.sha256:
            audit.record("document.integrity_failure", resource_type="document", resource_id=doc_id,
                         patient_id=d.patient_id, outcome="failure", severity="critical")
            raise HTTPException(500, "document integrity check failed")
        return Response(data, media_type=d.content_type,
                        headers={"Content-Disposition": f'attachment; filename="{d.id}"'})

    # Internal --------------------------------------------------------------------------
    @app.get("/internal/documents/{doc_id}/content", tags=["internal"])
    def internal_content(doc_id: str, p: Principal = Depends(require("*")), s: Session = Depends(get_db)):
        d = s.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "document not found")
        return Response(get_storage().download(CONTAINER, d.blob_path), media_type=d.content_type)

    @app.post("/internal/documents/{doc_id}/extraction", tags=["internal"])
    def internal_extraction(doc_id: str, body: ExtractionIn, p: Principal = Depends(require("*")),
                            s: Session = Depends(get_db)):
        d = s.get(Document, doc_id)
        if not d:
            raise HTTPException(404, "document not found")
        d.status = body.status
        d.extraction = json.dumps(body.extraction)
        d.detected_type = body.extraction.get("doc_type", "")
        d.processed_at = time.time()
        s.commit()
        return {"ok": True}

    return app
