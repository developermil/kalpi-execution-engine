import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

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


class FakeClock:
    """Monotonic fake: sleep(d) wakes at its own target time, so concurrent sleepers compose."""

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

    def advance(self, d: float) -> None:
        self.t += d


class Harness:
    """One Paper account + DB + fake clock; wall time follows the fake clock from T0."""

    def __init__(self, sm: async_sessionmaker[AsyncSession], faults: Faults | None = None) -> None:
        self.sm = sm
        self.broker = PaperBroker(faults)
        self.clock = FakeClock()
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
