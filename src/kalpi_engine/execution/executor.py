"""Run executor core (SPEC §5 steps 2, 4, 7; D7, D9, D21).

Per leg: CAS PLANNED->SUBMITTING is committed *before* place_order (write-ahead); only
RETRY_SAFE errors retry; AMBIGUOUS -> UNKNOWN with a recheck, never resent. Sells finish
(terminal or poll timeout) before any buy starts.

Not here yet: lease claim/renew + fencing (B4c; writes pass fence=None and rely on the status
CAS), tag reconciliation of AMBIGUOUS and the recheck sweep (B4b), notifications (B5).
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.domain.enums import LegStatus, OrderStatus, Phase, RunStatus
from kalpi_engine.domain.errors import AmbiguousSubmit, AuthExpired, KalpiError
from kalpi_engine.domain.models import BrokerOrderState, OrderIntent
from kalpi_engine.domain.status import run_status, sell_failed
from kalpi_engine.execution.limits import (
    RETRY_SAFE,
    Clock,
    LimiterRegistry,
    RetryPolicy,
    call_with_retry,
)
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Leg

log = logging.getLogger(__name__)

RECHECK_AFTER = timedelta(seconds=60)
_TERMINAL = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}


@dataclass(frozen=True)
class ExecConfig:
    poll_interval_s: float = 1.0
    max_concurrency: int = 5
    retry: RetryPolicy = field(default_factory=RetryPolicy)


def _utcnow() -> datetime:
    return datetime.now(UTC)


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

    async def run(self, run_id: str) -> RunStatus:
        async with self.sm.begin() as s:
            run = await repo.get_run(s, run_id)
            if run is None:
                raise LookupError(run_id)
            legs = await repo.get_legs(s, run_id)
            opts = run.options_json
            await repo.update_run_unfenced(s, run_id, status=RunStatus.RUNNING)
            await repo.append_event(s, run_id, "run.running", {}, self.wall())
        poll_timeout_s = float(opts.get("poll_timeout_s", 60))
        rl = self.broker.meta.rate_limits
        self._orders = self._limiters.get(
            self.broker.meta.id, run.session_id, "orders", rl.orders_per_sec
        )
        self._reads = self._limiters.get(
            self.broker.meta.id, run.session_id, "reads", rl.reads_per_sec
        )

        sells = [lg for lg in legs if lg.phase is Phase.SELL]
        buys = [lg for lg in legs if lg.phase is Phase.BUY]
        await self._phase(sells, poll_timeout_s)

        # Barrier (D21): every sell is terminal, UNKNOWN, or past poll timeout here.
        async with self.sm() as s:
            sells = [lg for lg in await repo.get_legs(s, run_id) if lg.phase is Phase.SELL]
        failed = [lg for lg in sells if sell_failed(lg.status)]
        halt = bool(failed) and bool(opts.get("halt_on_sell_failure", False))
        if failed:
            payload = {
                "halted": halt,
                "failed": [{"symbol": lg.symbol, "status": lg.status.value} for lg in failed],
            }
            async with self.sm.begin() as s:
                await repo.append_event(s, run_id, "barrier.sell_failures", payload, self.wall())
        if halt:
            for lg in buys:
                await self._skip(lg, "SELL_FAILURE_HALT")
        else:
            await self._phase(buys, poll_timeout_s)
        return await self._finalise(run_id)

    # --- phases / legs --------------------------------------------------------------
    async def _phase(self, legs: list[Leg], poll_timeout_s: float) -> None:
        await asyncio.gather(*(self._guarded(lg, poll_timeout_s) for lg in legs))

    async def _guarded(self, leg: Leg, poll_timeout_s: float) -> None:
        async with self._sem:
            if self._stop_reason is not None:
                await self._skip(leg, self._stop_reason)
                return
            await self._leg(leg, poll_timeout_s)

    async def _leg(self, leg: Leg, poll_timeout_s: float) -> None:
        if not await self._move(leg, LegStatus.PLANNED, LegStatus.SUBMITTING, "leg.submitting"):
            return  # someone else owns this leg
        intent = OrderIntent(
            leg_id=leg.id,
            phase=leg.phase,
            symbol=leg.symbol,
            exchange=leg.exchange,
            side=leg.side,
            quantity=leg.qty,
            order_type=leg.order_type,
            limit_price=leg.limit_price,
            tag=leg.tag,
        )
        try:
            order_id = await call_with_retry(
                lambda: self.broker.place_order(self.session, intent),
                limiter=self._orders,
                clock=self.clock,
                policy=self.cfg.retry,
            )
        except RETRY_SAFE as e:  # the broker provably never accepted it
            await self._move(
                leg, LegStatus.SUBMITTING, LegStatus.FAILED, "leg.failed", reason=_reason(e)
            )
            return
        except AuthExpired as e:  # refused before acceptance; D32 mapping completes in B4b
            self._stop_reason = "AUTH_EXPIRED"
            await self._move(
                leg, LegStatus.SUBMITTING, LegStatus.FAILED, "leg.failed", reason=_reason(e)
            )
            return
        except AmbiguousSubmit as e:
            await self._unknown(leg, LegStatus.SUBMITTING, _reason(e))
            return
        except KalpiError as e:  # REJECTED class: validation / RMS / funds
            await self._move(
                leg, LegStatus.SUBMITTING, LegStatus.REJECTED, "leg.rejected", reason=_reason(e)
            )
            return
        except Exception as e:  # unexpected after send: the order may exist (D9)
            log.exception("place_order crashed for leg %s", leg.id)
            await self._unknown(leg, LegStatus.SUBMITTING, f"UNEXPECTED: {type(e).__name__}")
            return
        await self._move(
            leg,
            LegStatus.SUBMITTING,
            LegStatus.SUBMITTED,
            "leg.submitted",
            broker_order_id=order_id,
        )
        await self._poll(leg, order_id, poll_timeout_s)

    async def _poll(self, leg: Leg, order_id: str, poll_timeout_s: float) -> None:
        deadline = self.clock.now() + poll_timeout_s
        current = LegStatus.SUBMITTED
        while True:
            try:
                state = await call_with_retry(
                    lambda: self.broker.get_order(self.session, order_id),
                    limiter=self._reads,
                    clock=self.clock,
                    policy=self.cfg.retry,
                )
            except Exception as e:  # order exists but we cannot see it: in doubt
                await self._unknown(leg, current, f"POLL_ERROR: {_reason(e)}")
                return
            new = LegStatus(state.status.value)
            if new is not current:
                values = _fill_values(state)
                if not await self._move(leg, current, new, f"leg.{new.value.lower()}", **values):
                    return
                current = new
            if state.status in _TERMINAL:
                return
            if self.clock.now() >= deadline:  # leave OPEN/PARTIAL; never auto-cancel
                await self._set_recheck(leg, current)
                return
            await self.clock.sleep(self.cfg.poll_interval_s)

    # --- state transitions ------------------------------------------------------------
    async def _move(
        self, leg: Leg, expected: LegStatus, new: LegStatus, event: str, **values: Any
    ) -> bool:
        payload = {"status": new.value} | {
            k: v for k, v in values.items() if k in ("broker_order_id", "filled_qty", "reason")
        }
        async with self.sm.begin() as s:
            ok = await repo.cas_leg(s, leg.id, expected, None, status=new, **values)
            if ok:
                await repo.append_event(s, leg.run_id, event, payload, self.wall(), leg_id=leg.id)
        if not ok:
            log.warning("leg %s CAS %s->%s lost", leg.id, expected, new)
        return ok

    async def _unknown(self, leg: Leg, expected: LegStatus, reason: str) -> None:
        await self._move(
            leg,
            expected,
            LegStatus.UNKNOWN,
            "leg.unknown",
            reason=reason,
            recheck_at=self.wall() + RECHECK_AFTER,
        )

    async def _set_recheck(self, leg: Leg, current: LegStatus) -> None:
        async with self.sm.begin() as s:
            await repo.cas_leg(s, leg.id, current, None, recheck_at=self.wall() + RECHECK_AFTER)

    async def _skip(self, leg: Leg, reason: str) -> None:
        await self._move(leg, LegStatus.PLANNED, LegStatus.SKIPPED, "leg.skipped", reason=reason)

    async def _finalise(self, run_id: str) -> RunStatus:
        async with self.sm.begin() as s:
            legs = await repo.get_legs(s, run_id)
            status = run_status(lg.status for lg in legs)
            await repo.update_run_unfenced(s, run_id, status=status, finished_at=self.wall())
            counts: dict[str, int] = {}
            for lg in legs:
                counts[lg.status.value] = counts.get(lg.status.value, 0) + 1
            await repo.append_event(
                s, run_id, "run.finished", {"status": status.value, "legs": counts}, self.wall()
            )
        return status


def _fill_values(state: BrokerOrderState) -> dict[str, Any]:
    values: dict[str, Any] = {"filled_qty": state.filled_qty, "avg_price": state.avg_price}
    if state.message:
        values["reason"] = state.message
    return values


def _reason(e: BaseException) -> str:
    code = getattr(e, "code", type(e).__name__)
    return f"{code}: {e}"[:500]
