"""Patient-level authorization used by services that don't own patient data.

Role permissions answer "may a doctor ever read prescriptions?"; this module
answers "may *this* doctor read *this* patient's prescriptions right now?"
by asking the patient service (owner of the care-team relationship).
"""
from __future__ import annotations

from fastapi import HTTPException

from . import audit, context, service_client
from .auth import Principal, deny


def has_patient_access(principal: Principal, patient_id: str, scope: str = "clinical") -> bool:
    """scope: clinical | billing | demographics"""
    role = principal.role
    if role == "service":
        return True
    if role == "patient":
        return principal.patient_id == patient_id
    if role == "doctor":
        if scope == "billing":
            return False
        try:
            resp = service_client.call(
                "patient", "GET", "/internal/access-check",
                as_service=context.service_name.get(),
                params={"patient_id": patient_id, "doctor_id": principal.doctor_id or ""},
            )
        except service_client.ServiceUnavailable:
            raise HTTPException(503, "authorization service unavailable")  # fail closed
        return resp.status_code == 200 and bool(resp.json().get("allowed"))
    if role == "billing":
        return scope in ("billing", "demographics")
    if role == "admin":
        return scope == "demographics"
    return False


def enforce_patient_access(
    principal: Principal, patient_id: str, *, scope: str, action: str, resource_type: str,
    resource_id: str = "",
) -> None:
    if not has_patient_access(principal, patient_id, scope):
        raise deny(principal, action, resource_type=resource_type, resource_id=resource_id,
                   patient_id=patient_id, reason="not authorized for this patient")
    if principal.role != "service":
        audit.record(action, actor=principal.sub, role=principal.role, resource_type=resource_type,
                     resource_id=resource_id, patient_id=patient_id)
