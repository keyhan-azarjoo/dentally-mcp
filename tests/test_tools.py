"""End-to-end tool behaviour against a mocked Dentally."""
from __future__ import annotations

import httpx
import pytest

from dentally_mcp import auth, config, runtime, surfaces
from dentally_mcp.client import DentallyClient
from dentally_mcp.errors import DentallyError, WriteBlockedError
from dentally_mcp.ratelimit import RateLimiter
from dentally_mcp.server import build


@pytest.fixture
def dentally(monkeypatch):
    """Route every tool call at an in-process fake Dentally."""
    state = {"requests": [], "routes": {}}

    def handler(request):
        state["requests"].append(request)
        path = request.url.path
        for prefix, response in state["routes"].items():
            if path.endswith(prefix) or prefix in path:
                body = response(request) if callable(response) else response
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": f"no fake route for {path}"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(config, "API_TOKEN", "test-token")
    monkeypatch.setattr(config, "TOKEN_STORE_KEY", "")

    def fake_client(self, practice_id=None):
        return DentallyClient("test-token", transport=transport,
                              limiter=RateLimiter(3_600_000))

    monkeypatch.setattr(auth.Resolver, "client", fake_client)
    return state


async def _call(tool_name: str, **kwargs):
    mcp = build()
    return await mcp.call_tool(tool_name, kwargs)


def _payload(result):
    """FastMCP returns (content, structured) or just content depending on version."""
    if isinstance(result, tuple):
        return result[1] if len(result) > 1 else result[0]
    return result


# --- reads -------------------------------------------------------------------
async def test_search_patients_returns_summaries_not_raw_records(dentally):
    dentally["routes"]["/patients"] = {"patients": [{
        "id": 1, "first_name": "Jane", "last_name": "Doe",
        "date_of_birth": "1990-05-01", "email_address": "jane@example.com",
        "clinical_notes": "must not leak", "tooth_chart": {"11": "caries"},
    }]}
    out = _payload(await _call("search_patients", query="Doe"))
    text = str(out)
    assert "Jane Doe" in text
    assert "must not leak" not in text
    assert "caries" not in text
    assert "jane@example.com" not in text


async def test_a_numeric_query_goes_straight_to_the_patient_record(dentally):
    dentally["routes"]["/patients/42"] = {"patient": {"id": 42, "first_name": "Sam", "last_name": "P"}}
    out = _payload(await _call("search_patients", query="42"))
    assert "Sam P" in str(out)
    assert any("/patients/42" in str(r.url) for r in dentally["requests"])


async def test_list_appointments_defaults_to_today_and_bounds_the_window(dentally):
    dentally["routes"]["/appointments"] = {"appointments": []}
    await _call("list_appointments")
    url = str(dentally["requests"][-1].url)
    assert "start_time=" in url and "finish_time=" in url


async def test_a_huge_date_range_is_clamped_before_it_reaches_dentally(dentally):
    """An unbounded range is the documented way to make this endpoint time out."""
    dentally["routes"]["/appointments"] = {"appointments": []}
    await _call("list_appointments", start_date="2026-01-01", end_date="2027-06-01")
    url = str(dentally["requests"][-1].url)
    assert "2027" not in url


async def test_recalls_due_excludes_patients_who_are_already_booked(dentally):
    """The point of the list is who to chase; already-booked patients are noise."""
    dentally["routes"]["/patients"] = {"patients": [{"id": 1, "first_name": "A", "last_name": "One"},
                                                    {"id": 2, "first_name": "B", "last_name": "Two"}]}
    dentally["routes"]["/appointments"] = {"appointments": [{"id": 9, "patient_id": 2}]}

    out = _payload(await _call("list_recalls_due", start_date="2026-09-01", end_date="2026-09-07"))
    assert out["due_total"] == 2
    assert out["already_booked"] == 1
    assert [p["name"] for p in out["patients"]] == ["A One"]


async def test_care_summary_degrades_instead_of_failing_when_a_module_is_absent(dentally):
    """Several collections are gated by scope or by whether the practice uses them.
    A missing section beats a failed call."""
    dentally["routes"]["/patients/7"] = {"patient": {"id": 7, "first_name": "C", "last_name": "Three"}}
    dentally["routes"]["/appointments"] = {"appointments": []}
    # treatment_plans and accounts deliberately have no route -> 404 from the fake.
    out = _payload(await _call("get_patient_care_summary", patient_id="7"))
    assert out["patient"]["name"] == "C Three"
    assert out["treatment_plans"] == []
    assert out["accounts"] == []


async def test_revenue_summary_totals_invoices_and_payments(dentally):
    dentally["routes"]["/invoices"] = {"invoices": [
        {"id": 1, "total": "100.50", "outstanding": "20.00"},
        {"id": 2, "total": 200, "outstanding": 0},
    ]}
    dentally["routes"]["/payments"] = {"payments": [{"id": 3, "amount": "280.50"}]}
    out = _payload(await _call("revenue_summary", start_date="2026-08-01", end_date="2026-08-31"))
    assert out["invoiced_total"] == 300.5
    assert out["collected_total"] == 280.5
    assert out["outstanding_total"] == 20.0


async def test_money_parsing_survives_currency_symbols(dentally):
    from dentally_mcp.tools.finance import _money
    assert _money("£1,200.50") == 1200.50
    assert _money(None) == 0.0
    assert _money("") == 0.0


# --- write gate --------------------------------------------------------------
async def test_booking_is_refused_while_the_server_is_read_only(dentally, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_WRITES", False)
    with pytest.raises(Exception) as exc:
        await _call("book_appointment", patient_id="1", practitioner_id="2",
                    start_time="2026-09-14T09:30:00")
    assert "read-only" in str(exc.value)
    assert not dentally["requests"], "a blocked write must never reach Dentally"


async def test_a_write_without_a_way_to_confirm_fails_closed(dentally, monkeypatch):
    """A model can be talked into booking by text it read in a note. If the client
    cannot ask a human, the answer is no."""
    monkeypatch.setattr(config, "ALLOW_WRITES", True)
    monkeypatch.setattr(config, "REQUIRE_CONSENT", True)
    monkeypatch.setattr(config, "CONSENT_FALLBACK_ALLOW", False)
    with pytest.raises(WriteBlockedError, match="confirm"):
        await runtime.confirm(None, "Book an appointment", "detail")


async def test_a_refused_confirmation_changes_nothing(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_CONSENT", True)

    class Ctx:
        async def elicit(self, message, schema):
            return type("R", (), {"action": "decline", "data": None})()

    with pytest.raises(WriteBlockedError, match="Cancelled by the user"):
        await runtime.confirm(Ctx(), "Cancel an appointment", "detail")


async def test_an_accepted_confirmation_lets_the_write_through(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_CONSENT", True)

    class Ctx:
        async def elicit(self, message, schema):
            return type("R", (), {"action": "accept",
                                  "data": type("D", (), {"confirm": True})()})()

    await runtime.confirm(Ctx(), "Book an appointment", "detail")  # must not raise


# --- role scoping ------------------------------------------------------------
async def test_a_tool_outside_the_role_is_refused_even_if_the_client_calls_it(dentally):
    """Filtering tools/list alone would be advisory — a client can cache a name."""
    token = auth.current_surface.set("nurse")
    try:
        # FastMCP re-wraps tool exceptions as ToolError, so match on the message the
        # assistant will actually receive rather than on our own exception class.
        with pytest.raises(Exception, match="not available to the 'nurse' role"):
            await _call("revenue_summary")
    finally:
        auth.current_surface.reset(token)
    assert not dentally["requests"]


async def test_tools_list_is_filtered_to_the_role():
    mcp = build()
    token = auth.current_surface.set("nurse")
    try:
        names = {t.name for t in await mcp.list_tools()}
    finally:
        auth.current_surface.reset(token)
    assert names == surfaces.visible("nurse")
    assert "revenue_summary" not in names


async def test_every_registered_tool_belongs_to_at_least_one_surface():
    """A tool missing from every surface is unreachable but still looks shipped."""
    mcp = build()
    token = auth.current_surface.set("full")
    try:
        registered = {t.name for t in await mcp.list_tools()}
    finally:
        auth.current_surface.reset(token)
    assert registered == surfaces.SURFACES["full"]


async def test_every_write_tool_actually_receives_a_context():
    """Regression guard for a silent failure.

    The tool modules use `from __future__ import annotations`, so their hints are
    strings, and FastMCP resolves a tool's hints against the *wrapper's* globals.
    Without the annotation fix-up in `runtime.tool`, `Context` would not resolve,
    FastMCP would decide the tool wants no context, `ctx` would arrive as None, and
    every write would lose its confirmation prompt while still looking wired up.
    """
    mcp = build()
    for name in surfaces.WRITE_TOOLS:
        tool = mcp._tool_manager.get_tool(name)
        assert tool is not None, name
        assert tool.context_kwarg == "ctx", f"{name} would never be able to ask for confirmation"


async def test_ctx_is_not_advertised_as_a_model_supplied_argument():
    """An injected Context must not leak into the schema as something to fill in."""
    mcp = build()
    schema = mcp._tool_manager.get_tool("book_appointment").parameters
    assert "ctx" not in schema.get("properties", {})


async def test_an_unknown_tool_name_is_an_error_not_a_silent_no_op(dentally):
    with pytest.raises(Exception):
        await _call("delete_everything")


async def test_a_missing_record_surfaces_as_a_clear_message(dentally):
    """No route registered -> the fake answers 404, as Dentally would."""
    with pytest.raises(Exception, match="(?i)not found"):
        await _call("get_appointment", appointment_id="999")


async def test_error_types_are_exported_for_callers():
    assert issubclass(WriteBlockedError, DentallyError)


async def test_every_tool_has_a_description_for_the_model():
    mcp = build()
    token = auth.current_surface.set("full")
    try:
        for tool in await mcp.list_tools():
            assert tool.description and len(tool.description) > 30, tool.name
    finally:
        auth.current_surface.reset(token)
