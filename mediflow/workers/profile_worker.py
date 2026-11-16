"""Profile worker - keeps the Unified Patient Profile timeline in sync.

Listens to events from every service and links them to the patient's ID:
appointments -> care team + visit history, documents -> lab results,
prescriptions -> medication history, payments -> billing history.
"""
from __future__ import annotations

from mediflow.common import service_client
from mediflow.common.bus import BusMessage

from .base import PermanentError, Worker


class ProfileWorker(Worker):
    name = "profile"
    subscription = "profile"

    def __init__(self):
        super().__init__()
        self.on("patient.registered", lambda m: None)  # timeline entry already written at registration
        self.on("appointment.booked", self.appointment_booked)
        self.on("appointment.completed", self.appointment_completed)
        self.on("appointment.cancelled", self.simple("appointment", "Appointment cancelled ({start_at})"))
        self.on("appointment.no_show", self.simple("appointment", "Missed appointment ({start_at})"))
        self.on("document.processed", self.document_processed)
        self.on("prescription.approved", self.prescription)
        self.on("prescription.discontinued", self.prescription)
        self.on("refill.approved", self.simple("prescription", "Refill approved: {medication}"))
        self.on("payment.succeeded", self.simple("payment", "Payment received: {description}"))

    def _post(self, m: BusMessage, kind: str, title: str, detail: dict) -> None:
        pid = m.body.get("patient_id")
        if not pid:
            raise PermanentError("missing patient_id")
        r = service_client.call("patient", "POST", f"/internal/patients/{pid}/timeline", as_service="profile-worker",
                                json={"kind": kind, "title": title[:200], "detail": detail,
                                      "source_service": m.topic, "source_id": m.message_id})
        if r.status_code == 404:
            raise PermanentError(f"unknown patient {pid}")
        r.raise_for_status()

    def simple(self, kind: str, title_tpl: str):
        def handler(m: BusMessage) -> None:
            self._post(m, kind, title_tpl.format_map({**m.body, "start_at": str(m.body.get("start_at", "")).replace("T", " ")}),
                       {k: v for k, v in m.body.items() if not k.startswith("_")})
        return handler

    def appointment_booked(self, m: BusMessage) -> None:
        b = m.body
        r = service_client.call("patient", "POST", "/internal/care-team", as_service="profile-worker",
                                json={"patient_id": b["patient_id"], "doctor_id": b["doctor_id"], "source": "appointment"})
        if r.status_code == 404:
            raise PermanentError("unknown patient")
        r.raise_for_status()
        self._post(m, "appointment", f"Appointment booked with {b.get('doctor_name')} ({b.get('specialty')}) for "
                                     f"{str(b.get('start_at')).replace('T', ' ')}",
                   {"appointment_id": b["appointment_id"], "doctor_id": b["doctor_id"], "start_at": b["start_at"]})

    def appointment_completed(self, m: BusMessage) -> None:
        b = m.body
        self._post(m, "visit", f"Visit completed with {b.get('doctor_name')} ({b.get('specialty')})",
                   {"appointment_id": b["appointment_id"], "doctor_id": b["doctor_id"], "start_at": b["start_at"],
                    "assessment": b.get("visit_notes", "")})

    def document_processed(self, m: BusMessage) -> None:
        b = m.body
        title = f"Report processed: {b.get('doc_type', 'document').replace('_', ' ')}"
        if b.get("abnormal_count"):
            title += f" - {b['abnormal_count']} abnormal result(s)"
        self._post(m, "lab_report" if b.get("doc_type") == "lab_report" else "document", title,
                   {"document_id": b["document_id"], "abnormal": b.get("abnormal", []), "lab_count": b.get("lab_count", 0)})

    def prescription(self, m: BusMessage) -> None:
        b = m.body
        verb = "discontinued" if m.event_type == "prescription.discontinued" else \
            {"create": "started", "update": "changed", "renew": "renewed"}.get(b.get("change_type"), "updated")
        self._post(m, "prescription", f"Medication {verb}: {b['medication']} {b.get('dosage', '')}",
                   {"prescription_id": b["prescription_id"], "version": b.get("version"),
                    "frequency": b.get("frequency"), "expiry_date": b.get("expiry_date")})


if __name__ == "__main__":
    ProfileWorker.main()
