"""Rate limiting, bounded concurrency and the retry policy (D9, D10).

Only RETRY_SAFE errors (RateLimited, TransientError: the broker provably did not accept the
request) are retried. AmbiguousSubmit, rejections and AuthExpired propagate on the first raise:
an ambiguous order is reconciled by tag by the executor, never resent.
"""

import asyncio
import logging
import math
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from kalpi_engine.domain.errors import RateLimited, TransientError

log = logging.getLogger(__name__)

RETRY_SAFE: tuple[type[Exception], ...] = (RateLimited, TransientError)


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class RateLimiter:
    """Sliding-window limiter: at most `n` acquisitions in any window of `window_s`.

    A classic token bucket of capacity r admits up to 2r calls across a 1s boundary (full
    bucket + refill), which would breach a broker's hard per-second cap; the window log does not.
    Fractional rates round down (2.5/s -> 2/s); rates below 1/s become 1 call per 1/rate s.
    """

    def __init__(self, per_second: float, clock: Clock) -> None:
        if per_second <= 0:
            raise ValueError("per_second must be > 0")
        if per_second >= 1:
            self.n, self.window_s = math.floor(per_second), 1.0
        else:
            self.n, self.window_s = 1, 1.0 / per_second
        self._clock = clock
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:  # FIFO among waiters; one sleeper at a time
            while True:
                now = self._clock.now()
                while self._stamps and now - self._stamps[0] >= self.window_s:
                    self._stamps.popleft()
                if len(self._stamps) < self.n:
                    self._stamps.append(now)
                    return
                await self._clock.sleep(self._stamps[0] + self.window_s - now)


class LimiterRegistry:
    """One limiter per (broker, session, kind); kind is 'orders' or 'reads'."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or MonotonicClock()
        self._limiters: dict[tuple[str, str, str], RateLimiter] = {}

    def get(self, broker: str, session_id: str, kind: str, per_second: float) -> RateLimiter:
        key = (broker, session_id, kind)
        if key not in self._limiters:
            self._limiters[key] = RateLimiter(per_second, self.clock)
        return self._limiters[key]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_s: float = 0.5
    cap_s: float = 8.0

    def delay(self, attempt: int, err: Exception, rng: random.Random) -> float:
        """Retry-After when the broker sent one, else full-jitter exponential backoff."""
        if isinstance(err, RateLimited) and err.retry_after is not None:
            return min(max(err.retry_after, 0.0), self.cap_s)
        return rng.uniform(0, min(self.cap_s, self.base_s * 2**attempt))


async def call_with_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    limiter: RateLimiter,
    clock: Clock,
    policy: RetryPolicy | None = None,
    semaphore: asyncio.Semaphore | None = None,
    rng: random.Random | None = None,
) -> T:
    """Each attempt takes a concurrency slot and a rate token; backoff sleeps hold neither."""
    policy = policy or RetryPolicy()
    rng = rng or random.Random()
    attempt = 0
    while True:
        try:
            if semaphore is not None:
                async with semaphore:
                    await limiter.acquire()
                    return await fn()
            await limiter.acquire()
            return await fn()
        except RETRY_SAFE as err:
            attempt += 1
            if attempt >= policy.max_attempts:
                raise
            wait = policy.delay(attempt - 1, err, rng)
            log.warning("retry-safe %s; attempt %d in %.2fs", type(err).__name__, attempt + 1, wait)
            await clock.sleep(wait)
