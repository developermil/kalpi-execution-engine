"""Leg state transitions: CAS on the expected status + an audit event in one transaction.

Every write carries the lease fence (D30): after a takeover it affects 0 rows.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.domain.enums import LegStatus
from kalpi_engine.domain.models import BrokerOrderState
from kalpi_engine.execution.reconcile import next_recheck
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Leg
from kalpi_engine.storage.repo import Fence

log = logging.getLogger(__name__)


class LegWriter:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        wall: Callable[[], datetime],
        fence: Callable[[], Fence],
    ) -> None:
        self.sm, self.wall, self.fence = sm, wall, fence

    async def move(
        self, leg: Leg, expected: LegStatus, new: LegStatus, event: str, **values: Any
    ) -> bool:
        payload = {"status": new.value} | {
            k: v for k, v in values.items() if k in ("broker_order_id", "filled_qty", "reason")
        }
        async with self.sm.begin() as s:
            ok = await repo.cas_leg(s, leg.id, expected, self.fence(), status=new, **values)
            if ok:
                await repo.append_event(s, leg.run_id, event, payload, self.wall(), leg_id=leg.id)
        if not ok:
            log.warning("leg %s CAS %s->%s lost", leg.id, expected, new)
        return ok

    async def unknown(
        self, leg: Leg, expected: LegStatus, reason: str, *, recheck: bool = True
    ) -> None:
        await self.move(
            leg,
            expected,
            LegStatus.UNKNOWN,
            "leg.unknown",
            reason=reason,
            recheck_at=next_recheck(self.wall()) if recheck else None,
        )

    async def set_recheck(self, leg: Leg, current: LegStatus, at: datetime | None) -> None:
        async with self.sm.begin() as s:
            await repo.cas_leg(s, leg.id, current, self.fence(), recheck_at=at)

    async def skip(self, leg: Leg, reason: str) -> None:
        await self.move(leg, LegStatus.PLANNED, LegStatus.SKIPPED, "leg.skipped", reason=reason)


def fill_values(state: BrokerOrderState) -> dict[str, Any]:
    values: dict[str, Any] = {"filled_qty": state.filled_qty, "avg_price": state.avg_price}
    if state.message:
        values["reason"] = state.message
    return values


def reason_of(e: BaseException) -> str:
    code = getattr(e, "code", type(e).__name__)
    return f"{code}: {e}"[:500]
