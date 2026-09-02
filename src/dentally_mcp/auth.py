"""Who is calling, which practice they may act for, and whether a write is allowed.

The MCP server runs in two very different shapes and this module is what makes them
share one code path:

* **stdio, single practice** — a clinician runs the server on their own machine with
  `DENTALLY_API_TOKEN` set. There is exactly one credential and no caller identity to
  establish beyond "whoever launched the process".

* **HTTP, multi practice** — the server is hosted, and MyOTGO / OpenAI / Claude
  connect over Streamable HTTP. Now the caller must prove itself (a bearer token we
  issued, not Dentally's) and name the practice it is acting for.

The important invariant: the *Dentally* credential is never accepted from the caller
over HTTP. A client that could pass its own token could reach any practice that ever
authorised us. Callers name a practice; the server resolves the credential.
"""
from __future__ import annotations

import hmac
from contextvars import ContextVar
from dataclasses import dataclass

from . import config
from .client import DentallyClient
from .errors import AuthError, WriteBlockedError
from .tokenstore import TokenStore

# Per-request context. FastMCP runs tools concurrently on one loop, so this cannot be
# module-level mutable state — that would leak one practice's context into another's
# tool call under any concurrency at all.
current_practice: ContextVar[str | None] = ContextVar("current_practice", default=None)
current_surface: ContextVar[str] = ContextVar("current_surface", default=config.DEFAULT_SURFACE)
current_caller: ContextVar[str] = ContextVar("current_caller", default="stdio")


@dataclass
class Credential:
    practice_id: str
    access_token: str
    scopes: list[str]
    practice_name: str | None = None


class Resolver:
    """Turns 'the current request' into a usable Dentally credential."""

    def __init__(self, store: TokenStore | None = None):
        self.store = store or TokenStore()

    def resolve(self, practice_id: str | None = None) -> Credential:
        practice_id = practice_id or current_practice.get()

        if config.DEMO:
            return Credential("demo", "demo-token",
                              ["user:read", "patient:read", "appointment:read"],
                              "DEMO PRACTICE — synthetic data, not a real practice")

        # Single-practice mode: the env token wins and no practice needs naming.
        if config.API_TOKEN and not practice_id:
            return Credential(
                practice_id="env",
                access_token=config.API_TOKEN,
                scopes=[],
                practice_name="configured practice",
            )

        if not practice_id:
            known = self._known_ids()
            if len(known) == 1:
                practice_id = known[0]
            elif not known:
                raise AuthError(
                    "No Dentally credential is available. Either set DENTALLY_API_TOKEN "
                    "for a single practice, or complete the OAuth login at /auth/login. "
                    "See docs/LOGIN.md."
                )
            else:
                raise AuthError(
                    f"Several practices are authorised ({', '.join(known)}); the caller "
                    "must say which one via the X-Dentally-Practice header."
                )

        token = self.store.get(practice_id) if self._store_ready() else None
        if token is None:
            if config.API_TOKEN:
                return Credential("env", config.API_TOKEN, [], "configured practice")
            raise AuthError(f"Practice {practice_id!r} has not authorised this integration.")

        return Credential(
            practice_id=token.practice_id,
            access_token=token.access_token,
            scopes=list(token.scopes),
            practice_name=token.practice_name,
        )

    def client(self, practice_id: str | None = None) -> DentallyClient:
        if config.DEMO:
            # Swap only the transport. Everything above it — pagination, the rate
            # limiter, redaction, the projections — is the code that ships, so a demo
            # exercises the real thing rather than a parallel implementation.
            from .demo import transport

            client = DentallyClient("demo-token", transport=transport())
            client.scopes = ["user:read", "patient:read", "appointment:read"]
            return client

        cred = self.resolve(practice_id)
        client = DentallyClient(cred.access_token)
        client.scopes = list(cred.scopes)
        return client

    def _store_ready(self) -> bool:
        return bool(config.TOKEN_STORE_KEY)

    def _known_ids(self) -> list[str]:
        if not self._store_ready():
            return []
        try:
            return self.store.list_ids()
        except Exception:  # noqa: BLE001 - an unreadable store is "no practices", not a crash
            return []


def verify_client(authorization: str | None) -> bool:
    """Check the bearer token OUR clients present on the HTTP transport.

    Compared with `hmac.compare_digest` rather than `==` so the check does not leak
    the token a character at a time through response timing.
    """
    if not config.CLIENT_AUTH_TOKEN:
        # No token configured. Allowed only for a loopback bind — enforced in server.py,
        # because "convenient locally" must not silently become "open on the internet".
        return True
    if not authorization:
        return False
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return hmac.compare_digest(value.strip(), config.CLIENT_AUTH_TOKEN)


def assert_writes_enabled(action: str) -> None:
    """Gate every mutating tool. Called before the request is built, not after."""
    if not config.ALLOW_WRITES:
        raise WriteBlockedError(
            f"Refusing to {action}: this server is running read-only. "
            "Set DENTALLY_ALLOW_WRITES=1 to permit writes, and read docs/SECURITY.md "
            "first — a booking or deletion made from a prompt-injected instruction is "
            "not recoverable from the model's side."
        )
