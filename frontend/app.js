/* MediFlow AI portal - vanilla JS, CSP-safe (no inline handlers), all output HTML-escaped. */
"use strict";

const S = { token: null, user: null, tab: null, patient: null };
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (p) => "₹" + (Number(p || 0) / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 });
const dt = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");
const chip = (s) => {
  const cls = { active: "ok", paid: "ok", processed: "ok", approved: "ok", completed: "ok", sent: "ok", booked: "ok",
    pending_approval: "warn", pending: "warn", open: "warn", draft: "warn", uploaded: "warn", requested: "warn",
    cancelled: "bad", discontinued: "bad", expired: "bad", void: "bad", failed: "bad", rejected: "bad", denied: "bad", no_show: "bad",
    high: "bad", low: "warn", critical: "bad", warning: "warn" }[s] || "";
  return `<span class="chip ${cls}">${esc(String(s).replace(/_/g, " "))}</span>`;
};
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), 3500);
}

async function api(method, path, body, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (S.token) headers.Authorization = "Bearer " + S.token;
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const r = await fetch("/api" + path, { method, headers, body: payload });
  let data = null;
  try { data = await r.json(); } catch (_) { /* non-json */ }
  if (r.status === 401 && S.token && !opts.quiet401) { logout(); toast("Session expired - please sign in again", true); }
  if (!r.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : d && d.message ? d.message + (d.warnings ? ": " + d.warnings.join("; ") : "") : `Request failed (${r.status})`;
    const e = new Error(msg); e.status = r.status; e.data = data; throw e;
  }
  return data;
}

function formData(form) {
  const o = {};
  new FormData(form).forEach((v, k) => { o[k] = v; });
  form.querySelectorAll("input[type=checkbox]").forEach((c) => { o[c.name] = c.checked; });
  return o;
}

/* ---------------- session ---------------- */
function saveSession() { try { sessionStorage.setItem("mf", JSON.stringify({ token: S.token, user: S.user })); } catch (_) {} }
function loadSession() {
  try { const s = JSON.parse(sessionStorage.getItem("mf") || "null"); if (s) { S.token = s.token; S.user = s.user; } } catch (_) {}
}
function logout() {
  S.token = null; S.user = null; S.patient = null;
  try { sessionStorage.removeItem("mf"); } catch (_) {}
  render();
}

const TABS = {
  patient: [["overview", "My profile"], ["appointments", "Appointments"], ["prescriptions", "Prescriptions"], ["bills", "Bills"],
    ["reports", "Reports"], ["assistant", "Assistant"], ["accesslog", "Access log"]],
  doctor: [["patients", "My patients"], ["schedule", "Schedule"], ["refills", "Refill requests"]],
  billing: [["invoices", "Invoices"], ["newinvoice", "New invoice"], ["summary", "Summary"]],
  admin: [["users", "Users"], ["doctors", "Doctors"], ["register", "Register patient"], ["audit", "Audit log"],
    ["alerts", "Security alerts"], ["ops", "Operations"]],
};

function render() {
  const logged = !!S.token;
  $("#login-view").classList.toggle("hidden", logged);
  $("#main-view").classList.toggle("hidden", !logged);
  $("#who").innerHTML = logged
    ? `<span>${esc(S.user.name)}</span> ${chip(S.user.role)} ${S.user.patient_id ? `<span class="pid">${esc(S.user.patient_id)}</span>` : ""}
       ${S.user.mfa ? '<span class="chip ok">MFA</span>' : ""} <button class="sm" data-action="logout">Sign out</button>`
    : "";
  if (!logged) return;
  const tabs = TABS[S.user.role] || [];
  if (!S.tab || !tabs.find((t) => t[0] === S.tab)) S.tab = tabs[0][0];
  $("#tabs").innerHTML = tabs.map(([k, l]) => `<button data-action="tab" data-tab="${k}" class="${k === S.tab ? "active" : ""}">${l}</button>`).join("");
  show(S.tab);
}

async function show(tab) {
  S.tab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const c = $("#content");
  c.innerHTML = '<div class="card muted">Loading…</div>';
  try { c.innerHTML = await VIEWS[tab](); after[tab] && after[tab](); }
  catch (e) { c.innerHTML = `<div class="card">${esc(e.message)}</div>`; }
}

/* ---------------- views ---------------- */
const after = {};
const VIEWS = {
  /* ===== patient ===== */
  async overview() {
    const p = await api("GET", "/patient/me");
    const tl = await api("GET", `/patient/patients/${p.patient_id}/timeline?limit=30`);
    return `<div class="card idcard"><div><div class="muted small">Your MediFlow patient ID</div><div class="pid">${esc(p.patient_id)}</div>
      <div class="muted small">Show this at every visit. It links all your records.</div></div>
      <div><b>${esc(p.full_name)}</b><br><span class="muted">${esc(p.age)} y · ${esc(p.sex)} · blood group ${esc(p.blood_group || "-")}</span></div></div>
    <div class="grid2"><div class="card"><h2>Health information</h2>
      <p><b>Allergies:</b> ${esc(p.allergies.join(", ") || "none recorded")}</p>
      <p><b>Conditions:</b> ${esc(p.chronic_conditions.join(", ") || "none recorded")}</p>
      <p><b>Diet plan:</b> ${esc(p.diet_plan || "none")}</p></div>
      <div class="card"><h2>Contact & notifications</h2>
      <form data-form="contact"><label>Phone <input name="phone" value="${esc(p.phone)}"></label>
      <label>Email <input name="email" value="${esc(p.email)}"></label>
      <label>Language <select name="preferred_language">${["en", "hi", "te"].map((l) => `<option ${l === p.preferred_language ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <label class="check"><input type="checkbox" name="whatsapp_opt_in" ${p.whatsapp_opt_in ? "checked" : ""}> WhatsApp confirmations</label>
      <button class="primary">Save</button></form></div></div>
    <div class="card"><h2>My health timeline</h2>${timelineHtml(tl)}</div>`;
  },

  async appointments() {
    const [appts, doctors] = await Promise.all([api("GET", "/appointment/appointments"), api("GET", "/appointment/doctors")]);
    const today = new Date(Date.now() + 864e5).toISOString().slice(0, 10);
    return `<div class="card"><h2>Book an appointment</h2><form data-form="slots" class="row">
      <label>Doctor <select name="doctor_id">${doctors.map((d) => `<option value="${esc(d.doctor_id)}">${esc(d.specialty)} - ${esc(d.name)} (${money(d.fee_paise)})</option>`).join("")}</select></label>
      <label>Date <input type="date" name="date" value="${today}"></label><button>Find slots</button></form>
      <div id="slots"></div></div>
      <div class="card"><h2>My appointments</h2>${apptTable(appts, true)}</div>`;
  },

  async prescriptions() {
    const list = await api("GET", `/prescription/patients/${S.user.patient_id}/prescriptions`);
    return `<div class="card"><h2>My prescriptions</h2>${list.length ? `<div class="table-wrap"><table><tr><th>Medicine</th><th>Dose</th><th>Valid until</th><th>Status</th><th>Refills left</th><th></th></tr>
      ${list.map((r) => `<tr><td><b>${esc(r.current.medication)}</b><div class="muted small">${esc(r.current.instructions)}</div></td>
      <td>${esc(r.current.dosage)}, ${esc(r.current.frequency)}</td><td>${esc(r.current.expiry_date)}</td><td>${chip(r.status)}</td>
      <td>${r.refills_remaining}</td><td>${r.refill_eligible ? `<button class="sm" data-action="refill" data-id="${esc(r.id)}">Request refill</button>`
        : `<span class="muted small">${esc(r.refill_ineligible_reason)}</span>`}</td></tr>`).join("")}</table></div>` : '<p class="muted">No prescriptions yet.</p>'}
      <p class="muted small">Only a doctor can change prescriptions. Changes appear here after the prescriber approves them.</p></div>`;
  },

  async bills() {
    const inv = await api("GET", "/billing/invoices");
    return `<div class="card"><h2>My bills</h2>${invoiceTable(inv, true)}<p class="muted small">After payment is verified, a receipt is emailed (and sent on WhatsApp if enabled).</p></div>`;
  },

  async reports() {
    const docs = await api("GET", `/document/patients/${S.user.patient_id}/documents`);
    return uploadCard(S.user.patient_id) + `<div class="card"><h2>My reports</h2>${docsTable(docs)}</div>`;
  },

  async assistant() {
    return `<div class="card"><h2>MediFlow assistant</h2><p class="muted small">Ask in English, हिन्दी or తెలుగు about hospital services, or your own appointments, bills and prescriptions. The assistant does not give diagnoses.</p>
      <div id="chat" class="chat"></div>
      <form data-form="chat" class="row"><label>Message <input name="message" maxlength="1000" placeholder="e.g. मेरा अपॉइंटमेंट कब है? / visiting hours / నా బిల్లులు" required></label>
      <label style="max-width:120px">Language <select name="language"><option value="auto">auto</option><option value="en">en</option><option value="hi">hi</option><option value="te">te</option></select></label>
      <button class="primary">Send</button></form></div>`;
  },

  async accesslog() {
    const log = await api("GET", "/audit/me/access-log");
    return `<div class="card"><h2>Who accessed my record</h2><p class="muted small">Every view or change of your record is logged in a tamper-evident audit trail.</p>
      <div class="table-wrap"><table><tr><th>When</th><th>Who (role)</th><th>Action</th><th>Outcome</th></tr>
      ${log.map((e) => `<tr><td>${esc(new Date(e.ts).toLocaleString())}</td><td>${e.by_you ? "you" : esc(e.role)}</td><td>${esc(e.action)}</td><td>${chip(e.outcome)}</td></tr>`).join("")}</table></div></div>`;
  },

  /* ===== doctor ===== */
  async patients() {
    const list = await api("GET", "/patient/patients");
    return `<div class="card"><h2>My patients (care team)</h2>
      <form data-form="psearch" class="row"><label>Search name or patient ID <input name="q"></label><button>Search</button></form>
      <div id="plist">${patientList(list)}</div>
      <h3>Emergency access</h3><form data-form="breakglass" class="row"><label>Patient ID <input name="patient_id" placeholder="MF-0000-0000-0" required></label>
      <label>Justification (audited) <input name="reason" minlength="15" required></label><button class="danger">Break glass</button></form></div>
      <div id="pdetail"></div>`;
  },

  async schedule() {
    const appts = await api("GET", "/appointment/appointments");
    return `<div class="card"><h2>My schedule</h2>${apptTable(appts, false, true)}</div>`;
  },

  async refills() {
    const list = await api("GET", "/prescription/refill-requests");
    return `<div class="card"><h2>Pending refill requests</h2>${list.length ? `<table><tr><th>Patient</th><th>Medicine</th><th>Note</th><th></th></tr>
      ${list.map((r) => `<tr><td class="pid">${esc(r.patient_id)}</td><td>${esc(r.medication)}</td><td>${esc(r.note)}</td>
      <td><button class="sm primary" data-action="refill-decide" data-id="${esc(r.id)}" data-d="approve">Approve</button>
      <button class="sm danger" data-action="refill-decide" data-id="${esc(r.id)}" data-d="deny">Deny</button></td></tr>`).join("")}</table>` : '<p class="muted">No pending requests.</p>'}</div>`;
  },

  /* ===== billing ===== */
  async invoices() {
    const inv = await api("GET", "/billing/invoices");
    return `<div class="card"><h2>All invoices</h2><p class="muted small">Billing staff see payment information only - never diagnoses or clinical notes.</p>${invoiceTable(inv, false, true)}</div>`;
  },
  async newinvoice() {
    return `<div class="card narrow"><h2>Create invoice</h2><form data-form="invoice">
      <label>Patient ID <input name="patient_id" required></label><label>Description <input name="description" required></label>
      <label>Amount (₹) <input name="amount" type="number" min="1" step="0.01" required></label><button class="primary">Create</button></form></div>`;
  },
  async summary() {
    const s = await api("GET", "/billing/billing/summary");
    const inv = s.invoices;
    return `<div class="grid3">${["open", "paid", "void"].map((k) => `<div class="card"><div class="muted">${k} invoices</div>
      <div class="stat">${(inv[k] || {}).count || 0}</div><div class="muted">${money((inv[k] || {}).amount_paise)}</div></div>`).join("")}</div>
      <div class="card"><h2>Payments</h2>${Object.entries(s.payments).map(([k, v]) => `${chip(k)} ${v}`).join(" &nbsp; ") || "none"}</div>`;
  },

  /* ===== admin ===== */
  async users() {
    const users = await api("GET", "/identity/admin/users");
    return `<div class="card"><h2>Create staff account</h2><form data-form="user" class="grid2">
      <label>Username <input name="username" required></label><label>Display name <input name="display_name" required></label>
      <label>Role <select name="role"><option>doctor</option><option>billing</option><option>admin</option></select></label>
      <label>Doctor ID (doctors only) <input name="doctor_id"></label><label>Initial password <input name="password" type="password" minlength="10" required></label>
      <div><button class="primary">Create</button></div></form><div id="mfa-out"></div></div>
      <div class="card"><h2>Accounts</h2><div class="table-wrap"><table><tr><th>User</th><th>Role</th><th>Linked</th><th>MFA</th><th>Status</th><th></th></tr>
      ${users.map((u) => `<tr><td>${esc(u.username)}<div class="muted small">${esc(u.display_name)}</div></td><td>${chip(u.role)}</td>
      <td class="pid small">${esc(u.patient_id || u.doctor_id || "")}</td><td>${u.mfa_enrolled ? chip("enrolled") : ""}</td>
      <td>${u.active ? (u.locked ? chip("locked") : chip("active")) : chip("deactivated")}</td>
      <td>${u.locked ? `<button class="sm" data-action="unlock" data-id="${esc(u.id)}">Unlock</button>` : ""}</td></tr>`).join("")}</table></div></div>`;
  },
  async doctors() {
    const docs = await api("GET", "/appointment/doctors");
    return `<div class="card"><h2>Add doctor</h2><form data-form="doctor" class="grid2">
      <label>Name <input name="name" required></label><label>Specialty <input name="specialty" required></label>
      <label>Qualifications <input name="qualifications"></label><label>Fee (₹) <input name="fee" type="number" value="500"></label>
      <label>Slot minutes <input name="slot_minutes" type="number" value="20"></label><label>Languages (comma) <input name="languages" value="en,hi,te"></label>
      <div><button class="primary">Add (Mon-Sat 09:00-13:00)</button></div></form></div>
      <div class="card"><h2>Doctors</h2><table><tr><th>ID</th><th>Name</th><th>Specialty</th><th>Fee</th><th>Languages</th></tr>
      ${docs.map((d) => `<tr><td class="pid small">${esc(d.doctor_id)}</td><td>${esc(d.name)}</td><td>${esc(d.specialty)}</td><td>${money(d.fee_paise)}</td><td>${esc(d.languages.join(", "))}</td></tr>`).join("")}</table></div>`;
  },
  async register() {
    return `<div class="card narrow"><h2>Register walk-in patient</h2><form data-form="register">
      <label>Full name <input name="full_name" required></label><label>Date of birth <input name="date_of_birth" type="date" required></label>
      <label>Sex <select name="sex"><option>female</option><option>male</option><option>other</option></select></label>
      <label>Phone <input name="phone" required></label><label>Email <input name="email" type="email" required></label>
      <label>Language <select name="preferred_language"><option>en</option><option>hi</option><option>te</option></select></label>
      <button class="primary">Register & generate patient ID</button></form><div id="reg-out"></div></div>`;
  },
  async audit() {
    const [rows, chain] = await Promise.all([api("GET", "/audit/audit?limit=150"), api("GET", "/audit/audit/verify")]);
    return `<div class="card"><h2>Audit trail</h2><p>${chain.valid ? chip("approved") + " Hash chain intact" : chip("failed") + " Chain broken at #" + esc(chain.broken_at_seq)} - ${chain.checked} entries verified.</p>
      <form data-form="auditq" class="row"><label>Patient ID <input name="patient_id"></label><label>Action prefix <input name="action" placeholder="e.g. prescription"></label><button>Filter</button></form>
      <div id="audit-rows">${auditTable(rows)}</div></div>`;
  },
  async alerts() {
    const a = await api("GET", "/audit/security/alerts?window_minutes=1440");
    return `<div class="card"><h2>Security alerts (24h)</h2>${a.length ? `<table><tr><th>Type</th><th>Severity</th><th>Actor / IP</th><th>Detail</th></tr>
      ${a.map((x) => `<tr><td>${esc(x.type)}</td><td>${chip(x.severity)}</td><td class="small">${esc(x.actor || x.ip || "")}${x.patient_id ? " → " + esc(x.patient_id) : ""}</td><td>${esc(x.detail || x.ts || "")}</td></tr>`).join("")}</table>`
      : '<p class="muted">No alerts.</p>'}</div>`;
  },
  async ops() {
    const [bus, dlq, health] = await Promise.all([api("GET", "/audit/ops/bus"), api("GET", "/audit/ops/deadletters"), api("GET", "/health")]);
    return `<div class="card"><h2>Service health</h2>${Object.entries(health.services).map(([k, v]) => `${esc(k)} ${chip(v === "up" ? "active" : "failed")}`).join(" &nbsp; ")}</div>
      <div class="card"><h2>Service Bus subscriptions</h2><table><tr><th>Subscription</th><th>Active (backlog)</th><th>Completed</th><th>Dead-lettered</th></tr>
      ${Object.entries(bus.subscriptions).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.active || 0}</td><td>${v.completed || 0}</td><td>${v.deadletter || 0}</td></tr>`).join("")}</table></div>
      <div class="card"><h2>Dead-letter queue</h2>${dlq.length ? `<table><tr><th>#</th><th>Subscription</th><th>Event</th><th>Reason</th><th>Error</th><th></th></tr>
      ${dlq.map((m) => `<tr><td>${m.seq}</td><td>${esc(m.subscription)}</td><td>${esc(m.event_type)}</td><td>${esc(m.dead_letter_reason)}</td><td class="small">${esc(m.last_error)}</td>
      <td><button class="sm" data-action="resubmit" data-id="${m.seq}">Resubmit</button></td></tr>`).join("")}</table>` : '<p class="muted">Empty.</p>'}</div>`;
  },
};

/* ---------------- fragments ---------------- */
function timelineHtml(tl) {
  if (!tl.length) return '<p class="muted">No entries yet.</p>';
  return `<ul class="timeline">${tl.map((e) => `<li><b>${esc(e.title)}</b><span class="muted small">${esc(e.occurred_at.replace("T", " ").slice(0, 16))} · ${esc(e.kind)}${e.author ? " · " + esc(e.author) : ""}</span>
    ${e.detail && e.detail.abnormal && e.detail.abnormal.length ? `<div class="small">${esc(e.detail.abnormal.join(", "))}</div>` : ""}
    ${e.detail && e.detail.assessment ? `<div class="small">Assessment: ${esc(e.detail.assessment)}</div>` : ""}</li>`).join("")}</ul>`;
}
function apptTable(list, patientView, doctorView) {
  if (!list.length) return '<p class="muted">No appointments.</p>';
  return `<div class="table-wrap"><table><tr><th>When</th><th>${patientView ? "Doctor" : "Patient"}</th><th>Reason</th><th>Status</th><th></th></tr>
    ${list.map((a) => `<tr><td>${esc(a.start_at.replace("T", " "))}</td><td>${patientView ? esc(a.doctor_name) + `<div class="muted small">${esc(a.specialty)}</div>` : `<span class="pid">${esc(a.patient_id)}</span>`}</td>
    <td>${esc(a.reason)}</td><td>${chip(a.status)}</td><td>${a.status === "booked" ? (doctorView
      ? `<button class="sm" data-action="open-patient" data-id="${esc(a.patient_id)}">Open</button> <button class="sm primary" data-action="appt-complete" data-id="${esc(a.id)}">Complete</button> <button class="sm" data-action="appt-noshow" data-id="${esc(a.id)}">No-show</button>`
      : `<button class="sm danger" data-action="appt-cancel" data-id="${esc(a.id)}">Cancel</button>`) : ""}</td></tr>`).join("")}</table></div>`;
}
function invoiceTable(list, canPay, staff) {
  if (!list.length) return '<p class="muted">No invoices.</p>';
  return `<div class="table-wrap"><table><tr><th>Invoice</th>${staff ? "<th>Patient</th>" : ""}<th>Description</th><th>Amount</th><th>Status</th><th></th></tr>
    ${list.map((i) => `<tr><td class="pid small">${esc(i.id)}</td>${staff ? `<td class="pid small">${esc(i.patient_id)}</td>` : ""}<td>${esc(i.description)}</td>
    <td>${money(i.amount_paise)}</td><td>${chip(i.status)}</td><td>${i.status === "open" ? (canPay
      ? `<button class="sm primary" data-action="pay" data-id="${esc(i.id)}">Pay (UPI)</button>`
      : `<button class="sm" data-action="cash" data-id="${esc(i.id)}">Record cash</button> <button class="sm danger" data-action="void" data-id="${esc(i.id)}">Void</button>`) : ""}</td></tr>`).join("")}</table></div>`;
}
function docsTable(docs) {
  if (!docs.length) return '<p class="muted">No documents.</p>';
  return `<div class="table-wrap"><table><tr><th>File</th><th>Type</th><th>Status</th><th>Extracted results</th><th></th></tr>
    ${docs.map((d) => {
      const ex = d.extraction || {};
      const labs = (ex.lab_results || []).map((l) => `${esc(l.test)}: ${esc(l.value)} ${esc(l.unit)} ${l.flag !== "normal" ? chip(l.flag) : ""}`).join("<br>");
      return `<tr><td>${esc(d.filename)}<div class="muted small">${dt(d.created_at)}</div></td><td>${esc(d.detected_type || d.declared_type)}</td>
      <td>${chip(d.status)}</td><td class="small">${labs || esc(ex.note || "")}</td><td><button class="sm" data-action="download" data-id="${esc(d.id)}">Download</button></td></tr>`;
    }).join("")}</table></div>`;
}
function uploadCard(pid) {
  return `<div class="card"><h2>Upload a report</h2><form data-form="upload" data-pid="${esc(pid)}" class="row">
    <label>File (PDF, PNG, JPEG or text, max 5 MB) <input type="file" name="file" required accept=".pdf,.png,.jpg,.jpeg,.txt"></label>
    <label style="max-width:180px">Type <select name="doc_type"><option value="lab_report">Lab report</option><option value="prescription">Prescription</option>
    <option value="bill">Bill</option><option value="discharge_summary">Discharge summary</option><option value="imaging">Imaging</option><option value="other">Other</option></select></label>
    <button class="primary">Upload</button></form><p class="muted small">Files are virus-type checked, encrypted at rest and processed in the background.</p></div>`;
}
function patientList(list) {
  if (!list.length) return '<p class="muted">No patients on your care team yet. Patients appear here when they book with you.</p>';
  return `<table><tr><th>Patient ID</th><th>Name</th><th>Age</th><th></th></tr>${list.map((p) => `<tr><td class="pid">${esc(p.patient_id)}</td><td>${esc(p.full_name)}</td>
    <td>${esc(p.age)}</td><td><button class="sm primary" data-action="open-patient" data-id="${esc(p.patient_id)}">Open</button></td></tr>`).join("")}</table>`;
}
function auditTable(rows) {
  return `<div class="table-wrap"><table><tr><th>#</th><th>Time</th><th>Service</th><th>Actor</th><th>Action</th><th>Patient</th><th>Outcome</th></tr>
    ${rows.map((r) => `<tr><td>${r.seq}</td><td class="small">${esc(r.ts.slice(0, 19).replace("T", " "))}</td><td>${esc(r.service)}</td>
    <td class="small">${esc(r.actor)} ${r.role ? chip(r.role) : ""}</td><td>${esc(r.action)}</td><td class="pid small">${esc(r.patient_id)}</td>
    <td>${chip(r.outcome)} ${r.severity !== "info" ? chip(r.severity) : ""}</td></tr>`).join("")}</table></div>`;
}

/* ---------------- doctor: patient workspace ---------------- */
async function openPatient(pid) {
  S.patient = pid;
  if (S.tab !== "patients") await show("patients");
  const box = $("#pdetail");
  box.innerHTML = '<div class="card muted">Loading patient…</div>';
  try {
    const [p, tl, rx, docs, sums] = await Promise.all([
      api("GET", `/patient/patients/${pid}`), api("GET", `/patient/patients/${pid}/timeline?limit=50`),
      api("GET", `/prescription/patients/${pid}/prescriptions`), api("GET", `/document/patients/${pid}/documents`),
      api("GET", `/ai/ai/patients/${pid}/summaries`)]);
    box.innerHTML = `<div class="card idcard"><div><div class="pid">${esc(p.patient_id)}</div><b>${esc(p.full_name)}</b>
      <span class="muted"> · ${esc(p.age)} y · ${esc(p.sex)} · ${esc(p.blood_group || "")}</span></div>
      <div><b>Allergies:</b> ${esc((p.allergies || []).join(", ") || "none")}<br><b>Conditions:</b> ${esc((p.chronic_conditions || []).join(", ") || "none")}</div></div>
    <div class="card"><h2>AI clinical summary</h2><div class="banner">AI drafts, clinicians decide. Summaries are compiled only from source records, cite every statement, and never diagnose.</div>
      <button class="primary" data-action="summarize" data-id="${esc(pid)}">Generate summary</button><div id="summary">${sums[0] ? summaryHtml(sums[0]) : ""}</div></div>
    <div class="grid2"><div class="card"><h2>Timeline</h2>${timelineHtml(tl)}</div>
      <div><div class="card"><h2>Add consultation note</h2><form data-form="consult" data-pid="${esc(pid)}">
        <label>Chief complaint <input name="chief_complaint" required></label><label>Clinical notes <textarea name="clinical_notes"></textarea></label>
        <label>Assessment <input name="assessment"></label><label>Plan <input name="plan"></label><button class="primary">Save note</button></form></div>
      <div class="card"><h2>Diet plan</h2><form data-form="diet" data-pid="${esc(pid)}"><textarea name="plan">${esc(p.diet_plan || "")}</textarea><button>Save diet plan</button></form></div></div></div>
    <div class="card"><h2>Prescriptions</h2>${rxDoctorTable(rx)}
      <h3>New prescription</h3><form data-form="rx" data-pid="${esc(pid)}" class="grid3">
      <label>Medication <input name="medication" required></label><label>Dosage <input name="dosage" placeholder="500 mg" required></label>
      <label>Frequency <input name="frequency" placeholder="twice daily" required></label><label>Route <select name="route"><option>oral</option><option>topical</option><option>inhalation</option><option>injection</option><option>sublingual</option><option>other</option></select></label>
      <label>Duration (days) <input name="duration_days" type="number" value="30" min="1" max="365"></label><label>Refills <input name="refills_allowed" type="number" value="0" min="0" max="12"></label>
      <label>Instructions <input name="instructions"></label><label class="check"><input type="checkbox" name="acknowledge_warnings"> Acknowledge safety warnings</label>
      <div><button class="primary">Create (pending approval)</button></div></form></div>
    ${uploadCard(pid)}<div class="card"><h2>Documents</h2>${docsTable(docs)}</div>`;
    box.scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    box.innerHTML = `<div class="card">${esc(e.message)}${e.status === 403 ? "<p class='muted'>This patient is not on your care team. Use emergency access only if clinically justified.</p>" : ""}</div>`;
  }
}
function rxDoctorTable(list) {
  if (!list.length) return '<p class="muted">No prescriptions.</p>';
  return `<div class="table-wrap"><table><tr><th>Medicine (approved)</th><th>Status</th><th>Pending change</th><th></th></tr>${list.map((r) => {
    const cur = r.current, pend = r.pending_change;
    return `<tr><td>${cur ? `<b>${esc(cur.medication)}</b> ${esc(cur.dosage)}, ${esc(cur.frequency)}<div class="muted small">v${cur.version} · until ${esc(cur.expiry_date)} · refills ${r.refills_remaining}</div>` : '<span class="muted">not yet approved</span>'}</td>
    <td>${chip(r.status)}</td><td>${pend ? `v${pend.version} ${esc(pend.change_type)}: ${esc(pend.medication)} ${esc(pend.dosage)}${pend.warnings.length ? "<br>" + chip("warning") + " " + esc(pend.warnings.join("; ")) : ""}
      <br><button class="sm primary" data-action="rx-approve" data-id="${esc(r.id)}" data-v="${pend.version}">Approve</button> <button class="sm danger" data-action="rx-reject" data-id="${esc(r.id)}" data-v="${pend.version}">Reject</button>` : ""}</td>
    <td>${r.status === "active" && !pend ? `<button class="sm" data-action="rx-renew" data-id="${esc(r.id)}">Renew</button> <button class="sm danger" data-action="rx-stop" data-id="${esc(r.id)}">Discontinue</button>` : ""}
      ${r.status === "expired" && !pend ? `<button class="sm" data-action="rx-renew" data-id="${esc(r.id)}">Renew</button>` : ""}</td></tr>`;
  }).join("")}</table></div>`;
}
function summaryHtml(s) {
  return `<div style="margin-top:12px">${chip(s.status)} <span class="muted small">${esc(s.model)} · ${dt(s.created_at)} · grounding: ${s.content.grounding ? s.content.grounding.ungrounded_bullets_dropped : 0} ungrounded statements removed</span>
    ${s.content.sections.map((sec) => `<div class="summary-sec"><h3>${esc(sec.title)}</h3><ul>${sec.bullets.map((b) => `<li>${esc(b.text)} <span class="cite">[${b.sources.map((x) => esc(x.type + ":" + x.id)).join(", ")}]</span></li>`).join("")}</ul></div>`).join("")}
    <p class="muted small">${esc(s.disclaimer)}</p>
    ${s.status === "draft" ? `<button class="sm primary" data-action="sum-review" data-id="${esc(s.id)}" data-d="approve">Mark reviewed & approve</button> <button class="sm danger" data-action="sum-review" data-id="${esc(s.id)}" data-d="reject">Reject</button>` : ""}</div>`;
}

/* ---------------- actions ---------------- */
const ACTIONS = {
  logout,
  tab: (el) => show(el.dataset.tab),
  async "appt-cancel"(el) { if (!confirm("Cancel this appointment?")) return; await api("POST", `/appointment/appointments/${el.dataset.id}/cancel`, { reason: "cancelled via portal" }); toast("Appointment cancelled"); show(S.tab); },
  async "appt-complete"(el) { const notes = prompt("Visit notes (optional)") ?? ""; await api("POST", `/appointment/appointments/${el.dataset.id}/complete`, { visit_notes: notes }); toast("Visit completed"); show(S.tab); },
  async "appt-noshow"(el) { await api("POST", `/appointment/appointments/${el.dataset.id}/no-show`); toast("Marked as no-show"); show(S.tab); },
  async book(el) {
    await api("POST", "/appointment/appointments", { doctor_id: el.dataset.doc, start_at: el.dataset.slot, reason: prompt("Reason for visit (optional)") || "" });
    toast("Appointment booked - confirmation on its way"); show("appointments");
  },
  async refill(el) { await api("POST", `/prescription/prescriptions/${el.dataset.id}/refill-requests`, { note: "" }); toast("Refill requested - awaiting doctor approval"); show(S.tab); },
  async "refill-decide"(el) { await api("POST", `/prescription/refill-requests/${el.dataset.id}/${el.dataset.d}`, { note: "" }); toast("Refill " + el.dataset.d + "d"); show(S.tab); },
  async pay(el) {
    el.disabled = true;
    const key = el.dataset.key || (el.dataset.key = uuid()); // same key on retry => never double-charged
    const r = await api("POST", `/billing/invoices/${el.dataset.id}/pay`, { method: "upi" }, { headers: { "Idempotency-Key": key } });
    toast(r.status === "succeeded" ? "Payment verified - receipt will be emailed" : "Payment " + r.status, r.status !== "succeeded"); show(S.tab);
  },
  async cash(el) { const r = await api("POST", `/billing/invoices/${el.dataset.id}/pay`, { method: "cash" }, { headers: { "Idempotency-Key": uuid() } }); toast("Cash payment " + r.status); show(S.tab); },
  async void(el) { if (!confirm("Void this invoice?")) return; await api("POST", `/billing/invoices/${el.dataset.id}/void`); toast("Invoice voided"); show(S.tab); },
  async download(el) {
    const r = await fetch(`/api/document/documents/${el.dataset.id}/download`, { headers: { Authorization: "Bearer " + S.token } });
    if (!r.ok) return toast("Download failed", true);
    const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = el.dataset.id; a.click();
  },
  "open-patient": (el) => openPatient(el.dataset.id),
  async summarize(el) { el.disabled = true; const s = await api("POST", `/ai/ai/patients/${el.dataset.id}/summary`); $("#summary").innerHTML = summaryHtml(s); el.disabled = false; },
  async "sum-review"(el) { const s = await api("POST", `/ai/ai/summaries/${el.dataset.id}/review`, { decision: el.dataset.d, notes: "" }); $("#summary").innerHTML = summaryHtml(s); },
  async "rx-approve"(el) { await api("POST", `/prescription/prescriptions/${el.dataset.id}/versions/${el.dataset.v}/approve`, { note: "" }); toast("Approved and sent to patient"); openPatient(S.patient); },
  async "rx-reject"(el) { const reason = prompt("Reason for rejecting"); if (!reason) return; await api("POST", `/prescription/prescriptions/${el.dataset.id}/versions/${el.dataset.v}/reject`, { reason }); openPatient(S.patient); },
  async "rx-renew"(el) { const days = Number(prompt("Renew for how many days?", "30")); if (!days) return; await api("POST", `/prescription/prescriptions/${el.dataset.id}/renew`, { duration_days: days, refills_allowed: 0, reason: "renewal" }); toast("Renewal pending approval"); openPatient(S.patient); },
  async "rx-stop"(el) { const reason = prompt("Reason for discontinuing"); if (!reason) return; await api("POST", `/prescription/prescriptions/${el.dataset.id}/discontinue`, { reason }); toast("Discontinuation pending approval"); openPatient(S.patient); },
  async unlock(el) { await api("POST", `/identity/admin/users/${el.dataset.id}/unlock`); toast("Unlocked"); show(S.tab); },
  async resubmit(el) { await api("POST", `/audit/ops/deadletters/${el.dataset.id}/resubmit`); toast("Message resubmitted"); show(S.tab); },
};

const FORMS = {
  async contact(f) { const d = formData(f); await api("PATCH", `/patient/patients/${S.user.patient_id}/contact`, d); toast("Saved"); },
  async slots(f) {
    const d = formData(f);
    const r = await api("GET", `/appointment/doctors/${d.doctor_id}/slots?date=${d.date}`);
    $("#slots").innerHTML = r.slots.length ? `<div class="slots">${r.slots.map((s) => `<button class="sm" data-action="book" data-doc="${esc(d.doctor_id)}" data-slot="${esc(s)}">${esc(s.slice(11))}</button>`).join("")}</div><p class="muted small">Click a time to book.</p>`
      : '<p class="muted">No free slots that day.</p>';
  },
  async chat(f) {
    const d = formData(f); const box = $("#chat");
    box.insertAdjacentHTML("beforeend", `<div class="msg me">${esc(d.message)}</div>`); f.reset();
    const r = await api("POST", "/ai/ai/chat", d);
    box.insertAdjacentHTML("beforeend", `<div class="msg bot">${esc(r.reply)}${r.sources && r.sources.length ? `<div class="src">Sources: ${r.sources.map((s) => esc(s.title || s.id)).join(", ")}</div>` : ""}${r.safety ? `<div class="src">safety: ${esc(r.safety)}</div>` : ""}</div>`);
    box.scrollTop = box.scrollHeight;
  },
  async upload(f) {
    const fd = new FormData(f);
    const r = await api("POST", `/document/patients/${f.dataset.pid}/documents`, fd);
    toast(`Uploaded ${r.filename} - extracting in background`);
    setTimeout(() => (S.user.role === "doctor" ? openPatient(f.dataset.pid) : show(S.tab)), 2500);
  },
  async psearch(f) { $("#plist").innerHTML = patientList(await api("GET", "/patient/patients?q=" + encodeURIComponent(formData(f).q))); },
  async breakglass(f) {
    const d = formData(f);
    if (!confirm("Emergency access is logged as a critical security event and reviewed. Continue?")) return;
    await api("POST", `/patient/patients/${d.patient_id.trim()}/break-glass`, { reason: d.reason });
    toast("Emergency access granted for 4 hours"); openPatient(d.patient_id.trim());
  },
  async consult(f) { await api("POST", `/patient/patients/${f.dataset.pid}/consultations`, formData(f)); toast("Note saved"); openPatient(f.dataset.pid); },
  async diet(f) { await api("PUT", `/patient/patients/${f.dataset.pid}/diet-plan`, formData(f)); toast("Diet plan saved"); },
  async rx(f) {
    const d = formData(f); d.duration_days = Number(d.duration_days); d.refills_allowed = Number(d.refills_allowed);
    await api("POST", `/prescription/patients/${f.dataset.pid}/prescriptions`, d); toast("Created - approve it to activate"); openPatient(f.dataset.pid);
  },
  async invoice(f) {
    const d = formData(f);
    const r = await api("POST", "/billing/invoices", { patient_id: d.patient_id.trim(), description: d.description, amount_paise: Math.round(Number(d.amount) * 100) });
    toast("Invoice " + r.id + " created"); f.reset();
  },
  async user(f) {
    const d = formData(f); if (!d.doctor_id) delete d.doctor_id;
    const r = await api("POST", "/identity/admin/users", d);
    $("#mfa-out").innerHTML = `<div class="banner">Account created. Give this MFA secret to the user once (authenticator app):<br><b class="pid">${esc(r.mfa_secret)}</b><pre>${esc(r.mfa_uri)}</pre></div>`;
  },
  async doctor(f) {
    const d = formData(f);
    const doc = await api("POST", "/appointment/doctors", { name: d.name, specialty: d.specialty, qualifications: d.qualifications,
      fee_paise: Math.round(Number(d.fee) * 100), slot_minutes: Number(d.slot_minutes), languages: d.languages.split(",").map((s) => s.trim()).filter(Boolean) });
    await api("PUT", `/appointment/doctors/${doc.doctor_id}/availability`, [0, 1, 2, 3, 4, 5].map((w) => ({ weekday: w, start: "09:00", end: "13:00" })));
    toast("Doctor " + doc.doctor_id + " added"); show(S.tab);
  },
  async register(f) {
    const r = await api("POST", "/patient/patients", formData(f));
    $("#reg-out").innerHTML = `<div class="banner">Registered. Patient ID: <span class="pid">${esc(r.patient_id)}</span></div>`; f.reset();
  },
  async auditq(f) {
    const d = formData(f); const q = new URLSearchParams(Object.entries(d).filter(([, v]) => v)).toString();
    $("#audit-rows").innerHTML = auditTable(await api("GET", "/audit/audit?" + q));
  },
};

/* ---------------- wiring ---------------- */
document.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-action]");
  if (!el) return;
  ev.preventDefault();
  try { await ACTIONS[el.dataset.action](el); } catch (e) { toast(e.message, true); el.disabled = false; }
});
document.addEventListener("submit", async (ev) => {
  const f = ev.target;
  if (!f.dataset.form) return;
  ev.preventDefault();
  try { await FORMS[f.dataset.form](f); } catch (e) { toast(e.message, true); }
});

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const d = formData(ev.target);
  if (!d.totp) delete d.totp;
  try {
    const r = await api("POST", "/identity/auth/login", d, { quiet401: true });
    S.token = r.access_token; S.user = r; saveSession(); ev.target.reset(); $("#totp-row").classList.add("hidden"); render();
  } catch (e) {
    if (e.message === "mfa_required") { $("#totp-row").classList.remove("hidden"); $("#totp-row input").focus(); toast("Enter the code from your authenticator app"); }
    else toast(e.message, true);
  }
});
$("#show-signup").addEventListener("click", (ev) => { ev.preventDefault(); $("#signup-form").classList.toggle("hidden"); });
$("#signup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const r = await api("POST", "/identity/auth/signup", formData(ev.target));
    S.token = r.access_token; S.user = r; saveSession(); render(); toast("Welcome! Your patient ID is " + r.patient_id);
  } catch (e) { toast(e.message, true); }
});

loadSession();
render();
