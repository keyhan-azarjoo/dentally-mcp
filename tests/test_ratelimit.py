"""Pacing against the 3,600/hour budget the practice shares with its other tools."""
from __future__ import annotations

from dentally_mcp.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


async def test_a_full_bucket_lets_a_burst_through():
    limiter = RateLimiter(3600, clock=FakeClock())
    for _ in range(50):
        await limiter.acquire()
    assert limiter.tokens <= 3550


def test_an_empty_bucket_reports_a_wait():
    clock = FakeClock()
    limiter = RateLimiter(3600, clock=clock)
    limiter._tokens = 0.0
    # 3600/hour is one per second, so an empty bucket is a one-second wait.
    assert 0.9 < limiter.delay_for_next() < 1.1


def test_the_bucket_refills_with_time():
    clock = FakeClock()
    limiter = RateLimiter(3600, clock=clock)
    limiter._tokens = 0.0
    clock.advance(10)
    assert limiter.delay_for_next() == 0.0
    assert limiter.tokens >= 9


def test_upstream_headers_override_our_optimistic_estimate():
    """The practice's other integrations spend from the same pool, so Dentally's
    count is authoritative and must be able to lower ours, never raise it."""
    limiter = RateLimiter(3600, clock=FakeClock())
    limiter.observe({"x-ratelimit-remaining": "5"})
    assert limiter.tokens == 5


def test_exhausted_upstream_budget_waits_for_the_reset():
    clock = FakeClock()
    limiter = RateLimiter(3600, clock=clock)
    limiter.observe({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "30"})
    assert 29 <= limiter.delay_for_next() <= 31


def test_missing_headers_are_ignored_rather_than_crashing():
    """The sandbox does not always send them; that must not break the client."""
    limiter = RateLimiter(3600, clock=FakeClock())
    limiter.observe({})
    limiter.observe({"x-ratelimit-remaining": "not-a-number"})
    assert limiter.delay_for_next() == 0.0
