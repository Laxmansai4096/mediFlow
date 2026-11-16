"""Test harness: every service runs in-process; service-to-service HTTP calls are
routed to in-memory TestClients; the bus/audit/storage use a temp directory."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mediflow-test-")
os.environ["MEDIFLOW_DATA_DIR"] = _TMP
os.environ["MEDIFLOW_BUS_RETRY_BASE_SECONDS"] = "0"
os.environ["MEDIFLOW_BOOTSTRAP_ADMIN_PASSWORD"] = "AdminPass12345"

from fastapi.testclient import TestClient  # noqa: E402

from mediflow.common import service_client  # noqa: E402
from mediflow.common.auth import issue_token  # noqa: E402
from mediflow.services.ai.main import create_app as ai_app  # noqa: E402
from mediflow.services.appointment.main import create_app as appointment_app  # noqa: E402
from mediflow.services.audit.main import create_app as audit_app  # noqa: E402
from mediflow.services.billing.main import create_app as billing_app  # noqa: E402
from mediflow.services.document.main import create_app as document_app  # noqa: E402
from mediflow.services.identity.main import create_app as identity_app  # noqa: E402
from mediflow.services.patient.main import create_app as patient_app  # noqa: E402
from mediflow.services.prescription.main import create_app as prescription_app  # noqa: E402


@pytest.fixture(scope="session")
def clients():
    apps = {"identity": identity_app(), "patient": patient_app(), "appointment": appointment_app(),
            "prescription": prescription_app(), "billing": billing_app(), "document": document_app(),
            "ai": ai_app(), "audit": audit_app()}
    cs = {name: TestClient(app) for name, app in apps.items()}
    service_client.OVERRIDES.update(cs)
    yield cs
    service_client.OVERRIDES.clear()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def tokens():
    return {
        "admin": issue_token("USR-ADMIN", "admin", name="Admin", mfa=True),
        "billing": issue_token("USR-BILL", "billing", name="Biller", mfa=True),
        "service": issue_token("svc:test", "service", mfa=True),
    }


def make_patient(clients, name="Asha Verma", allergies=None, whatsapp=True):
    r = clients["patient"].post("/internal/patients", headers=bearer(issue_token("svc:t", "service", mfa=True)), json={
        "full_name": name, "date_of_birth": "1988-04-12", "sex": "female", "phone": "+919800000001",
        "email": f"{name.split()[0].lower()}@example.com", "preferred_language": "en",
        "whatsapp_opt_in": whatsapp, "allergies": allergies or [],
    })
    assert r.status_code == 201, r.text
    pid = r.json()["patient_id"]
    return pid, issue_token(f"USR-{pid}", "patient", name=name, patient_id=pid)


def make_doctor(clients, tokens, name="Dr. Rao", specialty="Cardiology", mfa=True):
    r = clients["appointment"].post("/doctors", headers=bearer(tokens["admin"]),
                                    json={"name": name, "specialty": specialty, "fee_paise": 70000, "slot_minutes": 20})
    assert r.status_code == 201, r.text
    did = r.json()["doctor_id"]
    week = [{"weekday": d, "start": "09:00", "end": "17:00"} for d in range(7)]
    assert clients["appointment"].put(f"/doctors/{did}/availability", headers=bearer(tokens["admin"]), json=week).status_code == 200
    return did, issue_token(f"USR-{did}", "doctor", name=name, doctor_id=did, mfa=mfa)


def tomorrow_slot(hour: int = 10, minute: int = 0) -> str:
    d = date.today() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, hour, minute).strftime("%Y-%m-%dT%H:%M")


def drain_all():
    from mediflow.workers.billing_worker import BillingWorker
    from mediflow.workers.document_worker import DocumentWorker
    from mediflow.workers.notification_worker import NotificationWorker
    from mediflow.workers.profile_worker import ProfileWorker

    for _ in range(3):  # events can trigger further events (document -> document-processed)
        for w in (DocumentWorker(), ProfileWorker(), BillingWorker(), NotificationWorker()):
            w.drain()
