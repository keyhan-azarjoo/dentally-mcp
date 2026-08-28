"""The Dentally-specific behaviour of the HTTP client."""
from __future__ import annotations

import httpx
import pytest

from dentally_mcp import config
from dentally_mcp.client import DentallyClient, clamp_date_window, has_scope
from dentally_mcp.errors import AuthError, NotFoundError, UpstreamError, ValidationError
from dentally_mcp.ratelimit import RateLimiter


def _client(handler, **kw) -> DentallyClient:
    # A generous limiter: these tests are about the request, not the pacing.
    return DentallyClient("tok", transport=httpx.MockTransport(handler),
                          limiter=RateLimiter(3_600_000), **kw)


async def test_user_agent_is_always_sent():
    """Dentally answers 403 to any request without one, so it can never be optional."""
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"user": {"id": 1}})

    async with _client(handler) as client:
        await client.get("user")

    assert seen["ua"] == config.USER_AGENT
    assert seen["ua"], "a blank User-Agent is exactly what Dentally rejects"
    assert seen["auth"] == "Bearer tok"


async def test_url_is_versioned_once():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        await client.get("patients/7")
    assert seen["url"].endswith("/v1/patients/7")

    async with _client(handler) as client:
        await client.get("v1/patients/7")
    assert seen["url"].count("/v1/") == 1


async def test_scopes_are_learned_from_the_response_header():
    def handler(request):
        return httpx.Response(200, json={"user": {}},
                              headers={"X-OAuth-Scopes": "user:read patient:read"})

    async with _client(handler) as client:
        await client.whoami()
        assert client.scopes == ["user:read", "patient:read"]


@pytest.mark.parametrize(
    "status,expected",
    [(401, AuthError), (403, AuthError), (404, NotFoundError), (422, ValidationError)],
)
async def test_errors_map_to_actionable_types(status, expected):
    async with _client(lambda r: httpx.Response(status, json={"error": "no"})) as client:
        with pytest.raises(expected):
            await client.get("patients")


async def test_401_message_explains_the_missing_refresh_flow():
    """The most confusing failure mode deserves the most specific message."""
    async with _client(lambda r: httpx.Response(401, json={})) as client:
        with pytest.raises(AuthError) as exc:
            await client.get("user")
    assert "no refresh" in str(exc.value).lower()


async def test_get_retries_transient_failures():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"user": {"id": 1}})

    async with _client(handler) as client:
        await client.get("user")
    assert calls["n"] == 3


async def test_writes_are_never_retried():
    """Replaying a POST would double-book a surgery — worse than surfacing the error."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    async with _client(handler) as client:
        with pytest.raises(UpstreamError):
            await client.post("appointments", {"appointment": {}})
    assert calls["n"] == 1


async def test_pagination_stops_on_a_short_page():
    pages = {
        1: [{"id": i} for i in range(100)],
        2: [{"id": 100}],
    }
    seen_pages = []

    def handler(request):
        page = int(request.url.params.get("page", 1))
        seen_pages.append(page)
        return httpx.Response(200, json={"patients": pages.get(page, [])})

    async with _client(handler) as client:
        out = await client.list_all("patients", "patients")
    assert len(out) == 101
    assert seen_pages == [1, 2]


async def test_pagination_respects_the_caller_limit():
    def handler(request):
        return httpx.Response(200, json={"patients": [{"id": i} for i in range(100)]})

    async with _client(handler) as client:
        out = await client.list_all("patients", "patients", limit=5)
    assert len(out) == 5


async def test_pagination_is_bounded_even_if_dentally_never_runs_out():
    """Dentally warns that paging past 100 times out; an unbounded loop is also a
    quiet way to pull an entire patient database into a context window."""
    def handler(request):
        return httpx.Response(200, json={"patients": [{"id": 1}] * 100})

    async with _client(handler) as client:
        out = await client.list_all("patients", "patients")
    assert len(out) == config.MAX_PAGES * 100


def test_date_window_defaults_to_today_not_all_of_history():
    start, end = clamp_date_window(None, None)
    assert start == end


def test_date_window_is_clamped_to_the_documented_maximum():
    start, end = clamp_date_window("2026-01-01", "2027-01-01")
    from datetime import date
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days
    assert span == config.MAX_DATE_WINDOW_DAYS


def test_date_window_repairs_a_reversed_range():
    assert clamp_date_window("2026-03-10", "2026-03-01") == ("2026-03-01", "2026-03-10")


def test_bad_date_is_rejected_with_the_expected_format():
    from dentally_mcp.errors import DentallyError
    with pytest.raises(DentallyError, match="YYYY-MM-DD"):
        clamp_date_window("10/03/2026", None)


def test_scope_check_treats_a_resource_grant_as_covering_its_narrower_forms():
    assert has_scope(["patient"], "patient:read")
    assert has_scope(["patient:read"], "patient:read")
    assert not has_scope(["user:read"], "patient:read")
    # No scopes observed yet is "unknown", not "denied" — refusing here would break
    # every first call, before any response header has been seen.
    assert has_scope([], "patient:read")
