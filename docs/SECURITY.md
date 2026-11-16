# MediFlow AI - Threat model & security design

Method: STRIDE per trust boundary. Assets: patient clinical data (PHI), prescriptions, payment
state, credentials/MFA secrets, audit trail.

## Trust boundaries

1. Internet → API Management / gateway
2. Gateway → microservices (internal network)
3. Service → service (`/internal/*`, service identity)
4. Services/workers → Service Bus, SQL, Blob, Key Vault (managed identity)
5. Services → AI models (prompt content)
6. Payment provider → billing webhook

## STRIDE analysis

| Threat | Example | Mitigation | Where |
|---|---|---|---|
| **Spoofing** | Stolen password used for a doctor account | TOTP MFA for privileged roles; lockout after 5 failures; alert on brute force / spraying | `services/identity`, `services/audit` |
| | Forged payment-success callback | HMAC-SHA256 signature, constant-time compare, amount check; alert raised | `services/billing/gateway.py` |
| | End user calling internal APIs | `/internal/*` requires `service` role and is 404 at the gateway / APIM | `require("*")`, `gateway`, `apim.bicep` |
| **Tampering** | Insider edits audit rows to hide access | SHA-256 hash chain, `/audit/verify`; WORM archive in Azure | `common/audit.py`, `storage.bicep` |
| | Approved prescription silently changed | Versions are immutable; changes need MFA approval; full history kept | `services/prescription` |
| | Malicious file upload (renamed executable) | Magic-byte allow-list, 5 MB limit, server-generated blob names, SHA-256 integrity check | `services/document` |
| **Repudiation** | "I never viewed that record" | Every PHI read/write audited with actor, role, patient, correlation id, IP | `common/access.py` |
| **Information disclosure** | Billing clerk reads a diagnosis | Field-level views: demographics-only for billing/admin | `services/patient` |
| | Doctor browses patients they don't treat | Care-team check on every clinical request; break-glass is time-boxed and alerts | `services/patient` |
| | Chatbot tricked into revealing another patient | Retrieval scoped by the caller's token; other patient IDs in prompts blocked; injection screening | `services/ai`, `common/ai.py` |
| | AI hallucinates a clinical fact | Grounding validator drops uncited bullets; doctor approval required | `common/ai.py` |
| | Secrets in source control | Secrets generated per install / Key Vault references; managed identity, no connection strings | `common/config.py`, `keyvault.bicep` |
| **Denial of service** | Login flooding | Per-IP rate limits (stricter on auth), APIM limits | `gateway`, `apim.bicep` |
| | Burst of 5,000 uploads | Async processing via Service Bus; KEDA scales workers; body-size limits | `workers`, `containerapps.bicep` |
| | Poison message blocks a queue | Max delivery count → dead-letter queue; permanent errors dead-lettered immediately | `workers/base.py` |
| **Elevation of privilege** | Patient modifies own prescription | Role permissions deny prescription writes to patients | `common/auth.py` |
| | Billing user approves prescriptions | `doctor_only` + `prescription:approve` + MFA | `services/prescription` |

## Data protection

* **In transit:** TLS 1.2+ everywhere (HSTS, APIM HTTPS-only, Service Bus/SQL/Storage minimum TLS).
* **At rest:** AES-256-GCM for local blobs; Azure Storage SSE with infrastructure encryption; SQL TDE.
* **Minimisation:** events carry IDs, not clinical content; workers fetch contact details on demand;
  audit details never contain clinical text.
* **Consent:** WhatsApp messages only for patients who opted in; preferred language honoured.

## Monitoring & response

`/security/alerts` (admin) and the Azure Monitor rules in `monitoring.bicep` detect brute force,
password spraying, account lockouts, insider probing (repeated access denials), break-glass use,
AI abuse attempts and forged payment callbacks.
