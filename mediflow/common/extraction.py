"""Medical document extraction.

LocalExtractor (free): reads text from .txt / text-based PDFs and parses
key-value fields, lab results with units and reference ranges (flagging
high/low), medications and dates using deterministic rules.

AzureDocumentIntelligenceExtractor: same output contract, using the
`prebuilt-layout` model (key-value pairs + tables). Enabled only with
MEDIFLOW_EXTRACTION_MODE=azure.
"""
from __future__ import annotations

import io
import re

LAB_LINE = re.compile(
    r"^\s*(?P<test>[A-Za-z][A-Za-z0-9 ()/,.%-]{1,50}?)\s*[:\-]?\s+"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Zµ/%^0-9.]+(?:/[a-zA-Z0-9.]+)?)?\s*"
    r"(?:\(?\s*(?:ref(?:erence)?(?:\s*range)?\s*[:\-]?\s*)?(?P<lo>\d+(?:\.\d+)?)\s*[-–]\s*(?P<hi>\d+(?:\.\d+)?)\s*\)?)",
    re.IGNORECASE,
)
KV_LINE = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z ./()]{1,40}?)\s*:\s*(?P<val>.{1,200})$")
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")
MED_LINE = re.compile(
    r"^\s*(?:tab\.?|cap\.?|syp\.?|inj\.?|rx:?)?\s*(?P<name>[A-Z][a-zA-Z]{2,30}(?:\s[A-Z]?[a-z]{2,20})?)\s+"
    r"(?P<strength>\d+(?:\.\d+)?\s?(?:mg|mcg|g|ml|iu))\b(?P<rest>.*)$",
    re.IGNORECASE,
)

NON_LAB_LABELS = {"date", "dob", "date of birth", "age", "phone", "mobile", "patient id", "uhid", "sample id",
                  "report date", "collected", "received", "invoice no", "bill no"}

DOC_TYPE_KEYWORDS = {
    "lab_report": ["laboratory", "lab report", "hemoglobin", "haemoglobin", "glucose", "cholesterol", "reference range",
                   "cbc", "blood test", "lipid", "hba1c"],
    "prescription": ["rx", "prescription", "tab.", "cap.", "twice daily", "once daily", "bd", "tds"],
    "bill": ["invoice", "bill", "amount due", "total amount", "gst", "receipt"],
    "discharge_summary": ["discharge summary", "date of admission", "date of discharge"],
    "imaging": ["x-ray", "mri", "ct scan", "ultrasound", "impression"],
}


def extract_text(data: bytes, content_type: str) -> tuple[str, str]:
    """Returns (text, note)."""
    if content_type == "text/plain":
        return data.decode("utf-8", errors="replace"), ""
    if content_type == "application/pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:20])
            return text, "" if text.strip() else "scanned PDF: OCR requires Azure Document Intelligence"
        except Exception as exc:  # malformed PDFs must never crash the worker
            return "", f"could not read PDF: {type(exc).__name__}"
    return "", "image OCR requires Azure Document Intelligence (disabled in free local mode)"


def classify(text: str) -> str:
    lower = text.lower()
    scores = {k: sum(lower.count(w) for w in words) for k, words in DOC_TYPE_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"


def parse(text: str) -> dict:
    labs, fields, meds = [], {}, []
    seen_tests = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(line) > 250:
            continue
        m = LAB_LINE.match(line)
        if m and (float(m.group("lo")) > float(m.group("hi"))
                  or m.group("test").strip(" :-").lower() in NON_LAB_LABELS or DATE.search(line)):
            m = None
        if m:
            test = m.group("test").strip(" :-")
            if test.lower() in seen_tests:
                continue
            seen_tests.add(test.lower())
            value, lo, hi = float(m.group("value")), float(m.group("lo")), float(m.group("hi"))
            flag = "low" if value < lo else "high" if value > hi else "normal"
            labs.append({"test": test, "value": value, "unit": (m.group("unit") or "").strip(),
                         "reference_range": f"{m.group('lo')}-{m.group('hi')}", "flag": flag})
            continue
        mm = MED_LINE.match(line)
        if mm and not any(k in line.lower() for k in ("patient", "date", "doctor")):
            meds.append({"name": mm.group("name").strip(), "strength": mm.group("strength"),
                         "directions": mm.group("rest").strip(" -,")[:120]})
            continue
        kv = KV_LINE.match(line)
        if kv:
            key = kv.group("key").strip().lower().replace(" ", "_")
            if key not in fields and len(fields) < 40:
                fields[key] = kv.group("val").strip()
    dates = list(dict.fromkeys(DATE.findall(text)))[:10]
    return {"fields": fields, "lab_results": labs, "medications": meds, "dates": dates}


class LocalExtractor:
    model = "mediflow-local-rules-v1"

    def extract(self, data: bytes, content_type: str) -> dict:
        text, note = extract_text(data, content_type)
        parsed = parse(text) if text else {"fields": {}, "lab_results": [], "medications": [], "dates": []}
        abnormal = [l for l in parsed["lab_results"] if l["flag"] != "normal"]
        return {
            "model": self.model,
            "doc_type": classify(text) if text else "other",
            **parsed,
            "abnormal_count": len(abnormal),
            "text_excerpt": text[:600],
            "note": note,
        }


class AzureDocumentIntelligenceExtractor:  # pragma: no cover
    model = "azure-prebuilt-layout"

    def __init__(self):
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.identity import DefaultAzureCredential

        from mediflow.common import config

        self.client = DocumentIntelligenceClient(config.env("MEDIFLOW_DOCINTEL_ENDPOINT"), DefaultAzureCredential())

    def extract(self, data: bytes, content_type: str) -> dict:
        poller = self.client.begin_analyze_document("prebuilt-layout", body=data, content_type=content_type,
                                                    features=["keyValuePairs"])
        result = poller.result()
        text = result.content or ""
        parsed = parse(text)
        for kv in result.key_value_pairs or []:
            if kv.key and kv.value:
                parsed["fields"].setdefault(kv.key.content.lower().replace(" ", "_"), kv.value.content)
        return {"model": self.model, "doc_type": classify(text), **parsed,
                "abnormal_count": sum(1 for l in parsed["lab_results"] if l["flag"] != "normal"),
                "text_excerpt": text[:600], "note": ""}


def get_extractor():
    from mediflow.common import config

    return AzureDocumentIntelligenceExtractor() if config.mode("extraction") == "azure" else LocalExtractor()
