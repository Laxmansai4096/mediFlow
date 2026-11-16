"""Notification worker - emails and WhatsApp messages for hospital events.

Delivery status and retries are retained per (message, channel) so a retry
after a partial failure only re-sends the channel that failed.
"""
from __future__ import annotations

import sqlite3
import time

from mediflow.common import config, service_client
from mediflow.common.bus import BusMessage

from . import channels
from .base import PermanentError, Worker

TEXT = {
    "payment.succeeded": {
        "subject": "Payment receipt {invoice_id}",
        "en": "Dear {name}, we received your payment of {amount} for {description}. Invoice {invoice_id}, "
              "reference {provider_ref}. Thank you.",
        "hi": "प्रिय {name}, {description} के लिए आपका {amount} का भुगतान प्राप्त हुआ। इनवॉइस {invoice_id}, संदर्भ {provider_ref}। धन्यवाद।",
        "te": "ప్రియమైన {name}, {description} కోసం మీ {amount} చెల్లింపు అందింది. ఇన్వాయిస్ {invoice_id}, రిఫరెన్స్ {provider_ref}. ధన్యవాదాలు.",
        "wa_template": "payment_confirmation",
    },
    "appointment.booked": {
        "subject": "Appointment confirmed - {start}",
        "en": "Dear {name}, your appointment with {doctor} ({specialty}) is confirmed for {start}. Ref {appointment_id}.",
        "hi": "प्रिय {name}, {doctor} ({specialty}) के साथ आपका अपॉइंटमेंट {start} के लिए पक्का है। संदर्भ {appointment_id}।",
        "te": "ప్రియమైన {name}, {doctor} ({specialty})తో మీ అపాయింట్‌మెంట్ {start}కి నిర్ధారించబడింది. రిఫరెన్స్ {appointment_id}.",
        "wa_template": "appointment_confirmation",
    },
    "appointment.cancelled": {
        "subject": "Appointment cancelled - {start}",
        "en": "Dear {name}, your appointment with {doctor} on {start} has been cancelled.",
        "hi": "प्रिय {name}, {start} को {doctor} के साथ आपका अपॉइंटमेंट रद्द कर दिया गया है।",
        "te": "ప్రియమైన {name}, {start}న {doctor}తో మీ అపాయింట్‌మెంట్ రద్దు చేయబడింది.",
        "wa_template": "appointment_cancelled",
    },
    "appointment.reminder": {
        "subject": "Reminder: appointment {start}",
        "en": "Reminder: {name}, you have an appointment with {doctor} at {start}.",
        "hi": "याद दिलाना: {name}, {start} पर {doctor} के साथ आपका अपॉइंटमेंट है।",
        "te": "రిమైండర్: {name}, {start}కి {doctor}తో మీకు అపాయింట్‌మెంట్ ఉంది.",
        "wa_template": "appointment_reminder",
    },
    "prescription.approved": {
        "subject": "Your prescription has been updated",
        "en": "Dear {name}, your prescription for {medication} ({dosage}, {frequency}) is active until {expiry}.",
        "hi": "प्रिय {name}, {medication} ({dosage}, {frequency}) का आपका पर्चा {expiry} तक सक्रिय है।",
        "te": "ప్రియమైన {name}, {medication} ({dosage}, {frequency}) మీ ప్రిస్క్రిప్షన్ {expiry} వరకు యాక్టివ్‌గా ఉంది.",
        "wa_template": "prescription_update",
    },
    "prescription.expiring": {
        "subject": "Prescription expiring soon",
        "en": "Dear {name}, your prescription for {medication} expires on {expiry}. Request a refill or book a review.",
        "hi": "प्रिय {name}, {medication} का आपका पर्चा {expiry} को समाप्त हो रहा है। रिफिल का अनुरोध करें या समीक्षा बुक करें।",
        "te": "ప్రియమైన {name}, {medication} మీ ప్రిస్క్రిప్షన్ {expiry}న ముగుస్తుంది. రీఫిల్ అభ్యర్థించండి లేదా సమీక్ష బుక్ చేయండి.",
        "wa_template": "prescription_expiring",
    },
    "refill.approved": {
        "subject": "Refill approved",
        "en": "Dear {name}, your refill for {medication} has been approved. Refills remaining: {remaining}.",
        "hi": "प्रिय {name}, {medication} का आपका रिफिल स्वीकृत हो गया है। शेष रिफिल: {remaining}।",
        "te": "ప్రియమైన {name}, {medication} మీ రీఫిల్ ఆమోదించబడింది. మిగిలిన రీఫిల్స్: {remaining}.",
        "wa_template": "refill_approved",
    },
    "patient.registered": {
        "subject": "Welcome to MediFlow - your patient ID",
        "en": "Welcome {name}! Your MediFlow patient ID is {patient_id}. Keep it handy for every visit.",
        "hi": "स्वागत है {name}! आपकी MediFlow रोगी आईडी {patient_id} है। हर विज़िट पर इसे साथ रखें।",
        "te": "స్వాగతం {name}! మీ MediFlow పేషెంట్ ఐడీ {patient_id}. ప్రతి సందర్శనకు దీన్ని దగ్గర ఉంచుకోండి.",
        "wa_template": "welcome_patient_id",
    },
}


class NotificationWorker(Worker):
    name = "notification"
    subscription = "notification"

    def __init__(self):
        super().__init__()
        self.db_path = config.data_dir() / "notifications.db"
        with self._db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                message_id TEXT, channel TEXT, recipient TEXT, template TEXT, status TEXT,
                attempts INTEGER DEFAULT 0, provider_ref TEXT, last_error TEXT, updated_at REAL,
                PRIMARY KEY (message_id, channel))""")
        for event in TEXT:
            self.on(event, self.notify)

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _contact(self, patient_id: str) -> dict:
        r = service_client.call("patient", "GET", f"/internal/patients/{patient_id}/contact", as_service="notification")
        if r.status_code == 404:
            raise PermanentError(f"unknown patient {patient_id}")
        r.raise_for_status()
        return r.json()

    def _deliver(self, m: BusMessage, channel: str, recipient: str, template: str, send) -> None:
        with self._db() as c:
            row = c.execute("SELECT status FROM deliveries WHERE message_id=? AND channel=?",
                            (m.message_id, channel)).fetchone()
            if row and row[0] == "sent":
                return  # already delivered on a previous attempt
            c.execute("INSERT OR IGNORE INTO deliveries (message_id, channel, recipient, template, status, updated_at)"
                      " VALUES (?,?,?,?, 'pending', ?)", (m.message_id, channel, recipient, template, time.time()))
            c.execute("UPDATE deliveries SET attempts=attempts+1, updated_at=? WHERE message_id=? AND channel=?",
                      (time.time(), m.message_id, channel))
        try:
            ref = send()
        except Exception as exc:
            with self._db() as c:
                c.execute("UPDATE deliveries SET status='retrying', last_error=? WHERE message_id=? AND channel=?",
                          (str(exc)[:300], m.message_id, channel))
            raise
        with self._db() as c:
            c.execute("UPDATE deliveries SET status='sent', provider_ref=?, last_error=NULL WHERE message_id=? AND channel=?",
                      (ref, m.message_id, channel))

    def notify(self, m: BusMessage) -> None:
        b = m.body
        if "patient_id" not in b:
            raise PermanentError("event missing patient_id")
        contact = self._contact(b["patient_id"])
        lang = contact.get("preferred_language", "en")
        amount = f"₹{b.get('amount_paise', 0) / 100:,.2f}"
        fields = {
            "name": contact["full_name"], "patient_id": b["patient_id"], "amount": amount,
            "description": b.get("description", ""), "invoice_id": b.get("invoice_id", ""),
            "provider_ref": b.get("provider_ref", ""), "doctor": b.get("doctor_name", ""),
            "specialty": b.get("specialty", ""), "start": str(b.get("start_at", "")).replace("T", " "),
            "appointment_id": b.get("appointment_id", ""), "medication": b.get("medication", ""),
            "dosage": b.get("dosage", ""), "frequency": b.get("frequency", ""),
            "expiry": b.get("expiry_date", ""), "remaining": b.get("refills_remaining", ""),
        }
        tpl = TEXT[m.event_type]
        text = tpl.get(lang, tpl["en"]).format(**fields)
        subject = tpl["subject"].format(**fields)
        self._deliver(m, "email", contact["email"], m.event_type,
                      lambda: channels.send_email(contact["email"], subject, text))
        if contact.get("whatsapp_opt_in"):  # consent-based messaging only
            self._deliver(m, "whatsapp", contact["phone"], tpl["wa_template"],
                          lambda: channels.send_whatsapp(contact["phone"], tpl["wa_template"], {"text": text}, lang))

    def delivery_log(self, limit: int = 100) -> list[dict]:
        with self._db() as c:
            cols = ["message_id", "channel", "recipient", "template", "status", "attempts", "provider_ref", "last_error"]
            rows = c.execute(f"SELECT {','.join(cols)} FROM deliveries ORDER BY updated_at DESC LIMIT ?", (limit,))
            return [dict(zip(cols, r)) for r in rows]


if __name__ == "__main__":
    NotificationWorker.main()
