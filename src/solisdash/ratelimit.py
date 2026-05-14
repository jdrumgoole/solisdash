"""Async token-bucket rate limiter.

SolisCloud documents a 2 req/sec per-endpoint cap. The poller paces itself
through one of these so that the live-tile path and the scheduled jobs
share a single budget against the shared key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic


class TokenBucket:
    """Per-process token bucket.

    `rate` tokens are added per second up to `capacity`. `acquire` waits
    until enough tokens are available, then consumes them. A single
    `asyncio.Lock` serialises awaiters so we don't accidentally over-issue
    when several callers race for the last token.
    """

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        now: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self._rate = rate
        self._capacity = float(capacity) if capacity is not None else float(rate)
        self._tokens = self._capacity
        self._last = now()
        self._lock = asyncio.Lock()
        self._now = now
        self._sleep = sleep

    @property
    def tokens(self) -> float:
        """Current token count without acquiring. Useful for tests."""
        return self._tokens

    def _refill(self) -> None:
        now = self._now()
        elapsed = now - self._last
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last = now

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens > self._capacity:
            raise ValueError(
                f"requested {tokens} tokens but capacity is {self._capacity}"
            )
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                await self._sleep(deficit / self._rate)
