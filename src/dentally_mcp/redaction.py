"""Shape Dentally records into the minimum an assistant needs to answer.

This is data minimisation, not cosmetics. A UK dental record is special category
personal data (UK GDPR Art. 9): every field that reaches the model is a field that
reaches the model's provider, its logs, and its context window. So each tool returns
a hand-picked projection rather than the raw API object, and two categories never
leave this process at all:

* **tooth/surface-level clinical detail** — Dentally's own integration guidance flags
  it as data that should not be synced outward, and no scheduling, recall or billing
  question needs it;
* **free-text clinical notes** — unbounded, and the single most likely place for
  another patient's identity to appear.

`REDACT_PII` additionally masks contact details and narrows date of birth to an age.
Names survive, because "who is my 3pm" is unanswerable without them.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

from . import config

# Substrings that mark a field as clinical detail we never forward. Matching on the
# key (not a curated allow-list of exact names) means a new Dentally field is excluded
# by default rather than leaking until someone notices it.
_CLINICAL_MARKERS = (
    "tooth", "teeth", "surface", "chart", "perio", "bpe", "pocket",
    "diagnosis", "clinical_note", "note_body", "medical_history",
    "medical_condition", "allergy", "allergies", "medication", "prescription",
)

_CONTACT_MARKERS = (
    "email", "phone", "mobile", "telephone", "address", "postcode",
    "post_code", "zip", "nhs_number", "ni_number", "insurance",
)


def is_clinical(key: str) -> bool:
    k = key.lower()
    return any(marker in k for marker in _CLINICAL_MARKERS)


def is_contact(key: str) -> bool:
    k = key.lower()
    return any(marker in k for marker in _CONTACT_MARKERS)


def scrub(obj: Any) -> Any:
    """Recursively drop clinical fields, and contact fields when redaction is on.

    Applied as a backstop *after* each tool's own projection, so a field added to a
    projection by mistake still cannot carry clinical detail out.
    """
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if config.BLOCK_CLINICAL_DETAIL and is_clinical(str(key)):
                continue
            if config.REDACT_PII and is_contact(str(key)):
                out[str(key)] = _mask_contact(value)
                continue
            out[str(key)] = scrub(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    return obj


def _mask_contact(value: Any) -> str:
    if value in (None, "", []):
        return ""
    text = str(value)
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{local[:1]}***@{domain}"
    digits = [c for c in text if c.isdigit()]
    if len(digits) >= 6:
        return f"***{''.join(digits[-3:])}"
    return "[redacted]"


def age_from_dob(dob: str | None) -> int | None:
    """An age answers the clinical question; a birth date is an identifier."""
    if not dob:
        return None
    try:
        d = date.fromisoformat(str(dob)[:10])
    except ValueError:
        return None
    today = date.today()
    return today.year - d.year - ((today.month, today.day) < (d.month, d.day))


def patient_summary(p: Mapping[str, Any]) -> dict[str, Any]:
    """The projection every patient-returning tool uses."""
    out: dict[str, Any] = {
        "id": p.get("id"),
        "name": _full_name(p),
        "age": age_from_dob(p.get("date_of_birth")),
        "gender": p.get("gender"),
        "status": p.get("status") or p.get("payment_plan_name"),
        "site_id": p.get("site_id"),
        "active": p.get("active"),
    }
    if not config.REDACT_PII:
        out["date_of_birth"] = p.get("date_of_birth")
        out["email"] = p.get("email_address") or p.get("email")
        out["phone"] = p.get("mobile_phone") or p.get("home_phone")
    return {k: v for k, v in out.items() if v is not None}


def appointment_summary(a: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "id": a.get("id"),
        "start_time": a.get("start_time"),
        "finish_time": a.get("finish_time"),
        "duration_minutes": a.get("duration"),
        "state": a.get("state"),
        "patient_id": a.get("patient_id"),
        "practitioner_id": a.get("practitioner_id"),
        "site_id": a.get("site_id"),
        "reason": a.get("reason") or a.get("appointment_reason"),
        "cancellation_reason_id": a.get("cancellation_reason_id"),
        "confirmed": a.get("confirmed"),
        # `booked_via` matters operationally: it distinguishes a patient's own online
        # booking from one reception made, which changes how you handle a clash.
        "booked_via": a.get("booked_via"),
    }
    return {k: v for k, v in out.items() if v is not None}


def money_summary(m: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "id": m.get("id"),
        "patient_id": m.get("patient_id"),
        "date": m.get("date") or m.get("created_at"),
        "total": m.get("total") or m.get("amount") or m.get("gross"),
        "paid": m.get("paid"),
        "outstanding": m.get("outstanding") or m.get("balance"),
        "state": m.get("state") or m.get("status"),
        "reference": m.get("reference") or m.get("number"),
    }
    return {k: v for k, v in out.items() if v is not None}


def project(records: Iterable[Mapping[str, Any]], fn) -> list[dict[str, Any]]:
    return [scrub(fn(r)) for r in records]


def _full_name(p: Mapping[str, Any]) -> str:
    explicit = p.get("full_name") or p.get("name")
    if explicit:
        return str(explicit)
    parts = [p.get("first_name"), p.get("last_name")]
    return " ".join(str(x) for x in parts if x).strip() or f"patient {p.get('id')}"
