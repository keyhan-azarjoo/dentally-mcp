"""Async HTTP client for the Dentally REST API.

Everything that is Dentally-specific and easy to get wrong lives here, so no tool
has to remember it: the mandatory User-Agent, the 3,600/hour budget, the 100-item
page cap, the "don't page past 100" guidance, and the scope list the server echoes
back on every response.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

import httpx

from . import config
from .errors import DentallyError, UpstreamError, from_response
from .ratelimit import RateLimiter

log = logging.getLogger("dentally_mcp.client")

# Response header Dentally uses to tell you what the presented credential may do.
SCOPE_HEADER = "x-oauth-scopes"


class DentallyClient:
    """One client per credential. Not shared between practices."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str | None = None,
        limiter: RateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not token:
            raise DentallyError("DentallyClient needs an access token.")
        self._token = token
        self.base_url = (base_url or config.API_BASE).rstrip("/")
        self.limiter = limiter or RateLimiter(config.RATE_LIMIT_PER_HOUR)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        # Populated from the first response; lets tools give a precise "you are not
        # authorised for that" instead of surfacing a bare 403.
        self.scopes: list[str] = []

    # -- lifecycle ------------------------------------------------------------
    async def __aenter__(self) -> "DentallyClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.TIMEOUT, connect=config.CONNECT_TIMEOUT),
                transport=self._transport,
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- request path ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            # Non-negotiable: Dentally answers 403 to any request without one.
            "User-Agent": config.USER_AGENT,
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        url = config.api_url(path) if not path.startswith("http") else path
        if self.base_url != config.API_BASE and not path.startswith("http"):
            url = url.replace(config.API_BASE, self.base_url, 1)

        clean = {k: v for k, v in (params or {}).items() if v is not None}
        client = await self._ensure_client()

        # Retries are for idempotent reads only. Replaying a POST would double-book an
        # appointment slot, which is far worse than surfacing the error.
        attempts = config.GET_RETRIES + 1 if method.upper() == "GET" else 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            await self.limiter.acquire()
            try:
                resp = await client.request(method, url, params=clean, json=json, headers=self._headers())
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(config.RETRY_BACKOFF * (2 ** attempt))
                    continue
                raise UpstreamError(f"Could not reach Dentally: {exc}") from exc

            self.limiter.observe(resp.headers)
            raw_scopes = resp.headers.get(SCOPE_HEADER)
            if raw_scopes:
                self.scopes = [s.strip() for s in raw_scopes.replace(",", " ").split() if s.strip()]

            if resp.status_code in (502, 503, 504) and attempt + 1 < attempts:
                await asyncio.sleep(config.RETRY_BACKOFF * (2 ** attempt))
                continue

            if resp.status_code == 429 and attempt + 1 < attempts:
                retry_after = _retry_after(resp.headers)
                await asyncio.sleep(min(retry_after, 30.0))
                continue

            if resp.status_code >= 400:
                raise from_response(resp.status_code, _safe_json(resp), sent_user_agent=True)

            if resp.status_code == 204 or not resp.content:
                return None
            return _safe_json(resp)

        raise UpstreamError(f"Could not reach Dentally: {last_exc}")

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, payload: Any) -> Any:
        return await self.request("POST", path, json=payload)

    async def put(self, path: str, payload: Any) -> Any:
        return await self.request("PUT", path, json=payload)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)

    # -- pagination -----------------------------------------------------------
    async def list_all(
        self,
        path: str,
        key: str,
        *,
        limit: int | None = None,
        **params: Any,
    ) -> list[dict]:
        """Page through a collection, respecting Dentally's caps.

        Stops at `config.MAX_PAGES` because Dentally warns that requests beyond
        page 100 are prone to timeouts, and because an assistant that quietly pulls
        40,000 patients into a context window is a data-protection incident, not a
        thorough answer.
        """
        per_page = min(config.MAX_PER_PAGE, limit or config.MAX_PER_PAGE)
        out: list[dict] = []
        for page in range(1, config.MAX_PAGES + 1):
            body = await self.get(path, page=page, per_page=per_page, **params)
            batch = _extract(body, key)
            out.extend(batch)
            if limit and len(out) >= limit:
                return out[:limit]
            if len(batch) < per_page:
                break
        return out

    # -- identity -------------------------------------------------------------
    async def whoami(self) -> dict:
        """Cheapest authenticated call: validates the token and reveals its scopes.

        Doubles as the keepalive ping — Dentally has no refresh endpoint, so *using*
        a token is the only way to keep it alive.
        """
        body = await self.get("user")
        user = body.get("user") if isinstance(body, dict) else None
        return user if isinstance(user, dict) else (body if isinstance(body, dict) else {})


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:2000]}


def _retry_after(headers: Mapping[str, str]) -> float:
    raw = headers.get("retry-after")
    try:
        return float(raw) if raw else 5.0
    except (TypeError, ValueError):
        return 5.0


def _extract(body: Any, key: str) -> list[dict]:
    """Dentally wraps collections in the resource name: {"patients": [...]}."""
    if isinstance(body, dict):
        value = body.get(key)
        if isinstance(value, list):
            return value
        # A single-object response used where a list was expected.
        if isinstance(value, dict):
            return [value]
    if isinstance(body, list):
        return body
    return []


def clamp_date_window(
    start: str | None,
    end: str | None,
    *,
    max_days: int | None = None,
) -> tuple[str, str]:
    """Force a sane, bounded date range.

    Dentally's own guidance is to keep filters on large entities inside roughly three
    months; unbounded ranges are the documented way to make these endpoints time out.
    An absent range defaults to today, not to all of history.
    """
    max_days = max_days or config.MAX_DATE_WINDOW_DAYS
    today = date.today()
    try:
        d_start = date.fromisoformat(start) if start else today
    except ValueError as exc:
        raise DentallyError(f"start_date must be YYYY-MM-DD, got {start!r}") from exc
    try:
        d_end = date.fromisoformat(end) if end else d_start
    except ValueError as exc:
        raise DentallyError(f"end_date must be YYYY-MM-DD, got {end!r}") from exc

    if d_end < d_start:
        d_start, d_end = d_end, d_start
    if (d_end - d_start).days > max_days:
        d_end = d_start + timedelta(days=max_days)
    return d_start.isoformat(), d_end.isoformat()


def has_scope(scopes: Iterable[str], needed: str) -> bool:
    """True when `needed` is granted. An empty scope list means 'not yet observed'."""
    scopes = list(scopes)
    if not scopes:
        return True
    if needed in scopes:
        return True
    # A resource-wide grant covers its narrower forms (patient covers patient:read).
    resource = needed.split(":", 1)[0]
    return resource in scopes
