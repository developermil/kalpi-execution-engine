"""Resume sweep (SPEC §5 step 9, D30): at startup and every LEASE_TTL_S, claim non-terminal
runs whose lease is NULL or expired and resume them. Only the lease decides who executes.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.domain.enums import RunStatus
from kalpi_engine.execution.executor import Executor
from kalpi_engine.execution.limits import Clock
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Run

log = logging.getLogger(__name__)

ExecutorFactory = Callable[[Run], Awaitable[Executor | None]]


class ResumeSweeper:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        factory: ExecutorFactory,
        *,
        clock: Clock,
        wall: Callable[[], datetime],
        interval_s: float = 30,
    ) -> None:
        self.sm, self.factory, self.clock, self.wall = sm, factory, clock, wall
        self.interval_s = interval_s
        self._tasks: set[asyncio.Task[RunStatus | None]] = set()

    async def sweep_once(self) -> list[asyncio.Task[RunStatus | None]]:
        async with self.sm() as s:
            runs = await repo.resumable_runs(s, self.wall())
        started = []
        for run in runs:
            ex = await self.factory(run)  # None: e.g. broker session gone; left for a human
            if ex is None:
                continue
            log.info("resuming run %s", run.id)
            task = asyncio.create_task(ex.run(run.id), name=f"resume-{run.id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            started.append(task)
        return started

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.sweep_once()
            except Exception:
                log.exception("resume sweep failed")
            sleeper = asyncio.create_task(self.clock.sleep(self.interval_s))
            stopper = asyncio.create_task(stop.wait())
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
            for t in (sleeper, stopper):
                t.cancel()
        for task in list(self._tasks):  # shutdown: the lease expires and a later sweep resumes
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
