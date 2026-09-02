"""Demo mode: real code path, synthetic upstream, and impossible to mistake for live."""
from __future__ import annotations

import pytest

from dentally_mcp import auth, config, demo
from dentally_mcp.server import build


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "API_TOKEN", "")
    monkeypatch.setattr(config, "TOKEN_STORE_KEY", "")
    t = auth.current_surface.set("full")
    yield
    auth.current_surface.reset(t)


def _payload(result):
    """Unwrap what FastMCP hands back.

    A tool returning a list comes back as {"result": [...]}, because MCP structured
    output has to be a JSON object. Not a quirk of ours — worth unwrapping here so
    the assertions below read as the tool's own return value.
    """
    if isinstance(result, tuple):
        result = result[1] if len(result) > 1 else result[0]
    if isinstance(result, dict) and set(result) == {"result"}:
        return result["result"]
    return result


async def _call(name, **kw):
    return _payload(await build().call_tool(name, kw))


# --- it works without any credential at all ----------------------------------
async def test_demo_needs_no_credential(demo_on):
    """The whole point: an evaluator with no Dentally access can still run this."""
    out = await _call("whoami")
    assert out["practice_id"] == "demo"


async def test_without_demo_the_same_call_demands_a_credential(monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "API_TOKEN", "")
    monkeypatch.setattr(config, "TOKEN_STORE_KEY", "")
    t = auth.current_surface.set("full")
    try:
        with pytest.raises(Exception, match="No Dentally credential"):
            await _call("whoami")
    finally:
        auth.current_surface.reset(t)


# --- it cannot be mistaken for real data -------------------------------------
async def test_whoami_says_demo_in_words_a_human_will_read(demo_on):
    """Silent demo data presented as a real diary is the one way this feature
    could cause harm, so the label is asserted, not assumed."""
    out = await _call("whoami")
    assert "DEMO" in out["environment"]
    assert "NOT real patient data" in out["environment"]


async def test_healthz_flags_demo_mode(demo_on):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from dentally_mcp import http_api

    body = TestClient(Starlette(routes=http_api.routes())).get("/healthz").json()
    assert body["demo_mode"] is True
    assert "DEMO" in body["auth_mode"]


# --- the real code path still runs -------------------------------------------
async def test_redaction_still_strips_clinical_detail_in_demo(demo_on):
    """The demo treatment plans deliberately carry tooth/surface data, so this
    proves redaction is genuinely running rather than there being nothing to strip."""
    raw = str(demo._treatment_plans())
    assert "tooth" in raw and "surface" in raw

    plans = await _call("list_treatment_plans", patient_id="9005")
    text = str(plans)
    assert "tooth" not in text and "surface" not in text
    assert "650.00" in text  # the money survived; only the clinical detail went


async def test_pii_redaction_still_applies_in_demo(demo_on, monkeypatch):
    monkeypatch.setattr(config, "REDACT_PII", True)
    out = await _call("search_patients", query="Testwood")
    assert out[0]["name"] == "Ada Testwood"
    assert "ada@example.invalid" not in str(out)
    assert isinstance(out[0]["age"], int)


async def test_role_scoping_still_applies_in_demo(demo_on):
    t = auth.current_surface.set("nurse")
    try:
        with pytest.raises(Exception, match="not available to the 'nurse' role"):
            await _call("revenue_summary")
    finally:
        auth.current_surface.reset(t)


async def test_writes_are_still_blocked_in_demo(demo_on, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_WRITES", False)
    with pytest.raises(Exception, match="read-only"):
        await _call("book_appointment", patient_id="9001", practitioner_id="501",
                    start_time="2026-09-14T09:30:00")


# --- the data is actually useful ---------------------------------------------
async def test_todays_diary_is_populated(demo_on):
    appts = await _call("list_appointments")
    assert len(appts) >= 3, "a demo with an empty diary demonstrates nothing"
    assert all("start_time" in a for a in appts)


async def test_availability_returns_bookable_slots(demo_on):
    from datetime import date

    slots = await _call("find_appointment_slots", start_date=date.today().isoformat(),
                        duration_minutes=30)
    assert slots and all("start_time" in s for s in slots)


async def test_recalls_due_excludes_the_already_booked(demo_on):
    from datetime import date, timedelta

    out = await _call("list_recalls_due", start_date=date.today().isoformat(),
                      end_date=(date.today() + timedelta(days=6)).isoformat())
    assert out["due_total"] > 0
    assert out["already_booked"] > 0, "nobody excluded means the cross-reference is not running"


async def test_revenue_summary_adds_up(demo_on):
    from datetime import date, timedelta

    out = await _call("revenue_summary",
                      start_date=(date.today() - timedelta(days=5)).isoformat(),
                      end_date=date.today().isoformat())
    assert out["invoiced_total"] > 0
    assert out["collected_total"] > 0


async def test_care_summary_joins_the_records(demo_on):
    out = await _call("get_patient_care_summary", patient_id="9005")
    assert out["patient"]["name"] == "Esme Placeholder"
    assert out["treatment_plans"]
    assert "tooth" not in str(out)
