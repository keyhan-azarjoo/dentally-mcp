"""The wrapper every tool goes through: scope, consent, audit, error translation.

Each tool body is then only the interesting part — the Dentally call and its
projection — and the four things that must never be skipped happen in one place
where they can be read and tested together.
"""
from __future__ import annotations

import functools
import logging
import typing
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from . import audit, auth, config, surfaces
from .auth import Resolver
from .client import DentallyClient
from .errors import DentallyError, WriteBlockedError
from .oauth import OAuthFlow
from .tokenstore import TokenStore

log = logging.getLogger("dentally_mcp.runtime")

STORE = TokenStore()
RESOLVER = Resolver(STORE)
FLOW = OAuthFlow(STORE)


@asynccontextmanager
async def use_client(practice_id: str | None = None):
    """Yield a Dentally client for the practice this request is acting for."""
    client: DentallyClient = RESOLVER.client(practice_id)
    try:
        yield client
    finally:
        await client.aclose()


async def confirm(ctx: Any, action: str, detail: str) -> None:
    """Ask the human before a write. Fails closed when the client cannot ask.

    A model can be talked into booking or cancelling by text it read in a patient
    note; a human confirming the specific action is the control that survives that.
    Consent is therefore refused — not skipped — when the client has no way to
    surface the question.
    """
    if not config.REQUIRE_CONSENT:
        return

    prompt = f"{action}\n\n{detail}\n\nProceed?"
    elicit = getattr(ctx, "elicit", None) if ctx is not None else None
    if elicit is None:
        if config.CONSENT_FALLBACK_ALLOW:
            log.warning("Client cannot elicit; proceeding because CONSENT_FALLBACK_ALLOW is set: %s", action)
            return
        raise WriteBlockedError(
            f"Refusing to {action.lower()} — this client cannot ask the user to confirm, "
            "and this server will not make changes in a dental record without a human "
            "confirming the specific action. Set DENTALLY_CONSENT_FALLBACK_ALLOW=1 only "
            "if the client itself already confirms writes."
        )

    try:
        result = await elicit(message=prompt, schema=ConfirmWrite)
    except Exception as exc:  # noqa: BLE001 - includes "client does not support elicitation"
        if config.CONSENT_FALLBACK_ALLOW:
            log.warning("Elicitation failed (%s); proceeding because CONSENT_FALLBACK_ALLOW is set", exc)
            return
        raise WriteBlockedError(
            f"Could not obtain confirmation for: {action} ({exc}). Nothing was changed."
        ) from exc

    action_taken = getattr(result, "action", None)
    data = getattr(result, "data", None)
    accepted = action_taken == "accept" and bool(getattr(data, "confirm", False))
    if not accepted:
        raise WriteBlockedError(f"Cancelled by the user: {action}. Nothing was changed.")


class ConfirmWrite:
    """Elicitation schema — a single explicit yes."""

    confirm: bool


def tool(name: str) -> Callable:
    """Decorate a tool implementation with the guard rails.

    Order matters: surface, then write gate, then the call, then audit. The write
    gate runs before any request is built so a blocked write never touches Dentally.
    """

    def decorator(fn: Callable[..., Awaitable[Any]]):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            practice = auth.current_practice.get()
            caller = auth.current_caller.get()
            role = auth.current_surface.get()

            if not surfaces.allows(role, name):
                audit.record(tool=name, practice_id=practice, caller=caller,
                             args=kwargs, outcome="denied", error="not in surface")
                raise DentallyError(
                    f"`{name}` is not available to the '{surfaces.normalise(role)}' role. "
                    f"Available here: {', '.join(sorted(surfaces.visible(role)))}"
                )

            if surfaces.is_write(name):
                auth.assert_writes_enabled(name.replace("_", " "))

            try:
                result = await fn(*args, **kwargs)
            except DentallyError as exc:
                audit.record(tool=name, practice_id=practice, caller=caller,
                             args=kwargs, outcome="error", error=exc.message)
                raise
            except Exception as exc:  # noqa: BLE001
                audit.record(tool=name, practice_id=practice, caller=caller,
                             args=kwargs, outcome="error", error=str(exc))
                raise

            audit.record(tool=name, practice_id=practice, caller=caller,
                         args=kwargs, outcome="ok", count=_count(result))
            return result

        # Resolve the wrapped function's annotations to real classes.
        #
        # This is load-bearing, not tidiness. The tool modules use
        # `from __future__ import annotations`, so their hints are strings, and
        # FastMCP resolves a tool's hints against the *wrapper's* `__globals__` —
        # which is this module, where `Context` is not defined. The lookup would
        # fail, FastMCP would decide the tool wants no Context, and `ctx` would
        # arrive as None: every write silently losing its confirmation prompt while
        # still looking wired up. Resolving here, in the tool module's own
        # namespace, is what keeps that from happening.
        try:
            wrapper.__annotations__ = typing.get_type_hints(fn)
        except Exception:  # noqa: BLE001 - a tool with unresolvable hints still works
            log.debug("Could not resolve annotations for %s", name)

        return wrapper

    return decorator


def _count(result: Any) -> int | None:
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        for key in ("results", "appointments", "patients", "items", "invoices", "payments"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
    return None
