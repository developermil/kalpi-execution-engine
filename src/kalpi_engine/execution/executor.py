"""Run executor (SPEC §5 steps 2-7, 9; D7, D9, D20, D21, D30, D32).

Only the lease holder executes; every write is fenced. Write-ahead CAS PLANNED->SUBMITTING
before place_order; only RETRY_SAFE retries; AMBIGUOUS -> adopt by tag or UNKNOWN, never resent.
Sells settle before buys. On resume, SUBMITTING legs are reconciled by tag, SUBMITTED/OPEN/
PARTIAL re-polled; only PLANNED legs are ever placed.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.domain.enums import (
    TERMINAL_RUN_STATUSES,
    LegStatus,
    OrderStatus,
    Phase,
    RunStatus,
)
from kalpi_engine.domain.errors import AmbiguousSubmit, AuthExpired, KalpiError
from kalpi_engine.domain.models import OrderIntent
from kalpi_engine.domain.status import sell_failed
from kalpi_engine.execution.finalise import write_final
from kalpi_engine.execution.lease import Lease
from kalpi_engine.execution.legs import LegWriter, fill_values, reason_of
from kalpi_engine.execution.limits import (
    RETRY_SAFE,
    Clock,
    LimiterRegistry,
    RetryPolicy,
    call_with_retry,
)
from kalpi_engine.execution.reconcile import Reconciler, next_recheck
from kalpi_engine.logging import log_context
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Leg

log = logging.getLogger(__name__)

_TERMINAL = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
_POLLABLE = (LegStatus.SUBMITTED, LegStatus.OPEN, LegStatus.PARTIAL)


@dataclass(frozen=True)
class ExecConfig:
    poll_interval_s: float = 1.0
    max_concurrency: int = 5
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    default_webhook_url: str | None = None  # None -> console sink
    lease_ttl_s: int = 30
    owner: str = field(default_factory=lambda: f"w-{uuid4().hex[:12]}")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LeaseLost(Exception):
    """Internal: another worker owns the run now; stop without writing."""


class Executor:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        broker: BrokerAdapter,
        session: BrokerSession,
        limiters: LimiterRegistry,
        *,
        clock: Clock,
        config: ExecConfig | None = None,
        wall: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.sm, self.broker, self.session = sm, broker, session
        self.clock, self.wall = clock, wall
        self.cfg = config or ExecConfig()
        self._limiters = limiters
        self._sem = asyncio.Semaphore(self.cfg.max_concurrency)
        self._stop_reason: str | None = None  # set on AuthExpired: stop placing
        self.lease: Lease | None = None
        self.legs = LegWriter(sm, wall, lambda: self._lease().fence())
        self.rec = Reconciler(
            sm, broker, session, limiters, clock=clock, wall=wall,
            default_webhook_url=self.cfg.default_webhook_url,
        )  # fmt: skip

    def _lease(self) -> Lease:
        assert self.lease is not None, "fenced write outside run()"
        return self.lease

    async def run(self, run_id: str) -> RunStatus | None:
        """Execute or resume a run. None = not ours (lease held elsewhere) or lease lost."""
        self.lease = Lease(
            self.sm, run_id, self.cfg.owner, self.cfg.lease_ttl_s, clock=self.clock, wall=self.wall
        )
        if not await self.lease.claim():
            return None
        self.lease.start_renewing()
        try:
            with log_context(run_id=run_id):
                log.info("run started", extra={"owner": self.cfg.owner})
                status = await self._execute(run_id)
                log.info("run finished", extra={"status": status.value})
                return status
        except LeaseLost:
            log.warning("run %s: lease lost by %s; stopping", run_id, self.cfg.owner)
            return None
        finally:
            await self.lease.stop()

    async def _execute(self, run_id: str) -> RunStatus:
        async with self.sm.begin() as s:
            run = await repo.get_run(s, run_id)
            if run is None:
                raise LookupError(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                return run.status
            event = "run.resumed" if run.status is RunStatus.RUNNING else "run.running"
            if not await repo.update_run(s, self._lease().fence(), status=RunStatus.RUNNING):
                raise LeaseLost
            await repo.append_event(s, run_id, event, {"owner": self.cfg.owner}, self.wall())
            legs = await repo.get_legs(s, run_id)
        opts = run.options_json
        poll_timeout_s = float(opts.get("poll_timeout_s", 60))
        meta = self.broker.meta
        self._orders = self._limiters.get(
            meta.id, run.session_id, "orders", meta.rate_limits.orders_per_sec
        )
        self._reads = self._limiters.get(
            meta.id, run.session_id, "reads", meta.rate_limits.reads_per_sec
        )

        await self._phase([lg for lg in legs if lg.phase is Phase.SELL], poll_timeout_s)
        await self._check_lease()
        # Barrier (D21): every sell is terminal, UNKNOWN, or past poll timeout here.
        async with self.sm() as s:
            legs = await repo.get_legs(s, run_id)
        failed = [lg for lg in legs if lg.phase is Phase.SELL and sell_failed(lg.status)]
        buys = [lg for lg in legs if lg.phase is Phase.BUY]
        halt = bool(failed) and bool(opts.get("halt_on_sell_failure", False))
        if failed:
            payload = {
                "halted": halt,
                "failed": [{"symbol": lg.symbol, "status": lg.status.value} for lg in failed],
            }
            async with self.sm.begin() as s:
                if not await repo.lease_held(s, self._lease().fence()):
                    raise LeaseLost
                await repo.append_event(s, run_id, "barrier.sell_failures", payload, self.wall())
        if halt:
            for lg in buys:
                if lg.status is LegStatus.PLANNED:
                    await self.legs.skip(lg, "SELL_FAILURE_HALT")
        else:
            await self._phase(buys, poll_timeout_s)
        await self._check_lease()
        return await self._finalise(run_id)

    async def _check_lease(self) -> None:
        if not await self._lease().held():
            raise LeaseLost

    # --- phases / legs --------------------------------------------------------------
    async def _phase(self, legs: list[Leg], poll_timeout_s: float) -> None:
        await asyncio.gather(*(self._guarded(lg, poll_timeout_s) for lg in legs))

    async def _guarded(self, leg: Leg, poll_timeout_s: float) -> None:
        with log_context(leg_id=leg.id):
            await self._guarded_inner(leg, poll_timeout_s)

    async def _guarded_inner(self, leg: Leg, poll_timeout_s: float) -> None:
        async with self._sem:
            if self._lease().lost:
                return  # the next owner resumes this leg
            if leg.status is LegStatus.PLANNED:
                if self._stop_reason is not None:
                    await self.legs.skip(leg, self._stop_reason)
                else:
                    await self._leg(leg, poll_timeout_s)
            elif leg.status is LegStatus.SUBMITTING:  # crashed mid-submit: tag decides (D30)
                order_id = await self._adopt_by_tag(leg, "RESUMED_SUBMITTING")
                if order_id is not None:
                    await self._poll(leg, order_id, poll_timeout_s, LegStatus.SUBMITTED)
            elif leg.status in _POLLABLE and leg.broker_order_id:
                await self._poll(leg, leg.broker_order_id, poll_timeout_s, leg.status)

    async def _leg(self, leg: Leg, poll_timeout_s: float) -> None:
        if not await self.legs.move(leg, LegStatus.PLANNED, LegStatus.SUBMITTING, "leg.submitting"):
            return  # lease lost or someone else owns this leg
        if self._lease().lost:
            return  # renew failed: do not place (the next owner reconciles by tag)
        intent = OrderIntent(
            leg_id=leg.id, phase=leg.phase, symbol=leg.symbol, exchange=leg.exchange,
            side=leg.side, quantity=leg.qty, order_type=leg.order_type,
            limit_price=leg.limit_price, tag=leg.tag,
        )  # fmt: skip
        sub = LegStatus.SUBMITTING
        info = {"symbol": leg.symbol, "side": leg.side.value, "tag": leg.tag}
        log.info("placing order", extra=info)
        try:
            order_id = await call_with_retry(
                lambda: self.broker.place_order(self.session, intent),
                limiter=self._orders,
                clock=self.clock,
                policy=self.cfg.retry,
            )
        except RETRY_SAFE as e:  # the broker provably never accepted it
            await self.legs.move(leg, sub, LegStatus.FAILED, "leg.failed", reason=reason_of(e))
            return
        except AuthExpired as e:  # 401 before acceptance: nothing was placed
            self._stop_reason = "AUTH_EXPIRED"
            await self.legs.move(leg, sub, LegStatus.FAILED, "leg.failed", reason=reason_of(e))
            return
        except AmbiguousSubmit as e:
            adopted = await self._adopt_by_tag(leg, reason_of(e))
            if adopted is not None:
                await self._poll(leg, adopted, poll_timeout_s, LegStatus.SUBMITTED)
            return
        except KalpiError as e:  # REJECTED class: validation / RMS / funds
            await self.legs.move(leg, sub, LegStatus.REJECTED, "leg.rejected", reason=reason_of(e))
            return
        except Exception as e:  # unexpected after send: the order may exist (D9)
            log.exception("place_order crashed for leg %s", leg.id)
            await self.legs.unknown(leg, sub, f"UNEXPECTED: {type(e).__name__}")
            return
        if await self.legs.move(
            leg, sub, LegStatus.SUBMITTED, "leg.submitted", broker_order_id=order_id
        ):
            await self._poll(leg, order_id, poll_timeout_s, LegStatus.SUBMITTED)

    async def _adopt_by_tag(self, leg: Leg, reason: str) -> str | None:
        """AMBIGUOUS (D9): look the tag up; adopt the order if found, else UNKNOWN. Never resend."""
        sub = LegStatus.SUBMITTING
        try:
            state = await self.rec.find_by_tag(leg.tag)
        except AuthExpired:
            self._stop_reason = "AUTH_EXPIRED"
            await self.legs.unknown(leg, sub, "AUTH_EXPIRED", recheck=False)
            return None
        except Exception as e:
            await self.legs.unknown(leg, sub, f"{reason}; LOOKUP_ERROR: {reason_of(e)}")
            return None
        if state is None:
            await self.legs.unknown(leg, sub, reason)
            return None
        moved = await self.legs.move(
            leg, sub, LegStatus.SUBMITTED, "leg.adopted", broker_order_id=state.broker_order_id
        )
        return state.broker_order_id if moved else None

    async def _poll(
        self, leg: Leg, order_id: str, poll_timeout_s: float, current: LegStatus
    ) -> None:
        deadline = self.clock.now() + poll_timeout_s
        while not self._lease().lost:
            try:
                state = await call_with_retry(
                    lambda: self.broker.get_order(self.session, order_id),
                    limiter=self._reads,
                    clock=self.clock,
                    policy=self.cfg.retry,
                )
            except AuthExpired:  # D32: PARTIAL kept, otherwise UNKNOWN; rechecks need auth too
                self._stop_reason = "AUTH_EXPIRED"
                if current is LegStatus.PARTIAL:
                    await self.legs.set_recheck(leg, current, None)
                else:
                    await self.legs.unknown(leg, current, "AUTH_EXPIRED", recheck=False)
                return
            except Exception as e:  # order exists but we cannot see it: in doubt
                await self.legs.unknown(leg, current, f"POLL_ERROR: {reason_of(e)}")
                return
            new = LegStatus(state.status.value)
            if new is not current:
                values = fill_values(state)
                if not await self.legs.move(
                    leg, current, new, f"leg.{new.value.lower()}", **values
                ):
                    return
                current = new
            if state.status in _TERMINAL:
                return
            if self.clock.now() >= deadline:  # leave OPEN/PARTIAL; never auto-cancel
                await self.legs.set_recheck(leg, current, next_recheck(self.wall()))
                return
            await self.clock.sleep(self.cfg.poll_interval_s)

    async def _finalise(self, run_id: str) -> RunStatus:
        if self._stop_reason is None:  # one last tag/order lookup for UNKNOWN legs (D21)
            async with self.sm() as s:
                legs = await repo.get_legs(s, run_id)
            for lg in legs:
                if lg.status is LegStatus.UNKNOWN:
                    await self.rec.recheck_leg(lg)
        status = await write_final(
            self.sm,
            self._lease().fence(),
            broker_id=self.broker.meta.id,
            default_webhook_url=self.cfg.default_webhook_url,
            now=self.wall(),
        )
        if status is None:
            raise LeaseLost
        return status
