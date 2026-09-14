"""OAuth2 authorization-code flow against Dentally, plus token keepalive.

Two things here are unusual enough to call out:

1. **There is no refresh flow.** Dentally does not hand back a usable refresh token;
   a token stays valid by being *used* and dies when it goes idle. So the thing that
   keeps a production integration alive is not a refresh loop, it is `keepalive()` —
   a periodic authenticated ping. Treat it as load-bearing, not as a health check.

2. **PKCE is on by default.** Even for a confidential server-side client, it costs
   nothing and removes the authorization-code interception class of attack outright.
   If a partner's Dentally OAuth app rejects the `code_challenge` parameters, set
   `DENTALLY_OAUTH_PKCE=0`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from . import config
from .client import DentallyClient
from .errors import AuthError, DentallyError
from .tokenstore import PracticeToken, TokenStore

log = logging.getLogger("dentally_mcp.oauth")

USE_PKCE = os.environ.get("DENTALLY_OAUTH_PKCE", "1").strip().lower() not in ("0", "false", "no", "off")

# How long an in-flight authorization may stay pending before its state is dropped.
STATE_TTL_SECONDS = 600

# Hard ceiling on in-flight authorizations.
#
# `/auth/login` is unauthenticated by necessity — a practice has to reach it before
# it has any credential — and every call allocated a pending entry that lived for ten
# minutes. Anyone who could reach the URL could grow that dict without limit and
# exhaust the process's memory. A real onboarding never has more than a handful in
# flight, so the cap costs nothing and closes the hole.
MAX_PENDING_STATES = 256


@dataclass
class PendingAuth:
    state: str
    verifier: str
    practice_hint: str | None
    created_at: float


class OAuthFlow:
    """Server-side half of the authorization-code flow."""

    def __init__(self, store: TokenStore | None = None, *, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store or TokenStore()
        self._pending: dict[str, PendingAuth] = {}
        self._transport = transport

    # -- step 1: send the practice to Dentally --------------------------------
    def authorize_url(self, *, practice_hint: str | None = None) -> tuple[str, str]:
        """Return (url, state). Send the user to `url`; keep `state` to match the callback."""
        if not config.CLIENT_ID:
            raise DentallyError(
                "DENTALLY_CLIENT_ID is not set. OAuth login needs a Dentally OAuth "
                "application, which is issued during partner onboarding — see docs/LOGIN.md. "
                "Until then use single-practice API-token mode (DENTALLY_API_TOKEN)."
            )
        if not config.REDIRECT_URI:
            raise DentallyError("DENTALLY_REDIRECT_URI is not set; it must exactly match the value registered with Dentally.")

        self._expire_stale()
        if len(self._pending) >= MAX_PENDING_STATES:
            # Evict the oldest rather than refuse the request: a flood must not be
            # able to lock a real practice out of starting its own login.
            oldest = min(self._pending.values(), key=lambda p: p.created_at)
            del self._pending[oldest.state]
            log.warning("Pending OAuth states hit the %d cap; evicted the oldest.",
                        MAX_PENDING_STATES)

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        self._pending[state] = PendingAuth(state, verifier, practice_hint, time.time())

        params = {
            "client_id": config.CLIENT_ID,
            "redirect_uri": config.REDIRECT_URI,
            "response_type": "code",
            "scope": config.SCOPES,
            "state": state,
        }
        if USE_PKCE:
            params["code_challenge"] = _s256(verifier)
            params["code_challenge_method"] = "S256"
        return f"{config.AUTHORIZE_URL}?{urlencode(params)}", state

    # -- step 2: exchange the code -------------------------------------------
    async def exchange(self, code: str, state: str) -> PracticeToken:
        self._expire_stale()
        pending = self._pending.pop(state, None)
        if pending is None:
            # A callback we cannot tie to a request we made is either replayed or
            # forged. Refusing it is the entire point of `state`.
            raise AuthError("Unknown or expired OAuth state — restart the login at /auth/login.")

        payload = {
            "client_id": config.CLIENT_ID,
            "client_secret": config.CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": config.REDIRECT_URI,
        }
        if USE_PKCE:
            payload["code_verifier"] = pending.verifier

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.TIMEOUT, connect=config.CONNECT_TIMEOUT),
            transport=self._transport,
        ) as http:
            resp = await http.post(
                config.TOKEN_URL,
                data=payload,
                headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
            )
        if resp.status_code >= 400:
            raise AuthError(
                f"Dentally refused the token exchange ({resp.status_code}). "
                "Check client id/secret and that redirect_uri matches the registered value exactly.",
                status=resp.status_code,
                detail=_safe_json(resp),
            )
        body = _safe_json(resp)
        access = (body or {}).get("access_token")
        if not access:
            raise AuthError("Dentally's token response contained no access_token.", detail=body)

        granted = (body.get("scope") or config.SCOPES or "").split()
        token = PracticeToken(
            practice_id=str(pending.practice_hint or "pending"),
            access_token=access,
            refresh_token=body.get("refresh_token"),
            scopes=granted,
        )

        # Resolve the real practice identity before storing: the practice hint came
        # from our own caller and must not be trusted to key the store.
        identity = await self.identify(access)
        token.practice_id = identity["practice_id"]
        token.practice_name = identity.get("practice_name")
        token.site_id = identity.get("site_id")
        if identity.get("scopes"):
            token.scopes = identity["scopes"]

        return self.store.put(token)

    # -- validation / identity ------------------------------------------------
    async def identify(self, access_token: str) -> dict:
        """Validate a raw token by calling Dentally, and learn who it belongs to.

        This is also the direct-validation path for API-token mode: paste a token,
        we call `/v1/user` with it, and either it works or it does not. No guessing.
        """
        client = DentallyClient(access_token, transport=self._transport)
        try:
            user = await client.whoami()
        finally:
            await client.aclose()

        practice_id = str(
            user.get("practice_id")
            or user.get("site_id")
            or user.get("id")
            or "unknown"
        )
        return {
            "practice_id": practice_id,
            "practice_name": user.get("practice_name") or user.get("name"),
            "site_id": str(user.get("site_id")) if user.get("site_id") is not None else None,
            "user_id": str(user.get("id")) if user.get("id") is not None else None,
            "user_name": user.get("name") or user.get("full_name"),
            "email": user.get("email"),
            "scopes": client.scopes,
        }

    async def register_api_token(self, access_token: str, *, label: str | None = None) -> PracticeToken:
        """Store a practice-supplied API token after proving it works."""
        identity = await self.identify(access_token)
        token = PracticeToken(
            practice_id=identity["practice_id"],
            access_token=access_token,
            scopes=identity.get("scopes") or [],
            site_id=identity.get("site_id"),
            practice_name=label or identity.get("practice_name"),
        )
        return self.store.put(token)

    # -- keepalive ------------------------------------------------------------
    async def keepalive_once(self) -> dict[str, str]:
        """Ping every stored token so none dies of idleness. Returns id -> status."""
        results: dict[str, str] = {}
        for tok in self.store.all():
            client = DentallyClient(tok.access_token, transport=self._transport)
            try:
                await client.whoami()
                self.store.touch(tok.practice_id)
                results[tok.practice_id] = "ok"
            except AuthError as exc:
                # Do NOT delete it. A practice that must re-authorise needs to be told,
                # and silently dropping the record hides the outage from whoever can fix it.
                results[tok.practice_id] = f"reauth-required: {exc.message}"
                log.warning("Dentally token for practice %s needs re-auth", tok.practice_id)
            except Exception as exc:  # noqa: BLE001 - one bad token must not stop the sweep
                results[tok.practice_id] = f"error: {exc}"
                log.warning("Keepalive failed for practice %s: %s", tok.practice_id, exc)
            finally:
                await client.aclose()
        return results

    async def keepalive_loop(self) -> None:  # pragma: no cover - long-running
        interval = max(0.25, config.KEEPALIVE_INTERVAL_HOURS) * 3600
        while True:
            try:
                await self.keepalive_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("Keepalive sweep failed: %s", exc)
            await asyncio.sleep(interval)

    # -- internals ------------------------------------------------------------
    def _expire_stale(self) -> None:
        cutoff = time.time() - STATE_TTL_SECONDS
        for state, pending in list(self._pending.items()):
            if pending.created_at < cutoff:
                del self._pending[state]

    @property
    def pending_count(self) -> int:
        self._expire_stale()
        return len(self._pending)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:1000]}
