"""Ambiguous submits, late reconcile, auth expiry (SPEC §5 steps 5, 6, 8; D9/D21/D29/D31/D32)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.base import BrokerSession
from kalpi_engine.brokers.paper import Faults
from kalpi_engine.domain.enums import LegStatus, OrderStatus, RunStatus
from kalpi_engine.domain.models import BrokerOrderState
from kalpi_engine.domain.status import run_status
from kalpi_engine.execution.reconcile import next_recheck
from kalpi_engine.storage.db import create_all, make_engine, make_sessionmaker
from tests.engine.fakes import T0, Harness

SM = async_sessionmaker[AsyncSession]


@pytest.fixture
async def sm(tmp_path: Path) -> AsyncIterator[SM]:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    await create_all(eng)
    yield make_sessionmaker(eng)
    await eng.dispose()


def buy(sym: str, q: int, **kw: Any) -> dict[str, Any]:
    return {"symbol": sym, "action": "BUY", "quantity": q, **kw}


def sell(sym: str, q: int, **kw: Any) -> dict[str, Any]:
    return {"symbol": sym, "action": "SELL", "quantity": q, **kw}


def orders(h: Harness) -> int:
    return len(h.broker._orders)


# ---------- ambiguous submit (D9) ----------


async def test_timeout_after_accept_is_adopted_by_tag_exactly_one_order(sm: SM) -> None:
    h = Harness(sm, Faults(timeout_after_accept_next=1))
    run_id = await h.start("FIRST_TIME", [buy("TCS", 1)])
    assert await h.executor().run(run_id) is RunStatus.COMPLETED
    _, legs, events, _ = await h.state(run_id)
    assert legs[0].status is LegStatus.FILLED and legs[0].broker_order_id
    assert orders(h) == 1 and h.broker.place_calls == 1
    assert "leg.adopted" in [e.type for e in events]


async def test_timeout_not_accepted_is_unknown_zero_resubmits(sm: SM) -> None:
    h = Harness(sm, Faults(timeout_before_accept_next=1))
    run_id = await h.start("FIRST_TIME", [buy("TCS", 1)])
    assert await h.executor().run(run_id) is RunStatus.COMPLETED_WITH_FAILURES
    _, legs, _, _ = await h.state(run_id)
    assert legs[0].status is LegStatus.UNKNOWN and legs[0].broker_order_id is None
    assert legs[0].recheck_at is not None
    assert orders(h) == 0 and h.broker.place_calls == 1
    # a later sweep finds nothing and never places
    h.clock.advance(61)
    await h.reconciler().sweep(run_id)
    assert orders(h) == 0 and h.broker.place_calls == 1


async def test_unknown_sell_is_a_sell_failure_at_barrier(sm: SM) -> None:
    h = Harness(sm, Faults(timeout_before_accept_next=1))
    run_id = await h.start(
        "REBALANCE", [sell("TCS", 5), buy("ITC", 1)], holdings={"TCS": 5}, halt_on_sell_failure=True
    )
    await h.executor().run(run_id)
    _, legs, events, _ = await h.state(run_id)
    assert {lg.symbol: lg.status for lg in legs} == {
        "TCS": LegStatus.UNKNOWN,
        "ITC": LegStatus.SKIPPED,
    }
    barrier = next(e for e in events if e.type == "barrier.sell_failures")
    assert barrier.payload_json["failed"] == [{"symbol": "TCS", "status": "UNKNOWN"}]


# ---------- late fills (D21, D31) ----------


async def test_unknown_adopted_by_sweep_at_t_plus_60_writes_execution_updated(sm: SM) -> None:
    # 3 reconcile lookups + 1 finalise re-check miss; the order exists and is FILLED.
    h = Harness(sm, Faults(timeout_after_accept_next=1, hide_from_tag_lookups=4))
    run_id = await h.start("FIRST_TIME", [buy("TCS", 1)])
    assert await h.executor().run(run_id) is RunStatus.COMPLETED_WITH_FAILURES
    _, legs, _, outbox = await h.state(run_id)
    assert legs[0].status is LegStatus.UNKNOWN and len(outbox) == 1
    first_seq = outbox[0].payload_json["seq"]

    await h.reconciler().sweep(run_id)  # not due yet
    assert (await h.state(run_id))[1][0].status is LegStatus.UNKNOWN

    h.clock.advance(60)
    assert await h.reconciler().sweep(run_id) == 1
    run, legs, events, outbox = await h.state(run_id)
    assert legs[0].status is LegStatus.FILLED and legs[0].recheck_at is None
    assert run.status is RunStatus.COMPLETED
    assert [o.payload_json["event"] for o in outbox] == ["execution.completed", "execution.updated"]
    updated = outbox[1].payload_json
    assert updated["seq"] > first_seq and updated["seq"] == events[-1].seq
    assert updated["status"] == "COMPLETED"
    assert orders(h) == 1 and h.broker.place_calls == 1


async def test_unknown_adopted_at_finalise(sm: SM) -> None:
    h = Harness(sm, Faults(timeout_after_accept_next=1, hide_from_tag_lookups=3))
    run_id = await h.start("FIRST_TIME", [buy("TCS", 1)])
    assert await h.executor().run(run_id) is RunStatus.COMPLETED
    run, legs, _, outbox = await h.state(run_id)
    assert legs[0].status is LegStatus.FILLED
    assert [o.payload_json["event"] for o in outbox] == ["execution.completed"]
    assert orders(h) == 1


async def test_sweep_and_finalise_racing_on_one_leg_exactly_one_update(sm: SM) -> None:
    h = Harness(sm, Faults(timeout_after_accept_next=1, hide_from_tag_lookups=4))
    run_id = await h.start("FIRST_TIME", [buy("TCS", 1)])
    await h.executor().run(run_id)
    h.clock.advance(60)
    _, legs, _, _ = await h.state(run_id)
    a, b = h.reconciler(), h.reconciler()
    results = await asyncio.gather(a.recheck_leg(legs[0]), b.recheck_leg(legs[0]))
    assert sorted(results) == [False, True]
    _, legs, events, outbox = await h.state(run_id)
    assert [e.type for e in events].count("leg.rechecked") == 1
    assert [o.payload_json["event"] for o in outbox].count("execution.updated") == 1
    assert legs[0].status is LegStatus.FILLED


async def test_open_leg_that_fills_later_is_updated_by_sweep(sm: SM) -> None:
    h = Harness(sm, Faults(fill_after_polls=1_000))
    run_id = await h.start("FIRST_TIME", [buy("TCS", 1)], poll_timeout_s=3)
    assert await h.executor().run(run_id) is RunStatus.COMPLETED_WITH_FAILURES
    _, legs, _, _ = await h.state(run_id)
    assert legs[0].status is LegStatus.OPEN
    h.broker.faults.fill_after_polls = 0
    h.clock.advance(60)
    assert await h.reconciler().sweep(run_id) == 1
    run, legs, _, outbox = await h.state(run_id)
    assert legs[0].status is LegStatus.FILLED and legs[0].filled_qty == 1
    assert (
        run.status is RunStatus.COMPLETED
        and outbox[-1].payload_json["event"] == "execution.updated"
    )


async def test_unchanged_leg_is_rescheduled_without_event(sm: SM) -> None:
    h = Harness(sm, Faults(fill_after_polls=1_000))
    run_id = await h.start("FIRST_TIME", [buy("TCS", 1)], poll_timeout_s=3)
    await h.executor().run(run_id)
    n_events = len((await h.state(run_id))[2])
    h.clock.advance(60)
    assert await h.reconciler().sweep(run_id) == 0
    _, legs, events, outbox = await h.state(run_id)
    assert legs[0].status is LegStatus.OPEN and legs[0].recheck_at is not None
    assert legs[0].recheck_at > h.wall() and len(events) == n_events and len(outbox) == 1


def test_recheck_stops_at_1535_ist() -> None:
    assert next_recheck(T0) == T0 + timedelta(seconds=60)  # 11:00 IST
    late = T0.replace(hour=10, minute=4, second=30)  # 15:34:30 IST
    assert next_recheck(late) is None
    assert next_recheck(T0.replace(hour=10, minute=4)) == T0.replace(hour=10, minute=5)  # 15:35 ok


# ---------- auth expiry (D32) ----------


async def test_auth_expiry_mid_run_mapping(sm: SM) -> None:
    h = Harness(sm, Faults(partial_fill={"ITC": 2}))
    run_id = await h.start(
        "FIRST_TIME",
        [
            buy("TCS", 1, order_type="LIMIT", limit_price=1.0),  # stays OPEN
            buy("ITC", 5),  # PARTIAL
            buy("SBIN", 1),  # PLANNED (waits for a slot)
            buy("WIPRO", 1),  # PLANNED
        ],
        poll_timeout_s=30,
    )
    # Expire only after the engine has *seen* TCS OPEN and ITC PARTIAL, so the D32 mapping
    # is tested from known leg states. TCS's polls wait for ITC: fake time would otherwise
    # race through TCS's poll budget while ITC is still in real DB I/O.
    original_get_order = h.broker.get_order
    itc_seen = asyncio.Event()

    async def get_order(s: BrokerSession, oid: str) -> BrokerOrderState:
        state = await original_get_order(s, oid)
        if state.status is OrderStatus.PARTIAL:
            itc_seen.set()
            h.broker.faults.expire_sessions = True  # next call by either leg -> AuthExpired
        else:
            await itc_seen.wait()
        return state

    h.broker.get_order = get_order  # type: ignore[method-assign]
    status = await h.executor(max_concurrency=2).run(run_id)
    run, legs, _, _ = await h.state(run_id)
    by = {lg.symbol: lg for lg in legs}
    assert by["TCS"].status is LegStatus.UNKNOWN and by["TCS"].reason == "AUTH_EXPIRED"
    assert by["ITC"].status is LegStatus.PARTIAL and by["ITC"].filled_qty == 2
    assert by["SBIN"].status is LegStatus.SKIPPED and by["SBIN"].reason == "AUTH_EXPIRED"
    assert by["WIPRO"].status is LegStatus.SKIPPED
    assert all(lg.recheck_at is None for lg in legs)  # rechecks need auth too
    assert status is run.status is run_status(lg.status for lg in legs)
    assert status is RunStatus.COMPLETED_WITH_FAILURES
    assert h.broker.place_calls == 2


async def test_auth_expiry_at_placement_skips_rest(sm: SM) -> None:
    h = Harness(sm, Faults(expire_sessions=False))
    run_id = await h.start("FIRST_TIME", [buy("TCS", 1), buy("ITC", 1)])
    h.broker.faults.expire_sessions = True
    status = await h.executor(max_concurrency=1).run(run_id)
    _, legs, _, _ = await h.state(run_id)
    assert {lg.symbol: lg.status for lg in legs} == {
        "TCS": LegStatus.FAILED,
        "ITC": LegStatus.SKIPPED,
    }
    assert status is RunStatus.FAILED and orders(h) == 0
