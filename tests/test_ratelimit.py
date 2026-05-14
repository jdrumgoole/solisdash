from __future__ import annotations

import asyncio

import pytest

from solisdash.ratelimit import TokenBucket


def test_token_bucket_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0)
    with pytest.raises(ValueError):
        TokenBucket(rate=-1)


async def test_token_bucket_acquire_consumes_token_when_available() -> None:
    bucket = TokenBucket(rate=10, capacity=2)
    await bucket.acquire()
    assert bucket.tokens == pytest.approx(1.0, abs=0.1)


async def test_token_bucket_blocks_until_refill() -> None:
    """Use a fake clock + fake sleep so the test is deterministic."""
    clock = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    bucket = TokenBucket(
        rate=2.0,
        capacity=1.0,
        now=lambda: clock[0],
        sleep=fake_sleep,
    )
    # First acquire is free (capacity = 1).
    await bucket.acquire()
    assert sleeps == []

    # Second acquire requires waiting 0.5s for one more token at rate 2/sec.
    await bucket.acquire()
    assert pytest.approx(0.5, abs=1e-6) == sum(sleeps)


async def test_token_bucket_rejects_request_larger_than_capacity() -> None:
    bucket = TokenBucket(rate=1, capacity=1)
    with pytest.raises(ValueError, match="capacity"):
        await bucket.acquire(tokens=5)


async def test_token_bucket_serialises_concurrent_acquires() -> None:
    """Two acquirers must each take a token; the second one waits."""
    clock = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    bucket = TokenBucket(
        rate=1.0,
        capacity=1.0,
        now=lambda: clock[0],
        sleep=fake_sleep,
    )

    async def grab() -> None:
        await bucket.acquire()

    await asyncio.gather(grab(), grab())
    # One acquire was immediate; the other waited ~1s for a refill.
    assert pytest.approx(1.0, abs=1e-6) == sum(sleeps)
