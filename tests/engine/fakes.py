import asyncio
import functools
import heapq
import weakref
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.base import BrokerSession
from kalpi_engine.brokers.paper import Faults, PaperBroker
from kalpi_engine.domain.schemas import ExecuteRequest
from kalpi_engine.execution.executor import ExecConfig, Executor
from kalpi_engine.execution.limits import LimiterRegistry, RetryPolicy
from kalpi_engine.execution.reconcile import Reconciler
from kalpi_engine.planner import plan
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Event, Leg, Outbox, Run

# Wednesday 2026-09-30 11:00 IST: inside market hours, well before the 15:35 recheck cutoff.
T0 = datetime(2026, 9, 30, 5, 30, tzinfo=UTC)


_WATCHING: "weakref.WeakSet[FakeClock]" = weakref.WeakSet()


def _install_io_counter() -> None:
    """Count every SQLite call handed to aiosqlite's worker thread as busy, for all clocks."""
    conn_cls = aiosqlite.core.Connection
    if getattr(conn_cls, "_fake_clock_patched", False):
        return

    def counted(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            clocks = list(_WATCHING)
            for c in clocks:
                c.busy += 1
            try:
                return await fn(*args, **kwargs)
            finally:
                for c in clocks:
                    c.busy -= 1

        return wrapper

    conn_cls._execute = counted(conn_cls._execute)  # type: ignore[method-assign]
    conn_cls._connect = counted(conn_cls._connect)  # type: ignore[method-assign]
    conn_cls._fake_clock_patched = True  # type: ignore[attr-defined]


class FakeClock:
    """Virtual time. sleep(d) parks until time reaches now+d. A ticker advances time to the
    earliest wake-up only when the system is quiescent: no SQLite call in flight (watch())
    and nothing runnable after a few loop turns. Real I/O therefore costs no fake time and
    concurrent sleepers (pollers, the lease renewer) wake in timestamp order.
    """

    def __init__(self, tick_real_s: float = 0.001) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []
        self._tick_real_s = tick_real_s
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = 0
        self._ticker: asyncio.Task[None] | None = None
        self.busy = 0  # SQLite calls in flight

    def watch(self, sm: async_sessionmaker[AsyncSession]) -> None:
        _install_io_counter()
        _WATCHING.add(self)

    def now(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        if d <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._waiters, (self.t + d, self._seq, fut))
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.create_task(self._tick())
        await fut

    def advance(self, d: float) -> None:
        """Manual jump (between runs); wakes anything now due."""
        self.t += d
        self._wake_due()

    def _wake_due(self) -> None:
        while self._waiters and (self._waiters[0][2].done() or self._waiters[0][0] <= self.t):
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)

    async def _quiescent(self) -> bool:
        if self.busy:
            return False
        for _ in range(20):  # let every runnable task reach its next blocking await
            await asyncio.sleep(0)
            if self.busy:
                return False
        return True

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self._tick_real_s)
            if not await self._quiescent():
                continue
            while self._waiters and self._waiters[0][2].done():  # cancelled sleepers
                heapq.heappop(self._waiters)
            if not self._waiters:
                return
            self.t = max(self.t, self._waiters[0][0])
            self._wake_due()


class Harness:
    """One Paper account + DB + fake clock; wall time follows the fake clock from T0."""

    def __init__(self, sm: async_sessionmaker[AsyncSession], faults: Faults | None = None) -> None:
        self.sm = sm
        self.broker = PaperBroker(faults)
        self.clock = FakeClock()
        self.clock.watch(sm)
        self.limiters = LimiterRegistry(self.clock)
        self.session: BrokerSession | None = None

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.clock.now() - 1000.0)

    async def start(
        self,
        mode: str,
        instructions: list[dict[str, Any]],
        *,
        holdings: dict[str, int] | None = None,
        **options: Any,
    ) -> str:
        self.session = await self.broker.create_session({"holdings": holdings or {}})
        body = ExecuteRequest.model_validate(
            {
                "session_id": str(uuid4()),
                "mode": mode,
                "options": {"poll_timeout_s": 5} | options,
                "instructions": instructions,
            }
        )
        run_id = str(uuid4())
        await repo.create_run(
            self.sm,
            run_id=run_id,
            user_id="u1",
            idempotency_key=run_id,
            request_hash="h",
            session_id=str(body.session_id),
            mode=body.mode,
            options=body.options.model_dump(mode="json"),
            legs=plan(body, run_id),
            now=self.wall(),
        )
        return run_id

    def executor(self, **cfg: Any) -> Executor:
        assert self.session is not None
        config = ExecConfig(**({"retry": RetryPolicy(max_attempts=4)} | cfg))
        return Executor(
            self.sm,
            self.broker,
            self.session,
            self.limiters,
            clock=self.clock,
            config=config,
            wall=self.wall,
        )

    def reconciler(self) -> Reconciler:
        assert self.session is not None
        return Reconciler(
            self.sm, self.broker, self.session, self.limiters, clock=self.clock, wall=self.wall
        )

    async def state(self, run_id: str) -> tuple[Run, list[Leg], list[Event], list[Outbox]]:
        async with self.sm() as s:
            run = await repo.get_run(s, run_id)
            assert run is not None
            return (
                run,
                await repo.get_legs(s, run_id),
                await repo.get_events(s, run_id),
                await repo.get_outbox(s, run_id),
            )
