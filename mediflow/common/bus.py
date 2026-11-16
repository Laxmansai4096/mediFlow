"""Message bus: Azure Service Bus semantics, with a free local emulator.

Semantics implemented by BOTH adapters (so the code that uses them is identical):

* Topics + subscriptions   - one published event fans out to every subscriber
* Peek-lock receive         - a message is locked, not removed, while a worker
                              processes it; if the worker crashes the lock
                              expires and the message is redelivered
* complete / abandon        - settle a message; abandon = retry with backoff
* Max delivery count        - after N failed attempts the message is moved to
                              the dead-letter queue (DLQ) for inspection
* Duplicate detection       - re-publishing the same message_id inside the
                              detection window is ignored (e.g. payment
                              gateway sends the same webhook twice)

Service Bus buffers asynchronous work. It is NOT an HTTP load balancer -
synchronous API traffic goes through API Management / the gateway.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, context

# topic -> subscriptions. Mirrored 1:1 in infra/modules/servicebus.bicep
TOPOLOGY: dict[str, list[str]] = {
    "patient-events": ["profile", "notification"],
    "appointment-events": ["profile", "notification", "billing"],
    "payment-events": ["notification", "profile"],
    "prescription-events": ["notification", "profile"],
    "document-events": ["document"],
    "document-processed": ["profile"],
    "reminder-events": ["notification"],
}

MAX_DELIVERY_COUNT = 5
DUPLICATE_DETECTION_WINDOW_SECONDS = 600
DEFAULT_LOCK_SECONDS = 30


@dataclass
class BusMessage:
    message_id: str
    topic: str
    subscription: str
    event_type: str
    body: dict
    correlation_id: str = ""
    delivery_count: int = 1
    enqueued_at: float = 0.0
    lock_token: str = ""
    seq: int = 0
    _native: Any = field(default=None, repr=False)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LocalServiceBus:
    """SQLite-backed emulator; safe across processes (workers run as separate processes)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.env("MEDIFLOW_BUS_DB", str(config.data_dir() / "bus.db")))
        self.retry_base = float(config.env("MEDIFLOW_BUS_RETRY_BASE_SECONDS", "1"))
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL, topic TEXT NOT NULL, subscription TEXT NOT NULL,
                    event_type TEXT NOT NULL, body TEXT NOT NULL, correlation_id TEXT,
                    enqueued_at REAL NOT NULL, visible_at REAL NOT NULL,
                    delivery_count INTEGER NOT NULL DEFAULT 0,
                    lock_token TEXT, locked_until REAL,
                    state TEXT NOT NULL DEFAULT 'active',
                    dead_letter_reason TEXT, last_error TEXT, completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS ix_msg_ready ON messages(subscription, state, visible_at);
                CREATE INDEX IF NOT EXISTS ix_msg_dupe ON messages(subscription, message_id);
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # -- publish -----------------------------------------------------------------
    def publish(self, topic: str, event_type: str, body: dict, *, message_id: str | None = None) -> dict:
        if topic not in TOPOLOGY:
            raise ValueError(f"unknown topic {topic}")
        message_id = message_id or uuid.uuid4().hex
        now = time.time()
        payload = json.dumps({**body, "_event_type": event_type, "_published_at": utcnow_iso()})
        delivered, duplicates = [], []
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for sub in TOPOLOGY[topic]:
                dupe = conn.execute(
                    "SELECT 1 FROM messages WHERE subscription=? AND message_id=? AND enqueued_at>=?",
                    (sub, message_id, now - DUPLICATE_DETECTION_WINDOW_SECONDS),
                ).fetchone()
                if dupe:
                    duplicates.append(sub)
                    continue
                conn.execute(
                    "INSERT INTO messages (message_id, topic, subscription, event_type, body,"
                    " correlation_id, enqueued_at, visible_at) VALUES (?,?,?,?,?,?,?,?)",
                    (message_id, topic, sub, event_type, payload, context.correlation_id.get(), now, now),
                )
                delivered.append(sub)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return {"message_id": message_id, "delivered_to": delivered, "duplicate": bool(duplicates) and not delivered}

    # -- receive / settle --------------------------------------------------------
    def receive(self, subscription: str, max_messages: int = 10,
                lock_seconds: int = DEFAULT_LOCK_SECONDS) -> list[BusMessage]:
        now = time.time()
        out: list[BusMessage] = []
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM messages WHERE subscription=? AND state='active' AND visible_at<=?"
                " AND (locked_until IS NULL OR locked_until<?) ORDER BY seq LIMIT ?",
                (subscription, now, now, max_messages),
            ).fetchall()
            for r in rows:
                if r["delivery_count"] >= MAX_DELIVERY_COUNT:  # lock expired too many times
                    conn.execute(
                        "UPDATE messages SET state='deadletter', dead_letter_reason=?, lock_token=NULL"
                        " WHERE seq=?", ("MaxDeliveryCountExceeded", r["seq"]),
                    )
                    continue
                token = uuid.uuid4().hex
                conn.execute(
                    "UPDATE messages SET lock_token=?, locked_until=?, delivery_count=delivery_count+1"
                    " WHERE seq=?", (token, now + lock_seconds, r["seq"]),
                )
                body = json.loads(r["body"])
                out.append(BusMessage(
                    message_id=r["message_id"], topic=r["topic"], subscription=subscription,
                    event_type=r["event_type"], body=body, correlation_id=r["correlation_id"] or "",
                    delivery_count=r["delivery_count"] + 1, enqueued_at=r["enqueued_at"],
                    lock_token=token, seq=r["seq"],
                ))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return out

    def _settle(self, msg: BusMessage, sql: str, params: tuple) -> bool:
        with self._conn() as conn:
            cur = conn.execute(sql + " WHERE seq=? AND lock_token=?", (*params, msg.seq, msg.lock_token))
            return cur.rowcount == 1  # False => lock was lost (another replica owns it now)

    def complete(self, msg: BusMessage) -> bool:
        return self._settle(msg, "UPDATE messages SET state='completed', completed_at=?, lock_token=NULL,"
                                 " locked_until=NULL", (time.time(),))

    def abandon(self, msg: BusMessage, error: str = "") -> bool:
        if msg.delivery_count >= MAX_DELIVERY_COUNT:
            return self.dead_letter(msg, "MaxDeliveryCountExceeded", error)
        backoff = min(self.retry_base * (2 ** (msg.delivery_count - 1)), 60)
        return self._settle(msg, "UPDATE messages SET visible_at=?, lock_token=NULL, locked_until=NULL,"
                                 " last_error=?", (time.time() + backoff, error[:500]))

    def dead_letter(self, msg: BusMessage, reason: str, error: str = "") -> bool:
        return self._settle(msg, "UPDATE messages SET state='deadletter', dead_letter_reason=?, last_error=?,"
                                 " lock_token=NULL, locked_until=NULL", (reason, error[:500]))

    # -- operations ----------------------------------------------------------------
    def active_count(self, subscription: str) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM messages WHERE subscription=? AND state='active'", (subscription,)
            ).fetchone()[0]

    def stats(self) -> dict:
        subs = sorted({s for v in TOPOLOGY.values() for s in v})
        result = {s: {"active": 0, "deadletter": 0, "completed": 0} for s in subs}
        with self._conn() as conn:
            for r in conn.execute("SELECT subscription, state, COUNT(*) c FROM messages GROUP BY subscription, state"):
                result.setdefault(r["subscription"], {}).update({r["state"]: r["c"]})
        return {"topology": TOPOLOGY, "subscriptions": result}

    def dead_letters(self, subscription: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT seq, message_id, topic, subscription, event_type, delivery_count, dead_letter_reason," \
              " last_error, enqueued_at FROM messages WHERE state='deadletter'"
        params: list = []
        if subscription:
            sql += " AND subscription=?"
            params.append(subscription)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY seq DESC LIMIT ?", (*params, limit))]

    def resubmit(self, seq: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE messages SET state='active', delivery_count=0, visible_at=?, dead_letter_reason=NULL"
                " WHERE seq=? AND state='deadletter'", (time.time(), seq),
            )
            return cur.rowcount == 1


class AzureServiceBus:  # pragma: no cover - requires a real (paid) namespace
    """Adapter for Azure Service Bus (Standard tier: topics + duplicate detection).

    Authenticates with DefaultAzureCredential (managed identity) - no connection
    strings. Activated with MEDIFLOW_BUS_MODE=azure and
    MEDIFLOW_SERVICEBUS_NAMESPACE=<name>.servicebus.windows.net
    """

    def __init__(self):
        from azure.identity import DefaultAzureCredential
        from azure.servicebus import ServiceBusClient

        self.namespace = config.env("MEDIFLOW_SERVICEBUS_NAMESPACE")
        self.credential = DefaultAzureCredential()
        self.client = ServiceBusClient(self.namespace, self.credential)
        self._receivers: dict[str, Any] = {}

    def publish(self, topic: str, event_type: str, body: dict, *, message_id: str | None = None) -> dict:
        from azure.servicebus import ServiceBusMessage

        message_id = message_id or uuid.uuid4().hex
        msg = ServiceBusMessage(
            json.dumps({**body, "_event_type": event_type, "_published_at": utcnow_iso()}),
            message_id=message_id, subject=event_type, correlation_id=context.correlation_id.get() or None,
            application_properties={"event_type": event_type}, content_type="application/json",
        )
        with self.client.get_topic_sender(topic) as sender:
            sender.send_messages(msg)
        return {"message_id": message_id, "delivered_to": TOPOLOGY.get(topic, []), "duplicate": False}

    def _receiver(self, subscription: str):
        # One subscription name may exist on several topics; workers listen on all of them.
        if subscription not in self._receivers:
            self._receivers[subscription] = [
                self.client.get_subscription_receiver(topic, subscription)
                for topic, subs in TOPOLOGY.items() if subscription in subs
            ]
        return self._receivers[subscription]

    def receive(self, subscription: str, max_messages: int = 10, lock_seconds: int = DEFAULT_LOCK_SECONDS):
        out = []
        for receiver in self._receiver(subscription):
            for m in receiver.receive_messages(max_message_count=max_messages, max_wait_time=1):
                body = json.loads(str(m))
                out.append(BusMessage(
                    message_id=m.message_id, topic=receiver.entity_path.split("/")[0], subscription=subscription,
                    event_type=(m.application_properties or {}).get(b"event_type", b"").decode()
                    or body.get("_event_type", ""), body=body, correlation_id=m.correlation_id or "",
                    delivery_count=m.delivery_count or 1, lock_token=str(m.lock_token), _native=(receiver, m),
                ))
        return out

    def complete(self, msg: BusMessage) -> bool:
        receiver, native = msg._native
        receiver.complete_message(native)
        return True

    def abandon(self, msg: BusMessage, error: str = "") -> bool:
        receiver, native = msg._native
        receiver.abandon_message(native)  # broker dead-letters after MaxDeliveryCount
        return True

    def dead_letter(self, msg: BusMessage, reason: str, error: str = "") -> bool:
        receiver, native = msg._native
        receiver.dead_letter_message(native, reason=reason, error_description=error[:4096])
        return True

    def active_count(self, subscription: str) -> int:
        from azure.servicebus.management import ServiceBusAdministrationClient

        admin = ServiceBusAdministrationClient(self.namespace, self.credential)
        return sum(
            admin.get_subscription_runtime_properties(t, subscription).active_message_count
            for t, subs in TOPOLOGY.items() if subscription in subs
        )

    def stats(self) -> dict:
        return {"topology": TOPOLOGY, "subscriptions": {}, "note": "see Azure Monitor metrics"}

    def dead_letters(self, subscription: str | None = None, limit: int = 50) -> list[dict]:
        return []

    def resubmit(self, seq: int) -> bool:
        return False


_bus = None
_bus_lock = threading.Lock()


def get_bus():
    global _bus
    if _bus is None:
        with _bus_lock:  # threads in single-container mode must share one instance
            if _bus is None:
                _bus = AzureServiceBus() if config.mode("bus") == "azure" else LocalServiceBus()
    return _bus


def reset_bus() -> None:
    global _bus
    _bus = None
