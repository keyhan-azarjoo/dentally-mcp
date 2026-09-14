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
# Which practices the CALLER may act for. `{"*"}` means unrestricted, which is the
# default and what a single-practice deployment always gets.
current_allowed: ContextVar[frozenset[str]] = ContextVar("current_allowed", default=frozenset({"*"}))


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
        self._assert_caller_may_reach(practice_id)

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

    def _assert_caller_may_reach(self, practice_id: str | None) -> None:
        """Stop a caller naming a practice its token does not cover.

        The practice comes from a request header, so without this the server token
        alone decides everything and one customer's client can read another's
        records. Enforced here, at the point the credential is chosen, so no tool can
        route around it.
        """
        allowed = current_allowed.get()
        if "*" in allowed:
            return
        if practice_id is None:
            # An unscoped caller must say which practice it means; defaulting to
            # "the only one connected" would quietly ignore the restriction.
            raise AuthError(
                "This client is scoped to specific practices and must name one "
                "via the X-Dentally-Practice header."
            )
        if practice_id not in allowed:
            raise AuthError(f"This client is not authorised for practice {practice_id!r}.")

    def _store_ready(self) -> bool:
        return bool(config.TOKEN_STORE_KEY)

    def _known_ids(self) -> list[str]:
        if not self._store_ready():
            return []
        try:
            return self.store.list_ids()
        except Exception:  # noqa: BLE001 - an unreadable store is "no practices", not a crash
            return []


def allowed_practices(authorization: str | None) -> frozenset[str] | None:
    """Practices this caller may reach, or None if the credential is not valid.

    `{"*"}` means unrestricted. Checked with `compare_digest` against every
    configured token so the comparison does not leak which one matched via timing.
    """
    presented = _bearer(authorization)

    if config.CLIENT_TOKENS:
        match: frozenset[str] | None = None
        for token, practices in config.CLIENT_TOKENS.items():
            # Compare them ALL, even after a hit: returning early would make the
            # response time depend on the token's position in the mapping.
            if presented and hmac.compare_digest(presented, token):
                match = practices
        if match is not None:
            return match

    if config.CLIENT_AUTH_TOKEN:
        if presented and hmac.compare_digest(presented, config.CLIENT_AUTH_TOKEN):
            return frozenset({"*"})
        return None

    # No token configured at all: open, and only safe because server.py refuses a
    # non-loopback bind in that state.
    return None if config.CLIENT_TOKENS else frozenset({"*"})


def _bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def verify_client(authorization: str | None) -> bool:
    """Check the bearer token OUR clients present on the HTTP transport.

    Compared with `hmac.compare_digest` rather than `==` so the check does not leak
    the token a character at a time through response timing.
    """
    return allowed_practices(authorization) is not None


def assert_writes_enabled(action: str) -> None:
    """Gate every mutating tool. Called before the request is built, not after."""
    if not config.ALLOW_WRITES:
        raise WriteBlockedError(
            f"Refusing to {action}: this server is running read-only. "
            "Set DENTALLY_ALLOW_WRITES=1 to permit writes, and read docs/SECURITY.md "
            "first — a booking or deletion made from a prompt-injected instruction is "
            "not recoverable from the model's side."
        )
