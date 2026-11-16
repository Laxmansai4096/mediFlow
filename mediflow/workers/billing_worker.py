"""Billing worker - raises consultation invoices from appointment events."""
from __future__ import annotations

from mediflow.common import service_client
from mediflow.common.bus import BusMessage

from .base import Worker


class BillingWorker(Worker):
    name = "billing"
    subscription = "billing"

    def __init__(self):
        super().__init__()
        self.on("appointment.booked", self.invoice)
        self.on("appointment.cancelled", self.void)

    def invoice(self, m: BusMessage) -> None:
        b = m.body
        if not b.get("fee_paise"):
            return
        r = service_client.call("billing", "POST", "/internal/invoices", as_service="billing-worker", json={
            "patient_id": b["patient_id"], "amount_paise": b["fee_paise"],
            "description": f"Consultation - {b.get('doctor_name')} ({b.get('specialty')}) {str(b.get('start_at')).replace('T', ' ')}",
            "source_type": "appointment", "source_id": b["appointment_id"],
        })
        r.raise_for_status()

    def void(self, m: BusMessage) -> None:
        r = service_client.call("billing", "POST", "/internal/invoices/void-by-source", as_service="billing-worker",
                                params={"source_type": "appointment", "source_id": m.body["appointment_id"]})
        r.raise_for_status()


if __name__ == "__main__":
    BillingWorker.main()
