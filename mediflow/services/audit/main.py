"""Audit & operations service.

* Admin: search the audit trail, verify hash-chain integrity, security alerts
* Patients: see who accessed their own record (transparency)
* Ops: Service Bus subscription depth, dead-letter inspection and resubmission

Security alerts here mirror the Azure Monitor / Sentinel analytics rules
defined in infra/modules/monitoring.bicep.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Query

from mediflow.common import audit
from mediflow.common.app_factory import create_service_app
from mediflow.common.auth import Principal, require, require_mfa
from mediflow.common.bus import get_bus

FAILED_LOGIN_THRESHOLD = 5
DENIED_ACCESS_THRESHOLD = 5


def _since(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")


def security_alerts(window_minutes: int = 60) -> list[dict]:
    since = _since(window_minutes)
    alerts = []
    failed = audit.query(action_prefix="auth.login", outcome="failure", since=since, limit=1000)
    by_actor = Counter(r["actor"] for r in failed)
    by_ip = Counter(r["ip"] for r in failed if r["ip"])
    for actor, n in by_actor.items():
        if n >= FAILED_LOGIN_THRESHOLD:
            alerts.append({"type": "brute_force_suspected", "severity": "high", "actor": actor, "count": n,
                           "detail": f"{n} failed logins in {window_minutes} min"})
    for ip, n in by_ip.items():
        if n >= FAILED_LOGIN_THRESHOLD * 2:
            alerts.append({"type": "password_spray_suspected", "severity": "high", "ip": ip, "count": n,
                           "detail": f"{n} failed logins from one address"})
    for r in audit.query(action_prefix="auth.lockout", since=since, limit=200):
        alerts.append({"type": "account_locked", "severity": "medium", "actor": r["actor"], "ts": r["ts"]})
    denied = audit.query(action_prefix="access.denied", since=since, limit=1000)
    for actor, n in Counter(r["actor"] for r in denied).items():
        if n >= DENIED_ACCESS_THRESHOLD:
            alerts.append({"type": "abnormal_access_attempts", "severity": "high", "actor": actor, "count": n,
                           "detail": f"{n} denied requests - possible insider probing"})
    for r in audit.query(action_prefix="patient.breakglass", since=_since(24 * 60), limit=200):
        alerts.append({"type": "break_glass_used", "severity": "critical", "actor": r["actor"],
                       "patient_id": r["patient_id"], "ts": r["ts"], "detail": r["details"].get("reason", "")})
    for r in audit.query(action_prefix="ai.chat.blocked", since=since, limit=200):
        alerts.append({"type": "ai_abuse_attempt", "severity": "medium", "actor": r["actor"], "ts": r["ts"],
                       "detail": r["details"].get("reason", "")})
    for r in audit.query(action_prefix="payment.webhook.invalid_signature", since=since, limit=200):
        alerts.append({"type": "forged_payment_callback", "severity": "high", "ts": r["ts"],
                       "detail": r["resource_id"]})
    return alerts


def create_app():
    app = create_service_app("audit", "Audit & Operations Service")

    @app.get("/audit", tags=["audit"])
    def search(patient_id: str | None = None, actor: str | None = None, action: str | None = None,
               severity: str | None = None, limit: int = Query(200, le=1000),
               p: Principal = Depends(require("audit:read"))):
        require_mfa(p)
        rows = audit.query(patient_id=patient_id, actor=actor, action_prefix=action, severity=severity, limit=limit)
        audit.record("audit.search", actor=p.sub, role=p.role, details={"results": len(rows)})
        return rows

    @app.get("/audit/verify", tags=["audit"])
    def verify(p: Principal = Depends(require("audit:read"))):
        return audit.verify_chain()

    @app.get("/security/alerts", tags=["security"])
    def alerts(window_minutes: int = Query(60, le=1440), p: Principal = Depends(require("audit:read"))):
        return security_alerts(window_minutes)

    @app.get("/me/access-log", tags=["patients"])
    def my_access_log(p: Principal = Depends(require("accesslog:read:self"))):
        rows = audit.query(patient_id=p.patient_id, limit=300)
        return [{"ts": r["ts"], "role": r["role"] or "system", "action": r["action"], "outcome": r["outcome"],
                 "by_you": r["actor"] == p.sub} for r in rows if not r["actor"].startswith("svc:")]

    # Ops ------------------------------------------------------------------------------
    @app.get("/ops/bus", tags=["ops"])
    def bus_stats(p: Principal = Depends(require("ops:read"))):
        return get_bus().stats()

    @app.get("/ops/deadletters", tags=["ops"])
    def dead_letters(subscription: str | None = None, p: Principal = Depends(require("ops:read"))):
        return get_bus().dead_letters(subscription)

    @app.post("/ops/deadletters/{seq}/resubmit", tags=["ops"])
    def resubmit(seq: int, p: Principal = Depends(require("ops:write"))):
        require_mfa(p)
        if not get_bus().resubmit(seq):
            raise HTTPException(404, "dead-lettered message not found")
        audit.record("ops.deadletter.resubmit", actor=p.sub, role=p.role, resource_type="message",
                     resource_id=str(seq), severity="warning")
        return {"ok": True}

    return app
