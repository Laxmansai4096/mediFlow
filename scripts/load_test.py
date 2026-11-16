"""Burst test: publish N hospital events at once and watch the autoscaler react.

    python run_local.py --seed        # terminal 1
    python scripts/load_test.py 5000  # terminal 2

Publishes a realistic mix of registrations / payments / uploads / bookings
directly to the bus (like 5,000 patients acting simultaneously), then reports
backlog every second until all subscriptions are drained.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from mediflow.common.bus import get_bus  # noqa: E402


def main(n: int) -> None:
    bus = get_bus()
    # use real patients so workers succeed end-to-end
    from mediflow.common.auth import issue_token

    tok = issue_token("svc:loadtest", "service", mfa=True)
    patients = httpx.get("http://127.0.0.1:8002/patients", headers={"Authorization": f"Bearer {tok}"}).json()
    if not patients:
        sys.exit("seed data first: python run_local.py --seed")
    ids = [p["patient_id"] for p in patients]
    start = time.time()
    for i in range(n):
        pid = random.choice(ids)
        kind = random.random()
        if kind < 0.5:
            bus.publish("reminder-events", "appointment.reminder", {"patient_id": pid, "appointment_id": f"LOAD-{i}",
                        "doctor_name": "Dr. Load", "specialty": "Test", "start_at": "2026-12-01T10:00"})
        else:
            bus.publish("payment-events", "payment.succeeded", {"patient_id": pid, "payment_id": f"LOAD-{i}",
                        "invoice_id": f"INV-LOAD-{i}", "amount_paise": 50000, "description": "Load test",
                        "provider_ref": f"pg_load_{i}"}, message_id=f"payment.succeeded:LOAD-{i}")
    print(f"published {n} events in {time.time() - start:.1f}s; watching backlog (Ctrl+C to stop)")
    while True:
        counts = {s: bus.active_count(s) for s in ("notification", "profile")}
        print(f"t+{time.time() - start:5.0f}s backlog {counts}", flush=True)
        if not any(counts.values()):
            print(f"drained in {time.time() - start:.0f}s")
            break
        time.sleep(1)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000)
