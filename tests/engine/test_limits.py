"""Rate limiter + retry policy (D10, D9) with a fake clock: no real sleeping."""

import asyncio
import random

import pytest

from kalpi_engine.domain.errors import (
    AmbiguousSubmit,
    AuthExpired,
    BrokerRejected,
    InvalidOrder,
    KalpiError,
    RateLimited,
    TransientError,
)
from kalpi_engine.execution.limits import (
    LimiterRegistry,
    RateLimiter,
    RetryPolicy,
    call_with_retry,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        target = self.t + d
        await asyncio.sleep(0)
        self.t = max(self.t, target)


def max_in_window(ts: list[float], window: float = 1.0) -> int:
    ts = sorted(ts)
    j, best = 0, 0
    for i, t in enumerate(ts):
        while t - ts[j] >= window - 1e-9:
            j += 1
        best = max(best, i - j + 1)
    return best


# ---------- limiter ----------


async def test_10_per_s_never_exceeds_10_in_any_1s_window_sequential() -> None:
    clock = FakeClock()
    lim = RateLimiter(10, clock)
    ts = []
    for _ in range(200):
        await lim.acquire()
        ts.append(clock.now())
    assert max_in_window(ts) == 10
    assert ts[-1] - ts[0] == pytest.approx(19.0)  # and not slower than needed


async def test_10_per_s_never_exceeds_10_in_any_1s_window_concurrent() -> None:
    clock = FakeClock()
    lim = RateLimiter(10, clock)
    ts: list[float] = []

    async def one() -> None:
        await lim.acquire()
        ts.append(clock.now())

    await asyncio.gather(*(one() for _ in range(200)))
    assert len(ts) == 200 and max_in_window(ts) == 10


async def test_fractional_rate() -> None:
    clock = FakeClock()
    lim = RateLimiter(2.5, clock)
    ts = []
    for _ in range(50):
        await lim.acquire()
        ts.append(clock.now())
    assert max_in_window(ts) <= 3 and max_in_window(ts, 2.0) <= 5


def test_registry_keys_per_broker_session_and_kind() -> None:
    reg = LimiterRegistry(FakeClock())
    a = reg.get("zerodha", "s1", "orders", 10)
    assert reg.get("zerodha", "s1", "orders", 10) is a
    assert reg.get("zerodha", "s2", "orders", 10) is not a
    assert reg.get("upstox", "s1", "orders", 10) is not a
    assert reg.get("zerodha", "s1", "reads", 10) is not a


# ---------- retry ----------


class Script:
    """Raises the scripted errors in order, then returns 'ok'."""

    def __init__(self, *errors: KalpiError) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def policy(**kw: float) -> RetryPolicy:
    return RetryPolicy(**({"max_attempts": 4, "base_s": 0.5, "cap_s": 8.0} | kw))  # type: ignore[arg-type]


async def call(fn: Script, clock: FakeClock, **kw: float) -> str:
    return await call_with_retry(
        fn, limiter=RateLimiter(100, clock), clock=clock, policy=policy(**kw), rng=random.Random(1)
    )


async def test_retry_after_honoured() -> None:
    clock = FakeClock()
    fn = Script(RateLimited("429", retry_after=3.0))
    assert await call(fn, clock) == "ok"
    assert fn.calls == 2 and 3.0 in clock.sleeps


async def test_retry_after_capped() -> None:
    clock = FakeClock()
    fn = Script(RateLimited("429", retry_after=600.0))
    await call(fn, clock)
    assert max(clock.sleeps) == 8.0


async def test_429_without_retry_after_uses_jittered_backoff() -> None:
    clock = FakeClock()
    fn = Script(RateLimited("429"), RateLimited("429"), TransientError("conn refused"))
    assert await call(fn, clock) == "ok"
    backoffs = [s for s in clock.sleeps if s > 0.011]  # ignore limiter spacing
    assert fn.calls == 4 and len(backoffs) == 3
    for n, s in enumerate(backoffs):
        assert 0 <= s <= 0.5 * 2**n


async def test_retry_safe_gives_up_after_max_attempts() -> None:
    clock = FakeClock()
    fn = Script(*(TransientError("x") for _ in range(10)))
    with pytest.raises(TransientError):
        await call(fn, clock, max_attempts=3)
    assert fn.calls == 3


@pytest.mark.parametrize(
    "err",
    [
        AmbiguousSubmit("timeout after send"),
        BrokerRejected("RMS"),
        InvalidOrder("bad"),
        AuthExpired("401"),
    ],
)
async def test_ambiguous_rejected_and_auth_never_retried(err: KalpiError) -> None:
    clock = FakeClock()
    fn = Script(err)
    with pytest.raises(type(err)):
        await call(fn, clock)
    assert fn.calls == 1


async def test_every_attempt_passes_through_limiter() -> None:
    clock = FakeClock()
    lim = RateLimiter(1, clock)
    fn = Script(TransientError("x"))
    await call_with_retry(fn, limiter=lim, clock=clock, policy=policy(base_s=0.0))
    assert fn.calls == 2 and clock.now() - 1000.0 >= 1.0


async def test_bounded_concurrency() -> None:
    clock = FakeClock()
    sem = asyncio.Semaphore(3)
    inflight = peak = 0

    async def fn() -> str:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0)
        inflight -= 1
        return "ok"

    await asyncio.gather(
        *(
            call_with_retry(fn, limiter=RateLimiter(1000, clock), clock=clock, semaphore=sem)
            for _ in range(20)
        )
    )
    assert peak == 3
