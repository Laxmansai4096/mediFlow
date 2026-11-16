"""Load SYNTHETIC demo data through the public API (exercises the real flows).

All people, phone numbers and results below are fictitious.
Writes demo logins to data/demo_credentials.txt.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mediflow.common import config  # noqa: E402
from mediflow.common.passwords import totp_code  # noqa: E402

API = "http://127.0.0.1:8000/api"
DEMO_PASSWORD = "MediFlow#2026"

DOCTORS = [
    ("dr.rao", "Dr. Anil Rao", "Cardiology", "MD, DM (Cardiology)", ["en", "hi", "te"], 80000),
    ("dr.iyer", "Dr. Lakshmi Iyer", "General Medicine", "MBBS, MD", ["en", "te"], 50000),
    ("dr.khan", "Dr. Sameer Khan", "Endocrinology", "MD, DM (Endocrinology)", ["en", "hi"], 70000),
]
PATIENTS = [
    ("asha", "Asha Verma", "1988-04-12", "female", "+919800000001", "hi", True, ["Penicillin"], ["Type 2 diabetes"]),
    ("ravi", "Ravi Kumar", "1975-01-30", "male", "+919800000002", "te", True, [], ["Hypertension"]),
    ("neha", "Neha Singh", "1995-09-03", "female", "+919800000003", "en", False, ["Sulfa drugs"], []),
]

LAB_REPORT = """CITY DIAGNOSTICS - LABORATORY REPORT (SYNTHETIC)
Patient Name: {name}
Sample ID: SYN-{n}
Hemoglobin 11.1 g/dL (12.0-15.5)
Fasting Glucose 142 mg/dL (70-100)
HbA1c 7.2 % (4.0-5.6)
Total Cholesterol 186 mg/dL (125-200)
Creatinine 0.9 mg/dL (0.6-1.1)
"""


def call(c: httpx.Client, method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = c.request(method, path.lstrip("/"), headers=headers, **kw)
    if r.status_code >= 400:
        print(f"  ! {method} {path} -> {r.status_code} {r.text[:200]}")
    return r


def login(c: httpx.Client, username: str, password: str, secret: str | None = None) -> str:
    body = {"username": username, "password": password}
    if secret:
        body["totp"] = totp_code(secret)
    return call(c, "POST", "/identity/auth/login", json=body).json()["access_token"]


def next_slot(c, token, doctor_id, days_ahead=1) -> str | None:
    for d in range(days_ahead, days_ahead + 7):
        day = (date.today() + timedelta(days=d)).isoformat()
        slots = call(c, "GET", f"/appointment/doctors/{doctor_id}/slots", token, params={"date": day}).json()["slots"]
        if slots:
            return slots[len(slots) // 3]
    return None


def drain_bus(seconds: float = 15) -> None:
    from mediflow.common.bus import get_bus

    bus = get_bus()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if all(bus.active_count(s) == 0 for s in ("profile", "billing", "document", "notification")):
            return
        time.sleep(0.5)


def main(client=None) -> None:
    creds_file = config.data_dir() / "demo_credentials.txt"
    if creds_file.exists():
        print("Demo data already seeded (data/demo_credentials.txt). Delete the data/ folder to reseed.")
        return
    boot = json.loads((config.data_dir() / "bootstrap_admin.json").read_text())
    lines = ["MediFlow AI - SYNTHETIC demo accounts (local only)", "",
             "Staff accounts need a 6-digit MFA code. Get the current code with:",
             "    python scripts/totp.py <username>", ""]
    secrets: dict[str, str] = {"admin": boot["mfa_secret"]}

    with (client or httpx.Client(base_url=API + "/", timeout=30)) as c:
        admin = login(c, "admin", boot["password"], boot["mfa_secret"])
        lines.append(f"admin        / {boot['password']}   (role: admin)")

        doctor_tokens = {}
        for username, name, spec, qual, langs, fee in DOCTORS:
            d = call(c, "POST", "/appointment/doctors", admin, json={"name": name, "specialty": spec,
                     "qualifications": qual, "languages": langs, "fee_paise": fee, "slot_minutes": 20}).json()
            week = [{"weekday": w, "start": "09:00", "end": "13:00"} for w in range(6)] + \
                   [{"weekday": w, "start": "15:00", "end": "18:00"} for w in range(5)]
            call(c, "PUT", f"/appointment/doctors/{d['doctor_id']}/availability", admin, json=week)
            u = call(c, "POST", "/identity/admin/users", admin, json={"username": username, "password": DEMO_PASSWORD,
                     "role": "doctor", "display_name": name, "doctor_id": d["doctor_id"]}).json()
            secrets[username] = u["mfa_secret"]
            doctor_tokens[username] = (d["doctor_id"], login(c, username, DEMO_PASSWORD, u["mfa_secret"]))
            lines.append(f"{username:<12} / {DEMO_PASSWORD}   (role: doctor, {spec})")

        u = call(c, "POST", "/identity/admin/users", admin, json={"username": "billing.desk", "password": DEMO_PASSWORD,
                 "role": "billing", "display_name": "Billing Desk"}).json()
        secrets["billing.desk"] = u["mfa_secret"]
        lines.append(f"billing.desk / {DEMO_PASSWORD}   (role: billing)")
        lines.append("")

        for i, (username, name, dob, sex, phone, lang, wa, allergies, conditions) in enumerate(PATIENTS):
            r = call(c, "POST", "/identity/auth/signup", json={
                "username": username, "password": DEMO_PASSWORD, "full_name": name, "date_of_birth": dob, "sex": sex,
                "phone": phone, "email": f"{username}@example.com", "preferred_language": lang, "whatsapp_opt_in": wa}).json()
            ptok, pid = r["access_token"], r["patient_id"]
            lines.append(f"{username:<12} / {DEMO_PASSWORD}   (role: patient, {pid}, language {lang})")

            doc_user = ["dr.khan", "dr.rao", "dr.iyer"][i]
            did, dtok = doctor_tokens[doc_user]
            slot = next_slot(c, ptok, did)
            appt = call(c, "POST", "/appointment/appointments", ptok, json={"doctor_id": did, "start_at": slot,
                        "reason": "Follow-up consultation"}).json()
            drain_bus()  # care team + invoice created asynchronously

            call(c, "PATCH", f"/patient/patients/{pid}/clinical", dtok, json={
                "allergies": allergies, "chronic_conditions": conditions,
                "blood_group": ["B+", "O+", "A-"][i]})
            call(c, "POST", f"/patient/patients/{pid}/consultations", dtok, json={
                "chief_complaint": ["Fatigue and increased thirst", "Headache, high BP readings", "Seasonal cough"][i],
                "clinical_notes": "Synthetic demo note.", "assessment": ["Glycaemic control suboptimal",
                "BP above target", "Viral upper respiratory infection"][i],
                "plan": ["Repeat HbA1c in 3 months", "Home BP log for 2 weeks", "Symptomatic care"][i], "follow_up_days": 30})
            med = [("Metformin", "500 mg", "twice daily"), ("Amlodipine", "5 mg", "once daily"),
                   ("Cetirizine", "10 mg", "once daily at night")][i]
            rx = call(c, "POST", f"/prescription/patients/{pid}/prescriptions", dtok, json={
                "medication": med[0], "dosage": med[1], "frequency": med[2], "duration_days": [90, 60, 5][i],
                "refills_allowed": [2, 1, 0][i], "instructions": "Take after food."}).json()
            call(c, "POST", f"/prescription/prescriptions/{rx['id']}/versions/1/approve", dtok, json={"note": "seed"})
            call(c, "POST", f"/document/patients/{pid}/documents", ptok, data={"doc_type": "lab_report"},
                 files={"file": (f"lab_{username}.txt", LAB_REPORT.format(name=name, n=i).encode(), "text/plain")})
            drain_bus()
            if i == 0:  # one paid invoice to demonstrate the receipt flow
                inv = call(c, "GET", "/billing/invoices", ptok).json()
                if inv:
                    call(c, "POST", f"/billing/invoices/{inv[0]['id']}/pay", ptok, json={"method": "upi"},
                         headers={"Idempotency-Key": uuid.uuid4().hex})

    lines += ["", "All data is synthetic. Outgoing emails/WhatsApp messages: data/outbox/"]
    creds_file.write_text("\n".join(lines), encoding="utf-8")
    (config.data_dir() / ".demo_mfa.json").write_text(json.dumps(secrets))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
