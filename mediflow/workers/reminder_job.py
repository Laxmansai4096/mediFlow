"""Scheduled reminders - local equivalent of an Azure Functions timer trigger.

Runs periodically (run_local.py: every 5 min). Publishes reminder events with
deterministic message ids (one per appointment / prescription per day), so
running it many times never sends duplicate reminders.
"""
from __future__ import annotations

from datetime import date

from mediflow.common import service_client
from mediflow.common.bus import get_bus


def run_once() -> dict:
    bus = get_bus()
    today = date.today().isoformat()
    sent = {"appointment_reminders": 0, "prescription_expiry": 0}
    r = service_client.call("appointment", "GET", "/internal/appointments/upcoming", as_service="reminder-job",
                            params={"hours": 24})
    if r.status_code == 200:
        for a in r.json():
            res = bus.publish("reminder-events", "appointment.reminder", {
                "patient_id": a["patient_id"], "appointment_id": a["id"], "doctor_name": a.get("doctor_name"),
                "specialty": a.get("specialty"), "start_at": a["start_at"],
            }, message_id=f"reminder:appt:{a['id']}")
            sent["appointment_reminders"] += int(bool(res["delivered_to"]))
    r = service_client.call("prescription", "GET", "/internal/prescriptions/expiring", as_service="reminder-job",
                            params={"days": 3})
    if r.status_code == 200:
        for rx in r.json():
            res = bus.publish("reminder-events", "prescription.expiring", {
                "patient_id": rx["patient_id"], "prescription_id": rx["prescription_id"],
                "medication": rx["medication"], "expiry_date": rx["expiry_date"],
            }, message_id=f"reminder:rx:{rx['prescription_id']}:{today}")
            sent["prescription_expiry"] += int(bool(res["delivered_to"]))
    return sent


if __name__ == "__main__":
    print(run_once())
