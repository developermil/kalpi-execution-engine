"""Persistence (SPEC §7, §5; D20 lease, D27 idempotency, D30 fencing)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from kalpi_engine.domain.enums import Exchange, LegStatus, Mode, OrderType, Phase, RunStatus, Side
from kalpi_engine.domain.models import OrderIntent
from kalpi_engine.domain.tags import make_tag
from kalpi_engine.storage import repo
from kalpi_engine.storage.crypto import TokenCipher
from kalpi_engine.storage.db import Leg, create_all, make_engine, make_sessionmaker

NOW = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)
TTL = 30


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await create_all(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
def sm(engine: AsyncEngine) -> async_sessionmaker:  # type: ignore[type-arg]
    return make_sessionmaker(engine)


def legs_for(run_id: str) -> list[OrderIntent]:
    spec = [(Phase.SELL, "TCS", Side.SELL, 5), (Phase.BUY, "ITC", Side.BUY, 10)]
    return [
        OrderIntent(
            leg_id=str(i),
            phase=ph,
            symbol=sym,
            exchange=Exchange.NSE,
            side=side,
            quantity=q,
            order_type=OrderType.MARKET,
            tag=make_tag(run_id, i),
        )
        for i, (ph, sym, side, q) in enumerate(spec)
    ]


async def new_run(sm: async_sessionmaker, key: str = "k1", h: str = "h1") -> repo.CreatedRun:  # type: ignore[type-arg]
    run_id = str(uuid4())
    return await repo.create_run(
        sm,
        run_id=run_id,
        user_id="u1",
        idempotency_key=key,
        request_hash=h,
        session_id=str(uuid4()),
        mode=Mode.REBALANCE,
        options={"poll_timeout_s": 60},
        legs=legs_for(run_id),
        now=NOW,
    )


# ---------- idempotency ----------


async def test_create_run_inserts_run_legs_and_event(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    c = await new_run(sm)
    assert c.created and c.run.status == RunStatus.CREATED
    async with sm() as s:
        legs = await repo.get_legs(s, c.run.id)
        events = await repo.get_events(s, c.run.id)
    assert [(lg.idx, lg.status, lg.phase) for lg in legs] == [
        (0, LegStatus.PLANNED, Phase.SELL),
        (1, LegStatus.PLANNED, Phase.BUY),
    ]
    assert [(e.seq, e.type) for e in events] == [(1, "run.created")]


async def test_duplicate_key_same_hash_returns_existing_run(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    first = await new_run(sm)
    second = await new_run(sm)
    assert not second.created and second.run.id == first.run.id
    async with sm() as s:
        assert len(await repo.get_legs(s, first.run.id)) == 2


async def test_duplicate_key_different_hash_rejected(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    await new_run(sm)
    with pytest.raises(repo.IdempotencyKeyReused):
        await new_run(sm, h="other")


async def test_concurrent_duplicate_inserts_yield_one_run(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    a, b = await asyncio.gather(new_run(sm), new_run(sm))
    assert a.run.id == b.run.id and sorted([a.created, b.created]) == [False, True]


# ---------- lease (D20) ----------


async def test_two_concurrent_claims_exactly_one_wins(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    run_id = (await new_run(sm)).run.id

    async def claim(owner: str) -> bool:
        async with sm.begin() as s:
            return await repo.claim_lease(s, run_id, owner, NOW, TTL)

    results = await asyncio.gather(claim("w1"), claim("w2"))
    assert sorted(results) == [False, True]


async def test_expired_lease_can_be_claimed(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    run_id = (await new_run(sm)).run.id
    async with sm.begin() as s:
        assert await repo.claim_lease(s, run_id, "w1", NOW, TTL)
    async with sm.begin() as s:
        assert not await repo.claim_lease(s, run_id, "w2", NOW + timedelta(seconds=TTL - 1), TTL)
    async with sm.begin() as s:
        assert await repo.claim_lease(s, run_id, "w2", NOW + timedelta(seconds=TTL + 1), TTL)


async def test_renew_by_non_owner_affects_zero_rows(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    run_id = (await new_run(sm)).run.id
    async with sm.begin() as s:
        await repo.claim_lease(s, run_id, "w1", NOW, TTL)
        assert not await repo.renew_lease(s, run_id, "w2", NOW, TTL)
        assert await repo.renew_lease(s, run_id, "w1", NOW + timedelta(seconds=10), TTL)
        run = await repo.get_run(s, run_id)
    assert run is not None and run.lease_owner == "w1"


# ---------- fencing (D30) and CAS ----------


async def test_fenced_leg_update_by_former_owner_affects_zero_rows(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    run_id = (await new_run(sm)).run.id
    later = NOW + timedelta(seconds=TTL + 1)
    async with sm.begin() as s:
        await repo.claim_lease(s, run_id, "w1", NOW, TTL)
        await repo.claim_lease(s, run_id, "w2", later, TTL)  # w1 expired, w2 took over
        leg = (await repo.get_legs(s, run_id))[0]
        old = repo.Fence(run_id, "w1", later)
        assert not await repo.cas_leg(
            s, leg.id, LegStatus.PLANNED, old, status=LegStatus.SUBMITTING
        )
        assert not await repo.update_run(s, old, status=RunStatus.RUNNING)
        new = repo.Fence(run_id, "w2", later)
        assert await repo.cas_leg(s, leg.id, LegStatus.PLANNED, new, status=LegStatus.SUBMITTING)
        assert await repo.update_run(s, new, status=RunStatus.RUNNING)


async def test_fence_fails_after_own_lease_expired(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    run_id = (await new_run(sm)).run.id
    async with sm.begin() as s:
        await repo.claim_lease(s, run_id, "w1", NOW, TTL)
        leg = (await repo.get_legs(s, run_id))[0]
        late = repo.Fence(run_id, "w1", NOW + timedelta(seconds=TTL + 1))
        assert not await repo.cas_leg(
            s, leg.id, LegStatus.PLANNED, late, status=LegStatus.SUBMITTING
        )


async def test_second_cas_on_same_status_affects_zero_rows(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    run_id = (await new_run(sm)).run.id
    async with sm.begin() as s:
        await repo.claim_lease(s, run_id, "w1", NOW, TTL)
        fence = repo.Fence(run_id, "w1", NOW)
        leg = (await repo.get_legs(s, run_id))[0]
        assert await repo.cas_leg(s, leg.id, LegStatus.PLANNED, fence, status=LegStatus.SUBMITTING)
        assert not await repo.cas_leg(
            s, leg.id, LegStatus.PLANNED, fence, status=LegStatus.SUBMITTING
        )
        got = await s.scalar(select(Leg.status).where(Leg.id == leg.id))
    assert got == LegStatus.SUBMITTING


async def test_unfenced_cas_for_rechecks(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    run_id = (await new_run(sm)).run.id
    async with sm.begin() as s:
        leg = (await repo.get_legs(s, run_id))[0]
        assert await repo.cas_leg(s, leg.id, LegStatus.PLANNED, None, status=LegStatus.UNKNOWN)
        assert not await repo.cas_leg(s, leg.id, LegStatus.PLANNED, None, status=LegStatus.FILLED)


# ---------- events ----------


async def test_event_seq_is_1_2_3_per_run(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    a = (await new_run(sm, key="a")).run.id
    b = (await new_run(sm, key="b")).run.id
    async with sm.begin() as s:
        seqs = [await repo.append_event(s, a, "leg.submitted", {"i": i}, NOW) for i in range(3)]
        seqs_b = [await repo.append_event(s, b, "x", {}, NOW)]
    assert seqs == [2, 3, 4] and seqs_b == [2]  # seq 1 is run.created
    async with sm() as s:
        assert [e.seq for e in await repo.get_events(s, a)] == [1, 2, 3, 4]


# ---------- outbox ----------


async def test_outbox_due_and_mark(sm: async_sessionmaker) -> None:  # type: ignore[type-arg]
    run_id = (await new_run(sm)).run.id
    async with sm.begin() as s:
        ob = await repo.add_outbox(
            s, run_id, "http://x/hook", {"event": "execution.completed"}, NOW
        )
    async with sm.begin() as s:
        due = await repo.due_outbox(s, NOW)
        assert [o.id for o in due] == [ob]
        await repo.mark_outbox(s, ob, "PENDING", next_attempt_at=NOW + timedelta(seconds=10))
        assert await repo.due_outbox(s, NOW) == []
        await repo.mark_outbox(s, ob, "SENT")
        assert await repo.due_outbox(s, NOW + timedelta(hours=1)) == []
        row = (await repo.get_outbox(s, run_id))[0]
    assert row.attempts == 2 and row.status == "SENT"


# ---------- encryption ----------


def test_token_round_trips_and_ciphertext_differs() -> None:
    c = TokenCipher(Fernet.generate_key().decode())
    token = "eyJhbGciOi.secret-access-token"
    enc = c.encrypt(token)
    assert enc != token and token not in enc
    assert c.decrypt(enc) == token


def test_cipher_requires_key() -> None:
    with pytest.raises(ValueError, match="FERNET_KEY"):
        TokenCipher("")


async def test_broker_session_stored_encrypted_and_not_logged(
    sm: async_sessionmaker,  # type: ignore[type-arg]
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    cipher = TokenCipher(Fernet.generate_key().decode())
    token = "SUPER-SECRET-TOKEN-123"
    async with sm.begin() as s:
        sid = await repo.save_broker_session(
            s, cipher, "u1", "paper", token, NOW + timedelta(hours=8), {"client": "AB1"}
        )
    async with sm() as s:
        row, plain = await repo.load_broker_session(s, cipher, sid)  # type: ignore[misc]
    assert plain == token and row.token_enc != token and row.meta_json == {"client": "AB1"}
    assert sid in caplog.text  # something was logged...
    assert token not in caplog.text  # ...but never the token
    async with sm() as s:
        assert await repo.load_broker_session(s, cipher, "missing") is None
