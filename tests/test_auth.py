"""Credential resolution, the client-auth gate, the write gate, and token storage."""
from __future__ import annotations

import httpx
import pytest

from dentally_mcp import auth, config
from dentally_mcp.errors import AuthError, WriteBlockedError
from dentally_mcp.oauth import OAuthFlow
from dentally_mcp.tokenstore import PracticeToken, TokenStore

try:
    from cryptography.fernet import Fernet
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

crypto_only = pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography not installed")


# --- the gate on OUR clients -------------------------------------------------
def test_client_auth_rejects_a_wrong_or_missing_token(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_AUTH_TOKEN", "correct-token")
    assert auth.verify_client("Bearer correct-token")
    assert not auth.verify_client("Bearer wrong-token")
    assert not auth.verify_client("correct-token")   # no scheme
    assert not auth.verify_client(None)
    assert not auth.verify_client("Basic correct-token")


def test_client_auth_is_open_only_when_no_token_is_configured(monkeypatch):
    """Open here is safe only because server.py refuses a non-loopback bind
    without a token — see test_refuses_to_expose_an_unauthenticated_server."""
    monkeypatch.setattr(config, "CLIENT_AUTH_TOKEN", "")
    assert auth.verify_client(None)


def test_refuses_to_expose_an_unauthenticated_server(monkeypatch):
    from dentally_mcp import server

    monkeypatch.setattr(config, "CLIENT_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "HOST", "0.0.0.0")
    with pytest.raises(SystemExit, match="Refusing to start"):
        server._check_http_safety()


def test_loopback_without_a_token_is_allowed(monkeypatch):
    from dentally_mcp import server

    monkeypatch.setattr(config, "CLIENT_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "HOST", "127.0.0.1")
    server._check_http_safety()  # must not raise


# --- the write gate ----------------------------------------------------------
def test_writes_are_blocked_by_default(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_WRITES", False)
    with pytest.raises(WriteBlockedError, match="read-only"):
        auth.assert_writes_enabled("book appointment")


def test_writes_pass_when_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_WRITES", True)
    auth.assert_writes_enabled("book appointment")  # must not raise


# --- credential resolution ---------------------------------------------------
def test_env_token_is_used_when_no_practice_is_named(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "API_TOKEN", "env-token")
    monkeypatch.setattr(config, "TOKEN_STORE_KEY", "")
    cred = auth.Resolver(TokenStore(tmp_path / "t.enc")).resolve()
    assert cred.access_token == "env-token"


def test_no_credential_at_all_says_how_to_fix_it(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "API_TOKEN", "")
    monkeypatch.setattr(config, "TOKEN_STORE_KEY", "")
    with pytest.raises(AuthError, match="docs/LOGIN.md"):
        auth.Resolver(TokenStore(tmp_path / "t.enc")).resolve()


@crypto_only
def test_a_single_stored_practice_is_selected_automatically(monkeypatch, tmp_path):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(config, "API_TOKEN", "")
    monkeypatch.setattr(config, "TOKEN_STORE_KEY", key)
    store = TokenStore(tmp_path / "t.enc", key)
    store.put(PracticeToken(practice_id="p1", access_token="tok-1"))

    cred = auth.Resolver(store).resolve()
    assert cred.practice_id == "p1"


@crypto_only
def test_several_practices_force_the_caller_to_choose(monkeypatch, tmp_path):
    """Guessing which practice a call meant would be a cross-practice data breach."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(config, "API_TOKEN", "")
    monkeypatch.setattr(config, "TOKEN_STORE_KEY", key)
    store = TokenStore(tmp_path / "t.enc", key)
    store.put(PracticeToken(practice_id="p1", access_token="a"))
    store.put(PracticeToken(practice_id="p2", access_token="b"))

    with pytest.raises(AuthError, match="X-Dentally-Practice"):
        auth.Resolver(store).resolve()

    assert auth.Resolver(store).resolve("p2").access_token == "b"


# --- token storage -----------------------------------------------------------
@crypto_only
def test_tokens_are_encrypted_on_disk(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path / "t.enc"
    store = TokenStore(path, key)
    store.put(PracticeToken(practice_id="p1", access_token="super-secret-token"))

    raw = path.read_bytes()
    assert b"super-secret-token" not in raw, "the token must not be readable on disk"
    assert (path.stat().st_mode & 0o777) == 0o600


@crypto_only
def test_a_wrong_key_fails_loudly_rather_than_silently_empty(tmp_path):
    from dentally_mcp.errors import DentallyError

    path = tmp_path / "t.enc"
    TokenStore(path, Fernet.generate_key().decode()).put(
        PracticeToken(practice_id="p1", access_token="x"))

    with pytest.raises(DentallyError, match="does not match"):
        TokenStore(path, Fernet.generate_key().decode()).list_ids()


def test_a_stored_token_is_masked_when_shown(tmp_path):
    tok = PracticeToken(practice_id="p1", access_token="abcdefghijklmnop")
    shown = tok.redacted()
    assert shown["access_token"] == "abcd…mnop"
    assert "efghijkl" not in str(shown)


# --- OAuth flow --------------------------------------------------------------
def test_authorize_url_is_refused_without_a_client_id(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_ID", "")
    from dentally_mcp.errors import DentallyError
    with pytest.raises(DentallyError, match="partner onboarding"):
        OAuthFlow(TokenStore("/tmp/never-written.enc")).authorize_url()


def test_authorize_url_carries_state_and_pkce(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_ID", "cid")
    monkeypatch.setattr(config, "REDIRECT_URI", "https://example.com/auth/callback")
    url, state = OAuthFlow(TokenStore("/tmp/never-written.enc")).authorize_url()
    assert "client_id=cid" in url
    assert f"state={state}" in url
    assert "code_challenge=" in url and "code_challenge_method=S256" in url


async def test_an_unknown_callback_state_is_refused(monkeypatch):
    """A callback we cannot tie to a request we made is replayed or forged."""
    monkeypatch.setattr(config, "CLIENT_ID", "cid")
    flow = OAuthFlow(TokenStore("/tmp/never-written.enc"))
    with pytest.raises(AuthError, match="Unknown or expired OAuth state"):
        await flow.exchange("some-code", "a-state-we-never-issued")


async def test_identify_validates_a_raw_token_against_dentally():
    def handler(request):
        assert request.headers["authorization"] == "Bearer pasted-token"
        return httpx.Response(200, json={"user": {"id": 3, "name": "Dr Smith", "site_id": 9}},
                              headers={"X-OAuth-Scopes": "user:read patient:read"})

    flow = OAuthFlow(TokenStore("/tmp/never-written.enc"),
                     transport=httpx.MockTransport(handler))
    identity = await flow.identify("pasted-token")
    assert identity["user_name"] == "Dr Smith"
    assert identity["scopes"] == ["user:read", "patient:read"]


async def test_a_bad_pasted_token_fails_at_registration_not_at_first_tool_call():
    flow = OAuthFlow(TokenStore("/tmp/never-written.enc"),
                     transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(AuthError):
        await flow.register_api_token("typo-token")


@crypto_only
async def test_keepalive_reports_reauth_without_deleting_the_record(monkeypatch, tmp_path):
    """A practice that must re-authorise needs to be told. Dropping the record
    silently would hide the outage from whoever can actually fix it."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(config, "TOKEN_STORE_KEY", key)
    store = TokenStore(tmp_path / "t.enc", key)
    store.put(PracticeToken(practice_id="p1", access_token="dead"))

    flow = OAuthFlow(store, transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    results = await flow.keepalive_once()

    assert "reauth-required" in results["p1"]
    assert store.get("p1") is not None
