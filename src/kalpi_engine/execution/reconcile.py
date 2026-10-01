"""Late reconciliation (SPEC §5 steps 5 and 8; D9, D29, D31).

Adoption is by tag only (D29), never by symbol/side/qty heuristics. Rechecks never place
orders. Every leg write is a CAS on the status we saw, so a racing sweep and finalise
produce exactly one update.
"""

import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.domain.enums import TERMINAL_RUN_STATUSES, LegStatus, OrderStatus
from kalpi_engine.domain.errors import AuthExpired
from kalpi_engine.domain.models import BrokerOrderState
from kalpi_engine.domain.status import run_status
from kalpi_engine.execution.limits import Clock, LimiterRegistry, RateLimiter, call_with_retry
from kalpi_engine.notify.payload import build_payload
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Leg

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
RECHECK_EVERY = timedelta(seconds=60)
RECHECK_CUTOFF = time(15, 35)
TAG_LOOKUP_TRIES = 3
RECHECKABLE = (LegStatus.UNKNOWN, LegStatus.OPEN, LegStatus.PARTIAL)
_TERMINAL_ORDER = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}


def next_recheck(now: datetime) -> datetime | None:
    """now + 60s, unless that is past 15:35 IST on the same day (then stop: NULL)."""
    candidate = now + RECHECK_EVERY
    ist = now.astimezone(IST)
    cutoff = datetime.combine(ist.date(), RECHECK_CUTOFF, tzinfo=IST)
    return None if candidate > cutoff else candidate


class Reconciler:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        broker: BrokerAdapter,
        session: BrokerSession,
        limiters: LimiterRegistry,
        *,
        clock: Clock,
        wall: Callable[[], datetime],
        default_webhook_url: str | None = None,
    ) -> None:
        self.sm, self.broker, self.session = sm, broker, session
        self.clock, self.wall = clock, wall
        self.default_webhook_url = default_webhook_url
        self._reads: RateLimiter = limiters.get(
            broker.meta.id, "reconcile", "reads", broker.meta.rate_limits.reads_per_sec
        )

    async def find_by_tag(self, tag: str) -> BrokerOrderState | None:
        """Up to 3 lookups with 1s, 2s pauses. AuthExpired propagates."""
        for attempt in range(TAG_LOOKUP_TRIES):
            state = await call_with_retry(
                lambda: self.broker.find_order_by_tag(self.session, tag),
                limiter=self._reads,
                clock=self.clock,
            )
            if state is not None:
                return state
            if attempt < TAG_LOOKUP_TRIES - 1:
                await self.clock.sleep(2**attempt)
        return None

    async def sweep(self, run_id: str) -> int:
        """Recheck this run's due legs; returns how many changed."""
        async with self.sm() as s:
            due = await repo.due_rechecks(s, run_id, self.wall(), RECHECKABLE)
        changed = 0
        for leg in due:
            changed += await self.recheck_leg(leg)
        return changed

    async def recheck_leg(self, leg: Leg) -> bool:
        """One lookup; CAS WHERE status = what we saw. True if this call changed the leg."""
        seen = leg.status
        try:
            if leg.broker_order_id:
                state: BrokerOrderState | None = await call_with_retry(
                    lambda: self.broker.get_order(self.session, leg.broker_order_id or ""),
                    limiter=self._reads,
                    clock=self.clock,
                )
            else:
                state = await call_with_retry(
                    lambda: self.broker.find_order_by_tag(self.session, leg.tag),
                    limiter=self._reads,
                    clock=self.clock,
                )
        except AuthExpired:
            async with self.sm.begin() as s:
                await repo.cas_leg(s, leg.id, seen, None, recheck_at=None)
            return False
        except Exception:
            log.exception("recheck lookup failed for leg %s", leg.id)
            state = None

        new = LegStatus(state.status.value) if state else seen
        if state is None or (new is seen and state.filled_qty == leg.filled_qty):
            async with self.sm.begin() as s:
                await repo.cas_leg(s, leg.id, seen, None, recheck_at=next_recheck(self.wall()))
            return False

        terminal = state.status in _TERMINAL_ORDER
        async with self.sm.begin() as s:
            ok = await repo.cas_leg(
                s,
                leg.id,
                seen,
                None,
                status=new,
                broker_order_id=state.broker_order_id,
                filled_qty=state.filled_qty,
                avg_price=state.avg_price,
                recheck_at=None if terminal else next_recheck(self.wall()),
            )
            if not ok:
                return False  # someone else updated it first
            payload = {"from": seen.value, "status": new.value, "filled_qty": state.filled_qty}
            await repo.append_event(
                s, leg.run_id, "leg.rechecked", payload, self.wall(), leg_id=leg.id
            )
            await self._refresh_finalised_run(s, leg.run_id)
        return True

    async def _refresh_finalised_run(self, s: AsyncSession, run_id: str) -> None:
        """A finalised run whose leg changed: recompute status + execution.updated outbox row."""
        run = await repo.get_run(s, run_id)
        if run is None or run.status not in TERMINAL_RUN_STATUSES:
            return  # the executor still owns it and will finalise
        legs = await repo.get_legs(s, run_id)
        status = run_status(lg.status for lg in legs)
        await repo.set_final_status(s, run_id, status)
        seq = await repo.append_event(
            s, run_id, "run.updated", {"status": status.value}, self.wall()
        )
        run = await repo.get_run(s, run_id)
        assert run is not None
        payload = build_payload(
            run, legs, broker=self.broker.meta.id, event="execution.updated", seq=seq
        )
        url = run.options_json.get("webhook_url") or self.default_webhook_url or None
        await repo.add_outbox(s, run_id, url, payload, self.wall())
