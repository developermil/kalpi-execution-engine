"""Executor core on Paper (SPEC §5 steps 2, 4, 7; D7, D9, D21; run status per SPEC §3)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.base import BrokerSession
from kalpi_engine.brokers.paper import Faults, PaperBroker
from kalpi_engine.domain.enums import LegStatus, Phase, RunStatus
from kalpi_engine.domain.schemas import ExecuteRequest
from kalpi_engine.domain.status import run_status
from kalpi_engine.execution.executor import ExecConfig, Executor
from kalpi_engine.execution.limits import LimiterRegistry, RetryPolicy
from kalpi_engine.planner import plan
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Event, Leg, create_all, make_engine, make_sessionmaker
from tests.engine.fakes import T0, FakeClock

SM = async_sessionmaker[AsyncSession]


@pytest.fixture
async def sm(tmp_path: Path) -> AsyncIterator[SM]:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}")
    await create_all(eng)
    yield make_sessionmaker(eng)
    await eng.dispose()


@dataclass
class Result:
    status: RunStatus
    legs: list[Leg]
    events: list[Event]
    broker: PaperBroker

    def leg(self, symbol: str) -> Leg:
        return next(lg for lg in self.legs if lg.symbol == symbol)

    def statuses(self) -> dict[str, LegStatus]:
        return {lg.symbol: lg.status for lg in self.legs}


async def execute(
    sm: SM,
    mode: str,
    instructions: list[dict[str, Any]],
    *,
    holdings: dict[str, int] | None = None,
    faults: Faults | None = None,
    retry: RetryPolicy | None = None,
    **options: Any,
) -> Result:
    broker = PaperBroker(faults)
    session: BrokerSession = await broker.create_session({"holdings": holdings or {}})
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
        sm,
        run_id=run_id,
        user_id="u1",
        idempotency_key=run_id,
        request_hash="h",
        session_id=str(body.session_id),
        mode=body.mode,
        options=body.options.model_dump(mode="json"),
        legs=plan(body, run_id),
        now=datetime.now(UTC),
    )
    clock = FakeClock()
    clock.watch(sm)
    ex = Executor(
        sm,
        broker,
        session,
        LimiterRegistry(clock),
        clock=clock,
        wall=lambda: T0 + timedelta(seconds=clock.now() - 1000.0),
        config=ExecConfig(poll_interval_s=1.0, retry=retry or RetryPolicy(max_attempts=4)),
    )
    status = await ex.run(run_id)
    async with sm() as s:
        legs = await repo.get_legs(s, run_id)
        events = await repo.get_events(s, run_id)
        run = await repo.get_run(s, run_id)
    assert run is not None and run.status == status and run.finished_at is not None
    assert status == run_status(lg.status for lg in legs)  # SPEC §3 table, always
    return Result(status, legs, events, broker)


def buy(sym: str, q: int, **kw: Any) -> dict[str, Any]:
    return {"symbol": sym, "action": "BUY", "quantity": q, **kw}


def sell(sym: str, q: int, **kw: Any) -> dict[str, Any]:
    return {"symbol": sym, "action": "SELL", "quantity": q, **kw}


def stuck_sell(sym: str, q: int) -> dict[str, Any]:
    """A LIMIT sell far above market never crosses: stays OPEN at Paper."""
    return sell(sym, q, order_type="LIMIT", limit_price=1_000_000.0)


# ---------- happy paths ----------


async def test_happy_first_time(sm: SM) -> None:
    r = await execute(sm, "FIRST_TIME", [buy("RELIANCE", 2), buy("TCS", 1), buy("ITC", 10)])
    assert r.status is RunStatus.COMPLETED
    assert set(r.statuses().values()) == {LegStatus.FILLED}
    assert all(lg.broker_order_id and lg.filled_qty == lg.qty for lg in r.legs)
    assert r.broker.place_calls == 3


async def test_happy_rebalance_sells_resolved_before_first_buy(sm: SM) -> None:
    r = await execute(
        sm,
        "REBALANCE",
        [
            buy("SBIN", 5),
            sell("TCS", 5),
            {"symbol": "INFY", "action": "REBALANCE", "side": "BUY", "quantity": 2},
            {"symbol": "ITC", "action": "REBALANCE", "side": "SELL", "quantity": 10},
        ],
        holdings={"TCS": 5, "INFY": 8, "ITC": 40},
        faults=Faults(fill_after_polls=2),  # sells take a few polls to fill
    )
    assert r.status is RunStatus.COMPLETED
    sell_ids = {lg.id for lg in r.legs if lg.phase is Phase.SELL}
    buy_ids = {lg.id for lg in r.legs if lg.phase is Phase.BUY}
    last_sell_terminal = max(
        e.seq for e in r.events if e.leg_id in sell_ids and e.type == "leg.filled"
    )
    first_buy_submit = min(
        e.seq for e in r.events if e.leg_id in buy_ids and e.type == "leg.submitting"
    )
    assert last_sell_terminal < first_buy_submit
    assert [e.type for e in r.events][:2] == ["run.created", "run.running"]
    assert r.events[-1].type == "run.finished"
    assert [e.seq for e in r.events] == list(range(1, len(r.events) + 1))


# ---------- 429 storm ----------


async def test_429_storm_every_leg_filled_exactly_one_order_each(sm: SM) -> None:
    r = await execute(
        sm,
        "FIRST_TIME",
        [buy(s, 1) for s in ("RELIANCE", "TCS", "ITC", "SBIN", "WIPRO")],
        faults=Faults(rate_limit_next=12),
        retry=RetryPolicy(max_attempts=20, base_s=0.1),
    )
    assert r.status is RunStatus.COMPLETED
    assert r.broker.place_calls == 5 + 12
    assert len({lg.broker_order_id for lg in r.legs}) == 5


async def test_429_exhausted_leg_failed_never_sent(sm: SM) -> None:
    r = await execute(
        sm,
        "FIRST_TIME",
        [buy("ITC", 1)],
        faults=Faults(rate_limit_next=99),
        retry=RetryPolicy(max_attempts=3, base_s=0.1),
    )
    assert r.statuses() == {"ITC": LegStatus.FAILED}
    assert r.leg("ITC").reason and "RATE_LIMITED" in r.leg("ITC").reason
    assert r.status is RunStatus.FAILED


# ---------- rejects ----------


async def test_partial_rejects(sm: SM) -> None:
    r = await execute(
        sm,
        "FIRST_TIME",
        [buy("RELIANCE", 1), buy("TCS", 1), buy("ITC", 1)],
        faults=Faults(reject_symbols={"TCS"}),
    )
    assert r.statuses() == {
        "RELIANCE": LegStatus.FILLED,
        "TCS": LegStatus.REJECTED,
        "ITC": LegStatus.FILLED,
    }
    assert r.leg("TCS").reason and "BROKER_REJECTED" in r.leg("TCS").reason
    assert r.status is RunStatus.COMPLETED_WITH_FAILURES


async def test_all_rejected_is_failed(sm: SM) -> None:
    r = await execute(
        sm,
        "FIRST_TIME",
        [buy("TCS", 1), buy("ITC", 1)],
        faults=Faults(reject_symbols={"TCS", "ITC"}),
    )
    assert r.status is RunStatus.FAILED


async def test_insufficient_funds_rejected(sm: SM) -> None:
    r = await execute(sm, "FIRST_TIME", [buy("TCS", 10_000)])
    assert r.statuses() == {"TCS": LegStatus.REJECTED}
    assert r.status is RunStatus.FAILED


async def test_ambiguous_submit_marked_unknown_never_resent(sm: SM) -> None:
    faults = Faults(timeout_before_accept_next=1)
    r = await execute(sm, "FIRST_TIME", [buy("TCS", 1)], faults=faults)
    assert r.statuses() == {"TCS": LegStatus.UNKNOWN}
    assert r.broker.place_calls == 1
    assert r.status is RunStatus.COMPLETED_WITH_FAILURES


# ---------- polling ----------


async def test_buy_left_open_at_poll_timeout_not_cancelled(sm: SM) -> None:
    r = await execute(
        sm, "FIRST_TIME", [buy("TCS", 1, order_type="LIMIT", limit_price=1.0)], poll_timeout_s=3
    )
    leg = r.leg("TCS")
    assert leg.status is LegStatus.OPEN and leg.recheck_at is not None
    assert r.status is RunStatus.COMPLETED_WITH_FAILURES


# ---------- barrier (D21) ----------


@pytest.mark.parametrize(
    ("instr", "faults", "sell_status"),
    [
        (stuck_sell("TCS", 5), None, LegStatus.OPEN),
        (sell("TCS", 5), Faults(partial_fill={"TCS": 2}), LegStatus.PARTIAL),
    ],
    ids=["open-at-timeout", "partial"],
)
async def test_unresolved_sell_with_halt_skips_buys(
    sm: SM, instr: dict[str, Any], faults: Faults | None, sell_status: LegStatus
) -> None:
    r = await execute(
        sm,
        "REBALANCE",
        [instr, buy("ITC", 3), buy("SBIN", 1)],
        holdings={"TCS": 5},
        faults=faults,
        halt_on_sell_failure=True,
        poll_timeout_s=3,
    )
    assert r.statuses() == {
        "TCS": sell_status,
        "ITC": LegStatus.SKIPPED,
        "SBIN": LegStatus.SKIPPED,
    }
    assert r.leg("ITC").reason == "SELL_FAILURE_HALT"
    assert r.broker.place_calls == 1
    barrier = next(e for e in r.events if e.type == "barrier.sell_failures")
    assert barrier.payload_json["halted"] is True
    assert barrier.payload_json["failed"] == [{"symbol": "TCS", "status": sell_status.value}]
    assert r.status is RunStatus.COMPLETED_WITH_FAILURES


@pytest.mark.parametrize(
    ("instr", "faults", "sell_status"),
    [
        (stuck_sell("TCS", 5), None, LegStatus.OPEN),
        (sell("TCS", 5), Faults(partial_fill={"TCS": 2}), LegStatus.PARTIAL),
    ],
    ids=["open-at-timeout", "partial"],
)
async def test_unresolved_sell_without_halt_buys_proceed_failure_listed(
    sm: SM, instr: dict[str, Any], faults: Faults | None, sell_status: LegStatus
) -> None:
    r = await execute(
        sm,
        "REBALANCE",
        [instr, buy("ITC", 3)],
        holdings={"TCS": 5},
        faults=faults,
        halt_on_sell_failure=False,
        poll_timeout_s=3,
    )
    assert r.statuses() == {"TCS": sell_status, "ITC": LegStatus.FILLED}
    barrier = next(e for e in r.events if e.type == "barrier.sell_failures")
    assert barrier.payload_json == {
        "halted": False,
        "failed": [{"symbol": "TCS", "status": sell_status.value}],
    }
    assert r.status is RunStatus.COMPLETED_WITH_FAILURES


async def test_rejected_sell_with_halt_skips_buys_run_failed(sm: SM) -> None:
    r = await execute(
        sm,
        "REBALANCE",
        [sell("TCS", 5), buy("ITC", 1)],
        holdings={"TCS": 5},
        faults=Faults(reject_symbols={"TCS"}),
        halt_on_sell_failure=True,
    )
    assert r.statuses() == {"TCS": LegStatus.REJECTED, "ITC": LegStatus.SKIPPED}
    assert r.status is RunStatus.FAILED  # nothing executed, nothing in doubt


async def test_no_barrier_event_when_all_sells_fill(sm: SM) -> None:
    r = await execute(sm, "REBALANCE", [sell("TCS", 5), buy("ITC", 1)], holdings={"TCS": 5})
    assert r.status is RunStatus.COMPLETED
    assert not any(e.type == "barrier.sell_failures" for e in r.events)


# ---------- write-ahead ----------


async def test_leg_committed_submitting_before_place_order(sm: SM) -> None:
    """At the moment place_order runs, the DB already says SUBMITTING (D9 step 1)."""
    seen: list[LegStatus] = []

    class Spy(PaperBroker):
        async def place_order(self, s: BrokerSession, intent: Any) -> str:
            async with sm() as db:
                leg = await db.get(Leg, intent.leg_id)
                assert leg is not None
                seen.append(leg.status)
            return await super().place_order(s, intent)

    broker = Spy()
    session = await broker.create_session({})
    body = ExecuteRequest.model_validate(
        {"session_id": str(uuid4()), "mode": "FIRST_TIME", "instructions": [buy("TCS", 1)]}
    )
    run_id = str(uuid4())
    await repo.create_run(
        sm,
        run_id=run_id,
        user_id="u",
        idempotency_key="k",
        request_hash="h",
        session_id=str(body.session_id),
        mode=body.mode,
        options=body.options.model_dump(mode="json"),
        legs=plan(body, run_id),
        now=datetime.now(UTC),
    )
    clock = FakeClock()
    clock.watch(sm)
    status = await Executor(sm, broker, session, LimiterRegistry(clock), clock=clock).run(run_id)
    assert seen == [LegStatus.SUBMITTING] and status is RunStatus.COMPLETED
