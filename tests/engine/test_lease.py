"""Run lease, fencing and crash-resume (SPEC §5 steps 3 and 9; D3, D20, D30)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.base import BrokerSession
from kalpi_engine.brokers.paper import Faults
from kalpi_engine.domain.enums import LegStatus, RunStatus
from kalpi_engine.domain.models import OrderIntent
from kalpi_engine.execution import executor as executor_mod
from kalpi_engine.execution.executor import Executor
from kalpi_engine.execution.resume import ResumeSweeper
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Run, create_all, make_engine, make_sessionmaker
from tests.engine.fakes import Harness

SM = async_sessionmaker[AsyncSession]
TTL = 30


@pytest.fixture
async def sm(tmp_path: Path) -> AsyncIterator[SM]:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
    await create_all(eng)
    yield make_sessionmaker(eng)
    await eng.dispose()


class Crash(BaseException):
    """Simulated process death: bypasses every `except Exception` in the engine."""


def buy(sym: str, q: int = 1) -> dict[str, Any]:
    return {"symbol": sym, "action": "BUY", "quantity": q}


def orders(h: Harness) -> int:
    return len(h.broker._orders)


def crash_on_place(h: Harness, *, after_broker_accepts: bool) -> None:
    original = h.broker.place_order

    async def place(s: BrokerSession, intent: OrderIntent) -> str:
        h.broker.place_order = original  # type: ignore[method-assign]  # crash once
        if after_broker_accepts:
            await original(s, intent)
        else:
            h.broker.place_calls += 1  # the request left the process; the broker never saw it
        raise Crash

    h.broker.place_order = place  # type: ignore[method-assign]


async def crashed_run(h: Harness, *, after_broker_accepts: bool) -> str:
    run_id = await h.start("FIRST_TIME", [buy("TCS")])
    crash_on_place(h, after_broker_accepts=after_broker_accepts)
    with pytest.raises(Crash):
        await h.executor(owner="w1", lease_ttl_s=TTL).run(run_id)
    _, legs, _, _ = await h.state(run_id)
    assert legs[0].status is LegStatus.SUBMITTING  # write-ahead survived the crash
    return run_id


# ---------- crash-resume ----------


async def test_crash_after_accept_resume_adopts_by_tag_no_duplicate(sm: SM) -> None:
    h = Harness(sm)
    run_id = await crashed_run(h, after_broker_accepts=True)
    assert await h.executor(owner="w2", lease_ttl_s=TTL).run(run_id) is None  # w1 still leased
    h.clock.advance(TTL + 1)
    assert await h.executor(owner="w2", lease_ttl_s=TTL).run(run_id) is RunStatus.COMPLETED
    run, legs, events, _ = await h.state(run_id)
    assert legs[0].status is LegStatus.FILLED and run.lease_owner == "w2"
    assert orders(h) == 1 and h.broker.place_calls == 1
    assert "leg.adopted" in [e.type for e in events] and "run.resumed" in [e.type for e in events]


async def test_resumed_submitting_with_tag_miss_is_unknown_never_resent(sm: SM) -> None:
    h = Harness(sm)
    run_id = await crashed_run(h, after_broker_accepts=False)
    h.clock.advance(TTL + 1)
    status = await h.executor(owner="w2", lease_ttl_s=TTL).run(run_id)
    _, legs, _, _ = await h.state(run_id)
    assert legs[0].status is LegStatus.UNKNOWN and legs[0].recheck_at is not None
    assert status is RunStatus.COMPLETED_WITH_FAILURES
    assert orders(h) == 0 and h.broker.place_calls == 1  # 0 resends


async def test_two_concurrent_resumers_one_lease_holder_no_duplicates(sm: SM) -> None:
    h = Harness(sm)
    run_id = await h.start("FIRST_TIME", [buy("TCS"), buy("ITC"), buy("SBIN")])
    a = h.executor(owner="a", lease_ttl_s=TTL)
    b = h.executor(owner="b", lease_ttl_s=TTL)
    results = await asyncio.gather(a.run(run_id), b.run(run_id))
    assert sorted(results, key=str) == sorted([RunStatus.COMPLETED, None], key=str)
    assert orders(h) == 3 and h.broker.place_calls == 3


async def test_old_owner_lease_expired_mid_place_its_writes_affect_nothing(sm: SM) -> None:
    h = Harness(sm)
    run_id = await h.start("FIRST_TIME", [buy("TCS")])
    a = h.executor(owner="a", lease_ttl_s=TTL)
    original = h.broker.place_order

    async def stalled_place(s: BrokerSession, intent: OrderIntent) -> str:
        order_id = await original(s, intent)
        assert a.lease is not None and a.lease.renewer is not None
        a.lease.renewer.cancel()  # process paused: no renewals
        h.clock.advance(TTL + 1)
        async with sm.begin() as db:  # a new worker takes over the expired lease
            assert await repo.claim_lease(db, run_id, "b", h.wall(), TTL)
        return order_id

    h.broker.place_order = stalled_place  # type: ignore[method-assign]
    n_events = len((await h.state(run_id))[2])
    assert await a.run(run_id) is None  # lease lost: no finalise
    run, legs, events, outbox = await h.state(run_id)
    assert legs[0].status is LegStatus.SUBMITTING and legs[0].broker_order_id is None
    assert run.status is RunStatus.RUNNING and run.lease_owner == "b"
    # a's only committed writes are from before the takeover
    assert [e.type for e in events[n_events:]] == ["run.running", "leg.submitting"]
    assert outbox == [] and orders(h) == 1


async def test_failed_renew_stops_further_place_order_calls(
    sm: SM, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(sm, Faults(fill_after_polls=25))  # leg 1 polls ~25s: spans a renew (TTL/3)
    run_id = await h.start("FIRST_TIME", [buy("TCS"), buy("ITC"), buy("SBIN")], poll_timeout_s=60)

    original_renew = repo.renew_lease

    async def renew_fails_once_trading(*args: Any, **kw: Any) -> bool:
        # e.g. DB unreachable mid-run: the lease row is untouched, only the renew fails
        if h.broker.place_calls >= 1:
            return False
        return await original_renew(*args, **kw)

    monkeypatch.setattr(executor_mod.repo, "renew_lease", renew_fails_once_trading)
    ex = h.executor(owner="a", lease_ttl_s=TTL, max_concurrency=1, poll_interval_s=1.0)
    assert await ex.run(run_id) is None
    assert h.broker.place_calls == 1
    _, legs, _, _ = await h.state(run_id)
    assert [lg.status for lg in legs][1:] == [LegStatus.PLANNED, LegStatus.PLANNED]


async def test_renewal_keeps_a_long_run_alive(sm: SM) -> None:
    h = Harness(sm, Faults(fill_after_polls=100))  # 100s of polling, TTL 30s
    run_id = await h.start("FIRST_TIME", [buy("TCS")], poll_timeout_s=200)
    assert await h.executor(owner="a", lease_ttl_s=TTL).run(run_id) is RunStatus.COMPLETED
    async with sm() as s:
        run = await s.get(Run, run_id)
    assert run is not None and run.lease_until is not None and run.lease_until > h.wall()


# ---------- periodic resume sweep ----------


async def test_restart_within_ttl_resumed_by_periodic_sweep_after_expiry(sm: SM) -> None:
    h = Harness(sm)
    run_id = await crashed_run(h, after_broker_accepts=True)

    held_until = (await h.state(run_id))[0].lease_until
    assert held_until is not None

    finished = asyncio.Event()

    async def factory(run: Run) -> Executor:
        ex = h.executor(owner="w2", lease_ttl_s=TTL)
        original_run = ex.run

        async def run_and_signal(rid: str) -> RunStatus | None:
            try:
                return await original_run(rid)
            finally:
                finished.set()

        ex.run = run_and_signal  # type: ignore[method-assign]
        return ex

    sweeper = ResumeSweeper(sm, factory, clock=h.clock, wall=h.wall, interval_s=TTL)
    assert await sweeper.sweep_once() == []  # restarted within TTL: lease still held by w1
    stop = asyncio.Event()
    loop = asyncio.create_task(sweeper.run_forever(stop))
    await asyncio.wait_for(finished.wait(), timeout=30)  # no DB polling: keeps fake time moving
    stop.set()
    await loop
    run, legs, events, _ = await h.state(run_id)
    assert run.lease_owner == "w2" and legs[0].status is LegStatus.FILLED
    resumed = next(e for e in events if e.type == "run.resumed")
    assert resumed.ts > held_until  # only after the old lease expired
    assert orders(h) == 1


async def test_sweep_ignores_finished_runs(sm: SM) -> None:
    h = Harness(sm)
    run_id = await h.start("FIRST_TIME", [buy("TCS")])
    assert await h.executor(owner="a", lease_ttl_s=TTL).run(run_id) is RunStatus.COMPLETED
    h.clock.advance(TTL + 1)

    async def factory(run: Run) -> Executor:
        raise AssertionError("finished runs are never resumed")

    sweeper = ResumeSweeper(sm, factory, clock=h.clock, wall=h.wall)
    assert await sweeper.sweep_once() == []


async def test_no_unfenced_run_updates_left() -> None:
    assert not hasattr(repo, "update_run_unfenced")
