"""Outbox delivery (SPEC §6): at-least-once, backoff 1,2,4,8,16s, then dead-letter."""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.paper import PaperBroker
from kalpi_engine.domain.enums import Mode, RunStatus
from kalpi_engine.domain.schemas import ExecuteRequest
from kalpi_engine.execution.executor import ExecConfig, Executor
from kalpi_engine.execution.limits import LimiterRegistry
from kalpi_engine.notify.sign import SIGNATURE_HEADER, verify
from kalpi_engine.notify.worker import BACKOFF_S, Notifier
from kalpi_engine.planner import plan
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import create_all, make_engine, make_sessionmaker
from tests.engine.fakes import FakeClock

SM = async_sessionmaker[AsyncSession]
URL = "http://consumer.test/hook"
SECRET = "hooksecret"


@pytest.fixture
async def sm(tmp_path: Path) -> AsyncIterator[SM]:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'n.db'}")
    await create_all(eng)
    yield make_sessionmaker(eng)
    await eng.dispose()


class Consumer:
    def __init__(self) -> None:
        self.up = True
        self.received: list[httpx.Request] = []
        self.attempts = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if not self.up:
            return httpx.Response(503)
        self.received.append(req)
        return httpx.Response(200)


class Wall:
    def __init__(self) -> None:
        self.t = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, s: float) -> None:
        self.t += timedelta(seconds=s)


async def run_once(sm: SM, webhook_url: str | None, wall: Wall) -> str:
    """Execute a real 1-leg run on Paper; finalisation writes the outbox row."""
    broker = PaperBroker()
    session = await broker.create_session({})
    body = ExecuteRequest.model_validate(
        {
            "session_id": str(uuid4()),
            "mode": "FIRST_TIME",
            "options": {"webhook_url": webhook_url},
            "instructions": [{"symbol": "TCS", "action": "BUY", "quantity": 1}],
        }
    )
    run_id = str(uuid4())
    await repo.create_run(
        sm,
        run_id=run_id,
        user_id="u",
        idempotency_key=run_id,
        request_hash="h",
        session_id=str(body.session_id),
        mode=Mode.FIRST_TIME,
        options=body.options.model_dump(mode="json"),
        legs=plan(body, run_id),
        now=wall(),
    )
    clock = FakeClock()
    ex = Executor(
        sm, broker, session, LimiterRegistry(clock), clock=clock, wall=wall, config=ExecConfig()
    )
    assert await ex.run(run_id) is RunStatus.COMPLETED
    return run_id


def notifier(sm: SM, consumer: Consumer, wall: Wall) -> Notifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(consumer.handler))
    return Notifier(sm, client, secret=SECRET, wall=wall)


async def test_finalise_writes_one_outbox_row_with_latest_seq(sm: SM) -> None:
    wall = Wall()
    run_id = await run_once(sm, URL, wall)
    async with sm() as s:
        rows = await repo.get_outbox(s, run_id)
        events = await repo.get_events(s, run_id)
    assert len(rows) == 1 and rows[0].url == URL and rows[0].status == "PENDING"
    p = rows[0].payload_json
    assert p["event"] == "execution.completed" and p["status"] == "COMPLETED"
    assert p["seq"] == events[-1].seq and p["broker"] == "paper"


async def test_delivered_signed_exactly_once(sm: SM) -> None:
    wall = Wall()
    run_id = await run_once(sm, URL, wall)
    consumer = Consumer()
    n = notifier(sm, consumer, wall)
    assert await n.deliver_due() == 1
    assert await n.deliver_due() == 0  # nothing re-sent
    (req,) = consumer.received
    assert verify(SECRET, req.content, req.headers[SIGNATURE_HEADER])
    assert json.loads(req.content)["run_id"] == run_id
    async with sm() as s:
        row = (await repo.get_outbox(s, run_id))[0]
    assert row.status == "SENT" and row.attempts == 1


async def test_consumer_down_retries_with_backoff_then_dead_letter(sm: SM) -> None:
    wall = Wall()
    run_id = await run_once(sm, URL, wall)
    consumer = Consumer()
    consumer.up = False
    n = notifier(sm, consumer, wall)
    assert BACKOFF_S == (1, 2, 4, 8, 16)
    await n.deliver_due()  # first try fails
    for delay in BACKOFF_S:
        wall.advance(delay - 0.5)
        await n.deliver_due()
        assert consumer.attempts == BACKOFF_S.index(delay) + 1  # not due yet
        wall.advance(0.5)
        await n.deliver_due()
    assert consumer.attempts == 6  # 1 + 5 retries
    async with sm() as s:
        row = (await repo.get_outbox(s, run_id))[0]
        run = await repo.get_run(s, run_id)
    assert row.status == "DEAD" and row.attempts == 6
    assert run is not None and run.status is RunStatus.COMPLETED  # untouched
    wall.advance(3600)
    await n.deliver_due()
    assert consumer.attempts == 6  # dead letters are never retried


async def test_consumer_back_delivered_exactly_once(sm: SM) -> None:
    wall = Wall()
    run_id = await run_once(sm, URL, wall)
    consumer = Consumer()
    consumer.up = False
    n = notifier(sm, consumer, wall)
    await n.deliver_due()
    wall.advance(1)
    await n.deliver_due()  # 2 failures
    consumer.up = True
    wall.advance(2)
    await n.deliver_due()
    for _ in range(5):
        wall.advance(60)
        await n.deliver_due()
    assert consumer.attempts == 3 and len(consumer.received) == 1
    async with sm() as s:
        row = (await repo.get_outbox(s, run_id))[0]
    assert row.status == "SENT"


async def test_transport_error_counts_as_failure(sm: SM) -> None:
    wall = Wall()
    run_id = await run_once(sm, URL, wall)

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    n = Notifier(
        sm, httpx.AsyncClient(transport=httpx.MockTransport(boom)), secret=SECRET, wall=wall
    )
    await n.deliver_due()
    async with sm() as s:
        row = (await repo.get_outbox(s, run_id))[0]
    assert (
        row.status == "PENDING"
        and row.attempts == 1
        and row.next_attempt_at == wall() + timedelta(seconds=1)
    )


async def test_no_url_goes_to_console(sm: SM, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    wall = Wall()
    run_id = await run_once(sm, None, wall)
    consumer = Consumer()
    assert await notifier(sm, consumer, wall).deliver_due() == 1
    assert consumer.attempts == 0
    assert run_id in caplog.text and "execution.completed" in caplog.text
    async with sm() as s:
        assert (await repo.get_outbox(s, run_id))[0].status == "SENT"
