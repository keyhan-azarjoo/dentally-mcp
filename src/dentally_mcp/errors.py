"""Error types, and the mapping from a Dentally HTTP response to a useful message.

Dentally's status codes do not always mean what they usually mean — a missing
User-Agent is a 403, not a 400 — so the translation lives in one place and every
message says what to actually do about it.
"""
from __future__ import annotations


class DentallyError(Exception):
    """Base for anything this server raises at a caller."""

    def __init__(self, message: str, *, status: int | None = None, detail: object = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


class AuthError(DentallyError):
    """The Dentally credential is missing, invalid, expired, or under-scoped."""


class RateLimitError(DentallyError):
    """We would exceed (or Dentally reported) the 3,600/hour ceiling."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class NotFoundError(DentallyError):
    """The requested record does not exist, or is not visible to this token."""


class ValidationError(DentallyError):
    """Dentally rejected the payload (422) — the detail carries the field errors."""


class WriteBlockedError(DentallyError):
    """A write was attempted while writes are disabled or consent was refused."""


class UpstreamError(DentallyError):
    """Dentally returned 5xx or the network failed after retries."""


def from_response(status: int, body: object, *, sent_user_agent: bool = True) -> DentallyError:
    """Translate a non-2xx Dentally response into the most actionable exception."""
    detail = body

    if status == 401:
        return AuthError(
            "Dentally rejected the credential (401). The token is invalid or has expired. "
            "Dentally tokens have no refresh flow — an unused token dies, so re-authorise "
            "the practice (see docs/LOGIN.md) and keep DENTALLY_KEEPALIVE on.",
            status=status, detail=detail,
        )

    if status == 403:
        if not sent_user_agent:
            return AuthError(
                "Dentally returned 403 because the request had no User-Agent header. "
                "This is a header problem, not a permissions problem.",
                status=status, detail=detail,
            )
        return AuthError(
            "Dentally returned 403 — the token is valid but lacks the scope for this call. "
            "Check the X-OAuth-Scopes header on any response and re-authorise with the "
            "scope you need (e.g. patient:read, patient:update).",
            status=status, detail=detail,
        )

    if status == 404:
        return NotFoundError("Not found in Dentally (404).", status=status, detail=detail)

    if status == 422:
        return ValidationError("Dentally rejected the request body (422).", status=status, detail=detail)

    if status == 429:
        return RateLimitError(
            "Dentally rate limit hit (429). The ceiling is 3,600 requests/hour/user.",
            status=status, detail=detail,
        )

    if status >= 500:
        return UpstreamError(f"Dentally returned {status}.", status=status, detail=detail)

    return DentallyError(f"Dentally returned {status}.", status=status, detail=detail)
