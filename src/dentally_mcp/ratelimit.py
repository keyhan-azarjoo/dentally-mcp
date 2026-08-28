"""Client-side rate limiting against Dentally's 3,600 requests/hour/user ceiling.

Why self-limit rather than just handle 429s: the budget is per *user*, and the
practice's other integrations (and Dentally's own web app) draw on the same pool.
An assistant that burns the hour with a runaway pagination loop takes the practice's
online booking down with it, so the pacing has to happen before the request, not
after the rejection.

Two mechanisms, deliberately:
  * a token bucket sized under the documented ceiling — bounds our own behaviour
    even when Dentally sends no headers (sandbox sometimes does not);
  * adoption of Dentally's own `X-RateLimit-Remaining` / `-Reset` headers — the
    server is the only thing that knows what the *rest* of the practice has spent.
"""
from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Async token bucket with an upstream-header override."""

    def __init__(self, per_hour: int, *, clock=time.monotonic):
        self.per_hour = max(1, per_hour)
        self.capacity = float(self.per_hour)
        self._tokens = float(self.per_hour)
        self._refill_per_sec = self.per_hour / 3600.0
        self._clock = clock
        self._last = clock()
        self._lock = asyncio.Lock()
        # Set from upstream headers; when the server says we have very little left we
        # stop pacing off our own estimate and wait for its reset instead.
        self._upstream_remaining: int | None = None
        self._upstream_reset_at: float | None = None

    def _replenish(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self._refill_per_sec)

    @property
    def tokens(self) -> float:
        return self._tokens

    def delay_for_next(self) -> float:
        """Seconds a caller would have to wait right now. Zero means go."""
        self._replenish()
        if self._upstream_remaining is not None and self._upstream_remaining <= 0:
            if self._upstream_reset_at:
                return max(0.0, self._upstream_reset_at - self._clock())
            return 60.0
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self._refill_per_sec

    async def acquire(self) -> None:
        """Block until one request may be sent, then spend it."""
        while True:
            async with self._lock:
                wait = self.delay_for_next()
                if wait <= 0:
                    self._tokens -= 1.0
                    if self._upstream_remaining is not None:
                        self._upstream_remaining -= 1
                    return
            # Sleep OUTSIDE the lock: holding it would serialise every waiter behind
            # the longest sleep instead of letting them re-check as the bucket fills.
            await asyncio.sleep(min(wait, 5.0))

    def observe(self, headers) -> None:
        """Adopt Dentally's own view of the budget from response headers."""
        remaining = _first_int(headers, "x-ratelimit-remaining", "ratelimit-remaining")
        if remaining is not None:
            self._upstream_remaining = remaining
            # Never let our optimistic local estimate outrun the server's count.
            self._tokens = min(self._tokens, float(remaining))

        reset = _first_int(headers, "x-ratelimit-reset", "ratelimit-reset")
        if reset is not None:
            # Some servers send epoch seconds, others a delta. Anything that looks like
            # a wall-clock epoch is treated as one; small values are a delta.
            now_wall = time.time()
            delta = reset - now_wall if reset > 1_000_000_000 else float(reset)
            self._upstream_reset_at = self._clock() + max(0.0, delta)


def _first_int(headers, *names: str) -> int | None:
    for name in names:
        raw = headers.get(name) if headers is not None else None
        if raw is None:
            continue
        try:
            return int(float(str(raw).strip()))
        except (TypeError, ValueError):
            continue
    return None
