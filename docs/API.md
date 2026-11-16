# MediFlow AI - API reference

Generated from the service code. Public base URL: `https://mediflow-ai-458ce0.azurewebsites.net/api/<service>/<path>`.
Every endpoint (except login, signup, the public chat and the payment webhook) needs `Authorization: Bearer <token>`.
`/internal/*` routes are service-to-service only and return 404 through the gateway.
Interactive docs: run locally and open `http://127.0.0.1:8001/docs` ... `8008/docs`.

## identity service

Identity service - local stand-in for Microsoft Entra ID (External ID for patients).

| Method | Path (via gateway) | Purpose |
|---|---|---|
| POST | `/api/identity/auth/login` | login |
| POST | `/api/identity/auth/signup` | signup |
| GET | `/api/identity/auth/me` | me |
| POST | `/api/identity/admin/users` | create staff |
| GET | `/api/identity/admin/users` | list users |
| POST | `/api/identity/admin/users/{user_id}/unlock` | unlock |
| POST | `/api/identity/admin/users/{user_id}/deactivate` | deactivate |

## patient service

Patient service - the Unified Patient Profile.

| Method | Path (via gateway) | Purpose |
|---|---|---|
| POST | `/api/patient/patients` | register patient |
| GET | `/api/patient/me` | my profile |
| GET | `/api/patient/patients` | search |
| GET | `/api/patient/patients/{patient_id}` | get patient |
| GET | `/api/patient/patients/{patient_id}/timeline` | timeline |
| POST | `/api/patient/patients/{patient_id}/consultations` | add consultation |
| PUT | `/api/patient/patients/{patient_id}/diet-plan` | set diet plan |
| PATCH | `/api/patient/patients/{patient_id}/clinical` | update clinical |
| PATCH | `/api/patient/patients/{patient_id}/contact` | update contact |
| POST | `/api/patient/patients/{patient_id}/break-glass` | Emergency access for a doctor not on the care team. Time-boxed, justified, |
| GET | `/api/patient/fhir/Patient/{patient_id}` | fhir patient |
| POST | `/internal/patients` | internal: internal create |
| GET | `/internal/patients/{patient_id}/contact` | internal: internal contact |
| GET | `/internal/patients/{patient_id}/clinical-flags` | internal: internal flags |
| GET | `/internal/access-check` | internal: access check |
| POST | `/internal/care-team` | internal: add care team |
| POST | `/internal/patients/{patient_id}/timeline` | internal: internal timeline |

## appointment service

Appointment service - doctors, availability, slots and bookings.

| Method | Path (via gateway) | Purpose |
|---|---|---|
| GET | `/api/appointment/doctors` | list doctors |
| GET | `/api/appointment/specialties` | specialties |
| POST | `/api/appointment/doctors` | create doctor |
| PUT | `/api/appointment/doctors/{doctor_id}/availability` | set availability |
| GET | `/api/appointment/doctors/{doctor_id}/availability` | get availability |
| GET | `/api/appointment/doctors/{doctor_id}/slots` | free slots |
| POST | `/api/appointment/appointments` | book |
| GET | `/api/appointment/appointments` | list appointments |
| GET | `/api/appointment/patients/{patient_id}/visit-history` | visit history |
| POST | `/api/appointment/appointments/{appt_id}/cancel` | cancel |
| POST | `/api/appointment/appointments/{appt_id}/complete` | complete |
| POST | `/api/appointment/appointments/{appt_id}/no-show` | no show |
| GET | `/internal/appointments/upcoming` | internal: upcoming |

## prescription service

Prescription service - digital prescriptions, versioning, approval and refills.

| Method | Path (via gateway) | Purpose |
|---|---|---|
| POST | `/api/prescription/patients/{patient_id}/prescriptions` | create rx |
| PUT | `/api/prescription/prescriptions/{rx_id}` | update rx |
| POST | `/api/prescription/prescriptions/{rx_id}/renew` | renew rx |
| POST | `/api/prescription/prescriptions/{rx_id}/discontinue` | discontinue rx |
| POST | `/api/prescription/prescriptions/{rx_id}/versions/{version}/approve` | approve |
| POST | `/api/prescription/prescriptions/{rx_id}/versions/{version}/reject` | reject |
| GET | `/api/prescription/patients/{patient_id}/prescriptions` | list rx |
| GET | `/api/prescription/prescriptions/{rx_id}` | get rx |
| POST | `/api/prescription/prescriptions/{rx_id}/refill-requests` | request refill |
| GET | `/api/prescription/refill-requests` | list refills |
| POST | `/api/prescription/refill-requests/{req_id}/{decision}` | decide refill |
| GET | `/api/prescription/fhir/MedicationRequest` | fhir meds |
| GET | `/internal/prescriptions/expiring` | internal: expiring |

## billing service

Billing service - invoices, payments and verified payment events.

| Method | Path (via gateway) | Purpose |
|---|---|---|
| POST | `/api/billing/invoices` | create invoice |
| GET | `/api/billing/invoices` | list invoices |
| GET | `/api/billing/invoices/{invoice_id}` | get invoice |
| POST | `/api/billing/invoices/{invoice_id}/void` | void invoice |
| POST | `/api/billing/invoices/{invoice_id}/pay` | pay |
| POST | `/api/billing/payments/webhook` | Gateway callback - authenticated by HMAC signature, not by user token. |
| GET | `/api/billing/billing/summary` | summary |
| POST | `/internal/invoices` | internal: internal invoice |
| POST | `/internal/invoices/void-by-source` | internal: internal void |

## document service

Document service - secure upload & storage of medical reports, scans and bills.

| Method | Path (via gateway) | Purpose |
|---|---|---|
| POST | `/api/document/patients/{patient_id}/documents` | upload |
| GET | `/api/document/patients/{patient_id}/documents` | list docs |
| GET | `/api/document/documents/{doc_id}` | get doc |
| GET | `/api/document/documents/{doc_id}/download` | download |
| GET | `/internal/documents/{doc_id}/content` | internal: internal content |
| POST | `/internal/documents/{doc_id}/extraction` | internal: internal extraction |

## ai service

AI service - clinical history summarization and the multilingual assistant.

| Method | Path (via gateway) | Purpose |
|---|---|---|
| POST | `/api/ai/ai/patients/{patient_id}/summary` | summarize |
| GET | `/api/ai/ai/patients/{patient_id}/summaries` | list summaries |
| POST | `/api/ai/ai/summaries/{summary_id}/review` | review |
| POST | `/api/ai/ai/chat` | chat |

## audit service

Audit & operations service.

| Method | Path (via gateway) | Purpose |
|---|---|---|
| GET | `/api/audit/audit` | search |
| GET | `/api/audit/audit/verify` | verify |
| GET | `/api/audit/security/alerts` | alerts |
| GET | `/api/audit/me/access-log` | my access log |
| GET | `/api/audit/ops/bus` | bus stats |
| GET | `/api/audit/ops/deadletters` | dead letters |
| POST | `/api/audit/ops/deadletters/{seq}/resubmit` | resubmit |

## gateway

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web portal |
| GET | `/api/health` | Aggregated health of all services |
| ANY | `/api/<service>/<path>` | Routed to the service, with rate limits |
