"""Run the whole MediFlow platform locally - zero cloud cost.

    python run_local.py            # start 9 services + workers + autoscaler
    python run_local.py --seed     # ...and load synthetic demo data

Each microservice runs as its own process (as it would in its own container).
A KEDA-style autoscaler watches each Service Bus subscription's backlog and
scales worker replicas between min and max - the same rule Azure Container
Apps applies with a `azure-servicebus` KEDA scale rule (see
infra/modules/containerapps.bicep).
"""
from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mediflow.common.config import SERVICE_PORTS  # noqa: E402

PY = sys.executable

# worker -> (module, subscription, min replicas, max replicas, target messages per replica)
WORKERS = {
    "notification": ("mediflow.workers.notification_worker", "notification", 1, 5, 20),
    "document": ("mediflow.workers.document_worker", "document", 1, 4, 10),
    "profile": ("mediflow.workers.profile_worker", "profile", 1, 4, 30),
    "billing": ("mediflow.workers.billing_worker", "billing", 1, 3, 30),
}
REMINDER_INTERVAL_SECONDS = 300
SCALE_DOWN_COOLDOWN_SECONDS = 30


def start_service(name: str, port: int) -> subprocess.Popen:
    module = f"mediflow.services.{name}.main:create_app"
    return subprocess.Popen([PY, "-m", "uvicorn", module, "--factory", "--host", "127.0.0.1", "--port", str(port),
                             "--log-level", "warning"], cwd=ROOT, env=os.environ.copy())


def start_worker(module: str) -> subprocess.Popen:
    return subprocess.Popen([PY, "-m", module], cwd=ROOT, env=os.environ.copy(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_healthy(timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    pending = set(SERVICE_PORTS)
    while pending and time.time() < deadline:
        for name in list(pending):
            try:
                if httpx.get(f"http://127.0.0.1:{SERVICE_PORTS[name]}/health/ready", timeout=1).status_code == 200:
                    pending.discard(name)
            except httpx.HTTPError:
                pass
        time.sleep(0.5)
    return not pending


class LocalKedaScaler:
    def __init__(self):
        from mediflow.common.bus import get_bus

        self.bus = get_bus()
        self.replicas: dict[str, list[subprocess.Popen]] = {w: [] for w in WORKERS}
        self.last_scale_up: dict[str, float] = {w: 0.0 for w in WORKERS}

    def reconcile(self) -> None:
        for name, (module, sub, lo, hi, target) in WORKERS.items():
            procs = [p for p in self.replicas[name] if p.poll() is None]
            backlog = self.bus.active_count(sub)
            desired = max(lo, min(hi, math.ceil(backlog / target))) if backlog else lo
            if desired > len(procs):
                self.last_scale_up[name] = time.time()
            elif desired < len(procs) and time.time() - self.last_scale_up[name] < SCALE_DOWN_COOLDOWN_SECONDS:
                desired = len(procs)  # KEDA-style cooldown: avoid flapping
            if desired != len(procs):
                print(f"[autoscaler] {name:<12} backlog={backlog:<5} replicas {len(procs)} -> {desired}", flush=True)
            while len(procs) < desired:
                procs.append(start_worker(module))
            while len(procs) > desired:
                procs.pop().terminate()  # peek-lock: unfinished messages are redelivered
            self.replicas[name] = procs

    def stop(self) -> None:
        for procs in self.replicas.values():
            for p in procs:
                p.terminate()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="store_true", help="load synthetic demo data after start-up")
    args = parser.parse_args()

    services = {name: start_service(name, port) for name, port in SERVICE_PORTS.items()}
    print("Starting services...", flush=True)
    if not wait_healthy():
        print("Some services failed to start; check data/logs/*.log", flush=True)
    scaler = LocalKedaScaler()
    scaler.reconcile()
    if args.seed:
        subprocess.run([PY, str(ROOT / "scripts" / "seed.py")], cwd=ROOT, check=False)

    print("\n  MediFlow AI is running (local, zero-cost mode)")
    print("  Portal:        http://127.0.0.1:8000")
    print("  API docs:      http://127.0.0.1:800{1..8}/docs  (e.g. http://127.0.0.1:8002/docs)")
    print("  Demo logins:   data/demo_credentials.txt")
    print("  Outbox:        data/outbox/  (emails + WhatsApp messages)")
    print("  Ctrl+C to stop\n", flush=True)

    from mediflow.workers import reminder_job

    last_reminder = 0.0
    try:
        while True:
            scaler.reconcile()
            for name, proc in services.items():
                if proc.poll() is not None:  # self-healing, like a container restart policy
                    print(f"[supervisor] {name} exited; restarting", flush=True)
                    services[name] = start_service(name, SERVICE_PORTS[name])
            if time.time() - last_reminder > REMINDER_INTERVAL_SECONDS:
                try:
                    reminder_job.run_once()
                except Exception as exc:  # noqa: BLE001
                    print(f"[reminders] skipped: {exc}", flush=True)
                last_reminder = time.time()
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        scaler.stop()
        for p in services.values():
            p.terminate()


if __name__ == "__main__":
    main()
