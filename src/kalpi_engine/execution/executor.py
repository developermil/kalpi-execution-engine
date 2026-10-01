"""Run executor core (SPEC §5 steps 2, 4, 7; D7, D9, D21).

Per leg: CAS PLANNED->SUBMITTING is committed *before* place_order (write-ahead); only
RETRY_SAFE errors retry; AMBIGUOUS -> adopt by tag (3 lookups) or UNKNOWN, never resent.
Sells finish (terminal or poll timeout) before any buy starts. AuthExpired stops placing (D32).

Not here yet: lease claim/renew + fencing (B4c; writes pass fence=None and rely on the status
CAS).
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.domain.enums import LegStatus, OrderStatus, Phase, RunStatus
from kalpi_engine.domain.errors import AmbiguousSubmit, AuthExpired, KalpiError
from kalpi_engine.domain.models import OrderIntent
from kalpi_engine.domain.status import run_status, sell_failed
from kalpi_engine.execution.legs import LegWriter, fill_values, reason_of
from kalpi_engine.execution.limits import (
    RETRY_SAFE,
    Clock,
    LimiterRegistry,
    RetryPolicy,
    call_with_retry,
)
from kalpi_engine.execution.reconcile import Reconciler, next_recheck
from kalpi_engine.notify.payload import build_payload
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Leg

log = logging.getLogger(__name__)

_TERMINAL = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}


@dataclass(frozen=True)
class ExecConfig:
    poll_interval_s: float = 1.0
    max_concurrency: int = 5
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    default_webhook_url: str | None = None  # None -> console sink


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
        self.legs = LegWriter(sm, wall)
        self.rec = Reconciler(
            sm,
            broker,
            session,
            limiters,
            clock=clock,
            wall=wall,
            default_webhook_url=self.cfg.default_webhook_url,
        )

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
                await self.legs.skip(lg, "SELL_FAILURE_HALT")
        else:
            await self._phase(buys, poll_timeout_s)
        return await self._finalise(run_id)

    # --- phases / legs --------------------------------------------------------------
    async def _phase(self, legs: list[Leg], poll_timeout_s: float) -> None:
        await asyncio.gather(*(self._guarded(lg, poll_timeout_s) for lg in legs))

    async def _guarded(self, leg: Leg, poll_timeout_s: float) -> None:
        async with self._sem:
            if self._stop_reason is not None:
                await self.legs.skip(leg, self._stop_reason)
                return
            await self._leg(leg, poll_timeout_s)

    async def _leg(self, leg: Leg, poll_timeout_s: float) -> None:
        if not await self.legs.move(leg, LegStatus.PLANNED, LegStatus.SUBMITTING, "leg.submitting"):
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
            await self.legs.move(
                leg, LegStatus.SUBMITTING, LegStatus.FAILED, "leg.failed", reason=reason_of(e)
            )
            return
        except AuthExpired as e:  # 401 before acceptance: nothing was placed
            self._stop_reason = "AUTH_EXPIRED"
            await self.legs.move(
                leg, LegStatus.SUBMITTING, LegStatus.FAILED, "leg.failed", reason=reason_of(e)
            )
            return
        except AmbiguousSubmit as e:
            adopted = await self._adopt_by_tag(leg, reason_of(e))
            if adopted is None:
                return
            order_id = adopted
            await self._poll(leg, order_id, poll_timeout_s)
            return
        except KalpiError as e:  # REJECTED class: validation / RMS / funds
            await self.legs.move(
                leg, LegStatus.SUBMITTING, LegStatus.REJECTED, "leg.rejected", reason=reason_of(e)
            )
            return
        except Exception as e:  # unexpected after send: the order may exist (D9)
            log.exception("place_order crashed for leg %s", leg.id)
            await self.legs.unknown(leg, LegStatus.SUBMITTING, f"UNEXPECTED: {type(e).__name__}")
            return
        await self.legs.move(
            leg,
            LegStatus.SUBMITTING,
            LegStatus.SUBMITTED,
            "leg.submitted",
            broker_order_id=order_id,
        )
        await self._poll(leg, order_id, poll_timeout_s)

    async def _adopt_by_tag(self, leg: Leg, reason: str) -> str | None:
        """AMBIGUOUS (D9): look the tag up; adopt the order if found, else UNKNOWN. Never resend."""
        try:
            state = await self.rec.find_by_tag(leg.tag)
        except AuthExpired:
            self._stop_reason = "AUTH_EXPIRED"
            await self.legs.unknown(leg, LegStatus.SUBMITTING, "AUTH_EXPIRED", recheck=False)
            return None
        except Exception as e:
            await self.legs.unknown(
                leg, LegStatus.SUBMITTING, f"{reason}; LOOKUP_ERROR: {reason_of(e)}"
            )
            return None
        if state is None:
            await self.legs.unknown(leg, LegStatus.SUBMITTING, reason)
            return None
        moved = await self.legs.move(
            leg,
            LegStatus.SUBMITTING,
            LegStatus.SUBMITTED,
            "leg.adopted",
            broker_order_id=state.broker_order_id,
        )
        return state.broker_order_id if moved else None

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
                unknown = [
                    lg for lg in await repo.get_legs(s, run_id) if lg.status is LegStatus.UNKNOWN
                ]
            for lg in unknown:
                await self.rec.recheck_leg(lg)
        async with self.sm.begin() as s:
            legs = await repo.get_legs(s, run_id)
            status = run_status(lg.status for lg in legs)
            await repo.update_run_unfenced(s, run_id, status=status, finished_at=self.wall())
            counts: dict[str, int] = {}
            for lg in legs:
                counts[lg.status.value] = counts.get(lg.status.value, 0) + 1
            seq = await repo.append_event(
                s, run_id, "run.finished", {"status": status.value, "legs": counts}, self.wall()
            )
            run = await repo.get_run(s, run_id)
            assert run is not None
            payload = build_payload(
                run, legs, broker=self.broker.meta.id, event="execution.completed", seq=seq
            )
            url = run.options_json.get("webhook_url") or self.cfg.default_webhook_url or None
            # Same transaction as the final status (SPEC §5 step 8): never a run without its outbox.
            await repo.add_outbox(s, run_id, url, payload, self.wall())
        return status
