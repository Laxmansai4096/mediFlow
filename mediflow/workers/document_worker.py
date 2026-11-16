"""Document worker - extracts structured data from uploaded medical documents."""
from __future__ import annotations

from mediflow.common import service_client
from mediflow.common.bus import BusMessage, get_bus
from mediflow.common.extraction import get_extractor

from .base import PermanentError, Worker


class DocumentWorker(Worker):
    name = "document"
    subscription = "document"

    def __init__(self):
        super().__init__()
        self.extractor = get_extractor()
        self.on("document.uploaded", self.process)

    def process(self, m: BusMessage) -> None:
        doc_id = m.body.get("document_id")
        if not doc_id:
            raise PermanentError("missing document_id")
        r = service_client.call("document", "GET", f"/internal/documents/{doc_id}/content", as_service="document-worker")
        if r.status_code == 404:
            raise PermanentError(f"document {doc_id} not found")
        r.raise_for_status()
        try:
            extraction = self.extractor.extract(r.content, m.body.get("content_type", "text/plain"))
            status = "processed"
        except Exception as exc:  # unreadable document: record failure, don't retry forever
            extraction, status = {"error": type(exc).__name__}, "failed"
        resp = service_client.call("document", "POST", f"/internal/documents/{doc_id}/extraction",
                                   as_service="document-worker", json={"status": status, "extraction": extraction})
        resp.raise_for_status()
        if status == "processed":
            get_bus().publish("document-processed", "document.processed", {
                "document_id": doc_id, "patient_id": m.body["patient_id"], "doc_type": extraction.get("doc_type"),
                "abnormal_count": extraction.get("abnormal_count", 0),
                "abnormal": [f"{l['test']} {l['value']} {l['unit']} ({l['flag']})"
                             for l in extraction.get("lab_results", []) if l["flag"] != "normal"][:10],
                "lab_count": len(extraction.get("lab_results", [])),
            }, message_id=f"document.processed:{doc_id}")


if __name__ == "__main__":
    DocumentWorker.main()
