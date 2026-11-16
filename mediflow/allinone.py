"""Single-container mode - the whole platform in one process.

Used for the free Azure App Service (F1) demo, where only one app can run.
The architecture is unchanged; only the transport differs:

  * gateway -> services : in-memory ASGI calls instead of HTTP
  * service -> service  : in-memory clients (same service_client API)
  * workers             : background threads consuming the same bus
  * reminder job        : background thread (Azure Functions timer equivalent)
  * demo data           : seeded automatically on first start (synthetic)

    uvicorn mediflow.allinone:create_app --factory --port 8000
"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from mediflow.common import config, service_client
from mediflow.common.app_factory import get_logger
from mediflow.common.bus import get_bus
from mediflow.services.ai.main import create_app as ai_app
from mediflow.services.appointment.main import create_app as appointment_app
from mediflow.services.audit.main import create_app as audit_app
from mediflow.services.billing.main import create_app as billing_app
from mediflow.services.document.main import create_app as document_app
from mediflow.services.gateway.main import create_app as gateway_app
from mediflow.services.identity.main import create_app as identity_app
from mediflow.services.patient.main import create_app as patient_app
from mediflow.services.prescription.main import create_app as prescription_app

log = get_logger("allinone")


def _worker_loop(worker_cls) -> None:
    while True:  # retry start-up (e.g. transient SQLite lock) instead of dying silently
        try:
            worker = worker_cls()
            break
        except Exception:  # noqa: BLE001
            log.exception(f"{worker_cls.name} worker failed to start; retrying")
            time.sleep(2)
    idle = 1.0
    while True:
        try:
            n = worker.process_batch()
        except Exception:  # noqa: BLE001 - keep the thread alive, like a container restart
            log.exception(f"{worker.name} worker error")
            n = 0
        idle = 0.2 if n else min(idle * 1.5, 5.0)  # back off when idle (saves CPU quota on F1)
        time.sleep(idle)


def _reminder_loop() -> None:
    from mediflow.workers import reminder_job

    time.sleep(60)
    while True:
        try:
            reminder_job.run_once()
        except Exception:  # noqa: BLE001
            log.exception("reminder job failed")
        time.sleep(300)


def _seed(gateway) -> None:
    if config.env("MEDIFLOW_AUTO_SEED", "1") != "1" or (config.data_dir() / "demo_credentials.txt").exists():
        return
    time.sleep(2)
    try:
        from scripts import seed

        seed.main(client=TestClient(gateway, base_url="http://testserver/api"))
        log.info("synthetic demo data seeded")
    except Exception:  # noqa: BLE001
        log.exception("seeding failed")


def create_app():
    services = {"identity": identity_app(), "patient": patient_app(), "appointment": appointment_app(),
                "prescription": prescription_app(), "billing": billing_app(), "document": document_app(),
                "ai": ai_app(), "audit": audit_app()}
    for name, svc in services.items():
        service_client.OVERRIDES[name] = TestClient(svc)
    gateway = gateway_app(inprocess=services)
    get_bus()  # create the shared bus before any worker thread starts

    from mediflow.workers.billing_worker import BillingWorker
    from mediflow.workers.document_worker import DocumentWorker
    from mediflow.workers.notification_worker import NotificationWorker
    from mediflow.workers.profile_worker import ProfileWorker

    for cls in (NotificationWorker, DocumentWorker, ProfileWorker, BillingWorker):
        threading.Thread(target=_worker_loop, args=(cls,), name=f"worker-{cls.name}", daemon=True).start()
    threading.Thread(target=_reminder_loop, name="reminders", daemon=True).start()
    threading.Thread(target=_seed, args=(gateway,), name="seed", daemon=True).start()
    log.info("MediFlow all-in-one started")
    return gateway
