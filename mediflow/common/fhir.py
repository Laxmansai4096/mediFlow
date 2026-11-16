"""FHIR R4 mappings - local stand-in for Azure Health Data Services (FHIR service).

Records are exposed in standard FHIR shape so they can be exchanged with other
systems; in Azure they would be written to the FHIR service via its REST API.
"""
from __future__ import annotations


def patient_resource(p) -> dict:
    given, _, family = p.full_name.rpartition(" ")
    return {
        "resourceType": "Patient",
        "id": p.patient_id,
        "identifier": [{"system": "urn:mediflow:patient-id", "value": p.patient_id}],
        "name": [{"use": "official", "text": p.full_name, "family": family or p.full_name,
                  "given": [g for g in given.split(" ") if g]}],
        "gender": p.sex if p.sex in ("male", "female", "other") else "unknown",
        "birthDate": p.date_of_birth,
        "telecom": [t for t in (
            {"system": "phone", "value": p.phone} if p.phone else None,
            {"system": "email", "value": p.email} if p.email else None,
        ) if t],
        "communication": [{"language": {"coding": [{"system": "urn:ietf:bcp:47", "code": p.preferred_language}]},
                           "preferred": True}],
    }


def medication_request(rx, version) -> dict:
    status_map = {"active": "active", "discontinued": "stopped", "expired": "completed",
                  "pending_approval": "draft"}
    return {
        "resourceType": "MedicationRequest",
        "id": rx.id,
        "status": status_map.get(rx.status, "unknown"),
        "intent": "order",
        "subject": {"reference": f"Patient/{rx.patient_id}"},
        "requester": {"reference": f"Practitioner/{rx.prescriber_id}"},
        "medicationCodeableConcept": {"text": version.medication},
        "authoredOn": version.start_date,
        "dosageInstruction": [{
            "text": f"{version.dosage} {version.frequency}".strip(),
            "route": {"text": version.route},
            "patientInstruction": version.instructions,
        }],
        "dispenseRequest": {
            "numberOfRepeatsAllowed": version.refills_allowed,
            "validityPeriod": {"start": version.start_date, "end": version.expiry_date},
        },
    }
