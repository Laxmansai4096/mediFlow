"""Reliable message-consumer base class.

Delivery guarantees (Service Bus gives at-least-once delivery):
  * Idempotent consumer - an "inbox" table records processed message ids, so a
    redelivered or duplicated message is acknowledged without re-running
    side-effects (no duplicate receipts, no double invoices).
  * Transient failures -> abandon -> retried with exponential backoff.
  * Permanent failures (bad payload) -> dead-lettered immediately.
  * After MAX_DELIVERY_COUNT attempts -> dead-lettered for operator review.

Each worker runs as its own container (Azure Container Apps job/app) and is
scaled by KEDA on its subscription's backlog; locally run_local.py does the same.
"""
from __future__ import annotations

import argparse
import signal
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from mediflow.common import config, context
from mediflow.common.app_factory import get_logger
from mediflow.common.bus import BusMessage, get_bus


class PermanentError(Exception):
    """Raised for messages that can never succeed (e.g. malformed payload)."""


class Inbox:
    def __init__(self, worker: str):
        self.path = Path(config.data_dir() / f"inbox_{worker}.db")
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS processed (message_id TEXT PRIMARY KEY, processed_at REAL)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def seen(self, message_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM processed WHERE message_id=?", (message_id,)).fetchone() is not None

    def mark(self, message_id: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO processed VALUES (?, ?)", (message_id, time.time()))


class Worker:
    name = "worker"
    subscription = ""

    def __init__(self):
        self.bus = get_bus()
        self.inbox = Inbox(self.name)
        self.log = get_logger(f"worker-{self.name}")
        self.handlers: dict[str, Callable[[BusMessage], None]] = {}
        self._running = True

    def on(self, event_type: str, fn: Callable[[BusMessage], None]) -> None:
        self.handlers[event_type] = fn

    def process_batch(self, max_messages: int = 10) -> int:
        messages = self.bus.receive(self.subscription, max_messages=max_messages)
        for m in messages:
            context.correlation_id.set(m.correlation_id)
            context.service_name.set(f"worker-{self.name}")
            if self.inbox.seen(m.message_id):
                self.bus.complete(m)
                self.log.info("duplicate skipped", extra={"extra_fields": {"message_id": m.message_id}})
                continue
            handler = self.handlers.get(m.event_type)
            try:
                if handler:
                    handler(m)
                self.inbox.mark(m.message_id)
                self.bus.complete(m)
                self.log.info("processed", extra={"extra_fields": {
                    "message_id": m.message_id, "event_type": m.event_type, "attempt": m.delivery_count}})
            except PermanentError as exc:
                self.bus.dead_letter(m, "PermanentFailure", str(exc))
                self.log.error("dead-lettered", extra={"extra_fields": {"message_id": m.message_id, "error": str(exc)}})
            except Exception as exc:  # transient: retry with backoff
                self.bus.abandon(m, f"{type(exc).__name__}: {exc}")
                self.log.warning("retry scheduled", extra={"extra_fields": {
                    "message_id": m.message_id, "attempt": m.delivery_count, "error": str(exc)[:200]}})
        return len(messages)

    def drain(self, max_rounds: int = 50) -> int:
        """Process until the subscription is empty (used by tests and --once)."""
        total = 0
        for _ in range(max_rounds):
            n = self.process_batch()
            total += n
            if n == 0:
                break
        return total

    def run_forever(self, poll_seconds: float = 0.5) -> None:
        def stop(*_):
            self._running = False

        signal.signal(signal.SIGINT, stop)
        try:
            signal.signal(signal.SIGTERM, stop)
        except (ValueError, AttributeError):
            pass
        self.log.info("worker started", extra={"extra_fields": {"subscription": self.subscription}})
        while self._running:
            if self.process_batch() == 0:
                time.sleep(poll_seconds)
        self.log.info("worker stopped")

    @classmethod
    def main(cls) -> None:
        parser = argparse.ArgumentParser(description=f"MediFlow {cls.name} worker")
        parser.add_argument("--once", action="store_true", help="drain the queue and exit")
        args = parser.parse_args()
        worker = cls()
        if args.once:
            print(f"processed {worker.drain()} message(s)")
        else:
            worker.run_forever()
