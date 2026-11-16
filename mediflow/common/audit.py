"""Tamper-evident audit log.

Every access to, or change of, patient data is recorded as an append-only
entry. Each entry stores the SHA-256 of (previous hash + its own canonical
content), forming a hash chain: editing or deleting any historic row breaks
verification from that point on.

Local: SQLite file shared by all services (serialised with BEGIN IMMEDIATE).
Azure: the same records are emitted to Log Analytics via Application Insights
and archived to immutable (WORM) Blob Storage - see infra/modules/storage.bicep.

Entries hold identifiers only - never clinical content.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, context

GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    service TEXT, actor TEXT, role TEXT, action TEXT NOT NULL,
    resource_type TEXT, resource_id TEXT, patient_id TEXT,
    outcome TEXT, severity TEXT, correlation_id TEXT, ip TEXT,
    details TEXT, prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_patient ON audit_log(patient_id);
CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action);
"""

_FIELDS = (
    "ts", "service", "actor", "role", "action", "resource_type", "resource_id",
    "patient_id", "outcome", "severity", "correlation_id", "ip", "details",
)


def _db_path() -> Path:
    return Path(config.env("MEDIFLOW_AUDIT_DB", str(config.data_dir() / "audit.db")))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    return conn


def _hash(prev_hash: str, row: dict) -> str:
    canonical = json.dumps({k: row.get(k) for k in _FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def record(
    action: str,
    *,
    actor: str = "anonymous",
    role: str = "",
    resource_type: str = "",
    resource_id: str = "",
    patient_id: str = "",
    outcome: str = "success",
    severity: str = "info",
    details: dict | None = None,
) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "service": context.service_name.get(),
        "actor": actor,
        "role": role,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "patient_id": patient_id or "",
        "outcome": outcome,
        "severity": severity,
        "correlation_id": context.correlation_id.get(),
        "ip": context.client_ip.get(),
        "details": json.dumps(details or {}, sort_keys=True),
    }
    conn = _connect()
    try:
        for attempt in range(5):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError:
                time.sleep(0.05 * (attempt + 1))
        last = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        row["prev_hash"] = prev
        row["hash"] = _hash(prev, row)
        cols = ",".join(row)
        conn.execute(
            f"INSERT INTO audit_log ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values())
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def query(
    *,
    patient_id: str | None = None,
    actor: str | None = None,
    action_prefix: str | None = None,
    severity: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    limit: int = 200,
) -> list[dict]:
    clauses, params = [], []
    for col, val in (("patient_id", patient_id), ("actor", actor), ("severity", severity), ("outcome", outcome)):
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    if action_prefix:
        clauses.append("action LIKE ?")
        params.append(action_prefix + "%")
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM audit_log {where} ORDER BY seq DESC LIMIT ?", [*params, min(limit, 1000)]
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["details"] = json.loads(d["details"] or "{}")
        out.append(d)
    return out


def verify_chain() -> dict:
    conn = _connect()
    try:
        prev = GENESIS
        count = 0
        for r in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
            row = dict(r)
            if row["prev_hash"] != prev or _hash(prev, row) != row["hash"]:
                return {"valid": False, "checked": count, "broken_at_seq": row["seq"]}
            prev = row["hash"]
            count += 1
        return {"valid": True, "checked": count, "broken_at_seq": None}
    finally:
        conn.close()
