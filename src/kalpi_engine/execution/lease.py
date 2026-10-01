"""Run lease (D20) with fencing (D30): claim, renew from a separate task, detect loss.

Every execution-time write carries `fence()`; once the lease is lost (renew failed, or another
worker took over) those writes affect 0 rows and the executor stops placing orders.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.execution.limits import Clock
from kalpi_engine.storage import repo
from kalpi_engine.storage.repo import Fence

log = logging.getLogger(__name__)


class Lease:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        run_id: str,
        owner: str,
        ttl_s: int,
        *,
        clock: Clock,
        wall: Callable[[], datetime],
    ) -> None:
        self.sm, self.run_id, self.owner, self.ttl_s = sm, run_id, owner, ttl_s
        self.clock, self.wall = clock, wall
        self.lost = False
        self.renewer: asyncio.Task[None] | None = None

    def fence(self) -> Fence:
        return Fence(self.run_id, self.owner, self.wall())

    async def claim(self) -> bool:
        async with self.sm.begin() as s:
            return await repo.claim_lease(s, self.run_id, self.owner, self.wall(), self.ttl_s)

    def start_renewing(self) -> None:
        self.renewer = asyncio.create_task(self._renew_loop(), name=f"lease-renew-{self.run_id}")

    async def _renew_loop(self) -> None:
        while True:
            await self.clock.sleep(self.ttl_s / 3)
            try:
                async with self.sm.begin() as s:
                    ok = await repo.renew_lease(s, self.run_id, self.owner, self.wall(), self.ttl_s)
            except Exception:
                log.exception("lease renew errored run=%s", self.run_id)
                ok = False
            if not ok:
                self.lost = True
                log.error("lease lost run=%s owner=%s: stop placing", self.run_id, self.owner)
                return

    async def held(self) -> bool:
        """False once lost; also detects a takeover the renewer has not noticed yet."""
        if not self.lost:
            async with self.sm() as s:
                self.lost = not await repo.lease_held(s, self.fence())
        return not self.lost

    async def stop(self) -> None:
        if self.renewer is not None:
            self.renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.renewer
