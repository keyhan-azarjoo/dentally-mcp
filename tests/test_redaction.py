"""Data minimisation. These are the tests that stop a patient record leaking."""
from __future__ import annotations

from dentally_mcp import config, redaction


def test_clinical_fields_never_survive():
    raw = {
        "id": 1,
        "name": "A Patient",
        "tooth_number": 24,
        "surface": "MOD",
        "clinical_notes": "sensitive",
        "medical_history": {"allergies": ["penicillin"]},
        "bpe_scores": [1, 2],
    }
    out = redaction.scrub(raw)
    assert out == {"id": 1, "name": "A Patient"}


def test_clinical_fields_are_stripped_at_any_depth():
    raw = {"plan": {"items": [{"id": 2, "tooth": 11, "price": 100}]}}
    out = redaction.scrub(raw)
    assert out["plan"]["items"][0] == {"id": 2, "price": 100}


def test_a_new_unknown_clinical_field_is_excluded_by_default():
    """Matching on the key name rather than an allow-list means a field Dentally adds
    tomorrow is dropped, not leaked until someone notices."""
    out = redaction.scrub({"id": 1, "perio_pocket_depths": [3, 4]})
    assert "perio_pocket_depths" not in out


def test_contact_details_are_masked_when_redaction_is_on(monkeypatch):
    monkeypatch.setattr(config, "REDACT_PII", True)
    out = redaction.scrub({
        "email_address": "jane.doe@example.com",
        "mobile_phone": "07700900123",
        "postcode": "SW1A 1AA",
    })
    assert out["email_address"] == "j***@example.com"
    assert out["mobile_phone"] == "***123"
    assert "@example.com" in out["email_address"]
    assert "jane.doe" not in out["email_address"]


def test_patient_summary_gives_an_age_not_a_birth_date(monkeypatch):
    monkeypatch.setattr(config, "REDACT_PII", True)
    out = redaction.patient_summary({
        "id": 5, "first_name": "Jane", "last_name": "Doe",
        "date_of_birth": "1990-01-01", "email_address": "j@example.com",
    })
    assert out["name"] == "Jane Doe"
    assert isinstance(out["age"], int) and out["age"] > 30
    assert "date_of_birth" not in out
    assert "email" not in out


def test_turning_redaction_off_returns_contact_details(monkeypatch):
    """The switch has to actually do something, or it is a false reassurance."""
    monkeypatch.setattr(config, "REDACT_PII", False)
    out = redaction.patient_summary({
        "id": 5, "first_name": "Jane", "last_name": "Doe",
        "date_of_birth": "1990-01-01", "email_address": "j@example.com",
    })
    assert out["date_of_birth"] == "1990-01-01"
    assert out["email"] == "j@example.com"


def test_clinical_detail_stays_blocked_even_with_redaction_off(monkeypatch):
    """PII redaction is a setting; clinical detail is not."""
    monkeypatch.setattr(config, "REDACT_PII", False)
    out = redaction.scrub({"id": 1, "tooth_number": 24, "email": "j@example.com"})
    assert "tooth_number" not in out
    assert out["email"] == "j@example.com"


def test_names_survive_because_the_diary_is_unusable_without_them():
    out = redaction.patient_summary({"id": 1, "first_name": "Sam", "last_name": "Patel"})
    assert out["name"] == "Sam Patel"


def test_a_patient_with_no_name_still_gets_an_identifier():
    assert redaction.patient_summary({"id": 9})["name"] == "patient 9"


def test_malformed_date_of_birth_is_not_fatal():
    assert redaction.age_from_dob("not-a-date") is None
    assert redaction.age_from_dob(None) is None


def test_appointment_summary_keeps_only_operational_fields():
    out = redaction.appointment_summary({
        "id": 1, "start_time": "2026-09-01T09:00:00", "state": "active",
        "patient_id": 5, "practitioner_id": 2, "clinical_note": "secret",
    })
    assert out["state"] == "active"
    assert "clinical_note" not in redaction.scrub(out)
