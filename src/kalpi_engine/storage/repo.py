"""Repository functions (SPEC §5, §7). Portable SQL; every guard is a conditional UPDATE + rowcount.

Functions taking an AsyncSession run inside the caller's transaction; create_run owns its own.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, Select, and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.domain.enums import TERMINAL_RUN_STATUSES, LegStatus, Mode, RunStatus
from kalpi_engine.domain.models import OrderIntent
from kalpi_engine.storage.crypto import TokenCipher
from kalpi_engine.storage.db import BrokerSessionRow, Event, Leg, Outbox, Run

log = logging.getLogger(__name__)


class IdempotencyKeyReused(Exception):
    """Same (user_id, Idempotency-Key) with a different request body (D27)."""

    code = "IDEMPOTENCY_KEY_REUSED"


@dataclass(frozen=True)
class CreatedRun:
    run: Run
    created: bool


@dataclass(frozen=True)
class Fence:
    """Proof of lease ownership at `now`; every write while executing carries one (D30)."""

    run_id: str
    owner: str
    now: datetime


async def _rowcount(s: AsyncSession, stmt: Any) -> int:
    res = await s.execute(stmt.execution_options(synchronize_session=False))
    return cast(CursorResult[Any], res).rowcount


def _fresh[S: Select[Any]](stmt: S) -> S:
    """Guarded UPDATEs bypass the identity map, so reads must overwrite cached objects."""
    return stmt.execution_options(populate_existing=True)


# ---------- runs ----------


async def get_run(s: AsyncSession, run_id: str) -> Run | None:
    return await s.get(Run, run_id, populate_existing=True)


async def get_run_by_key(s: AsyncSession, user_id: str, key: str) -> Run | None:
    stmt = select(Run).where(Run.user_id == user_id, Run.idempotency_key == key)
    return await s.scalar(_fresh(stmt))


def _existing(run: Run, request_hash: str) -> CreatedRun:
    if run.request_hash != request_hash:
        raise IdempotencyKeyReused(f"Idempotency-Key reused with a different body (run {run.id})")
    return CreatedRun(run, created=False)


async def create_run(
    sm: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
    user_id: str,
    idempotency_key: str,
    request_hash: str,
    session_id: str,
    mode: Mode,
    options: dict[str, Any],
    legs: list[OrderIntent],
    now: datetime,
) -> CreatedRun:
    """Insert run + PLANNED legs + run.created event atomically; duplicates return the original."""
    try:
        async with sm.begin() as s:
            found = await get_run_by_key(s, user_id, idempotency_key)
            if found is not None:
                return _existing(found, request_hash)
            run = Run(
                id=run_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                session_id=session_id,
                mode=mode,
                status=RunStatus.CREATED,
                options_json=options,
                created_at=now,
            )
            s.add(run)
            await s.flush()
            s.add_all(
                Leg(
                    id=str(uuid4()),
                    run_id=run_id,
                    idx=int(i.leg_id),
                    phase=i.phase,
                    symbol=i.symbol,
                    exchange=i.exchange,
                    side=i.side,
                    qty=i.quantity,
                    order_type=i.order_type,
                    limit_price=i.limit_price,
                    tag=i.tag,
                    status=LegStatus.PLANNED,
                    filled_qty=0,
                )
                for i in legs
            )
            await append_event(s, run_id, "run.created", {"legs": len(legs)}, now)
            return CreatedRun(run, created=True)
    except IntegrityError:
        # A racing request with the same key won the unique constraint: fall back to lookup.
        async with sm() as s:
            found = await get_run_by_key(s, user_id, idempotency_key)
        if found is None:
            raise
        return _existing(found, request_hash)


async def claim_lease(s: AsyncSession, run_id: str, owner: str, now: datetime, ttl_s: int) -> bool:
    stmt = (
        update(Run)
        .where(Run.id == run_id, or_(Run.lease_until.is_(None), Run.lease_until < now))
        .values(lease_owner=owner, lease_until=now + timedelta(seconds=ttl_s))
    )
    return await _rowcount(s, stmt) == 1


async def renew_lease(s: AsyncSession, run_id: str, owner: str, now: datetime, ttl_s: int) -> bool:
    stmt = (
        update(Run)
        .where(Run.id == run_id, Run.lease_owner == owner, Run.lease_until >= now)
        .values(lease_until=now + timedelta(seconds=ttl_s))
    )
    return await _rowcount(s, stmt) == 1


async def update_run(s: AsyncSession, fence: Fence, **values: Any) -> bool:
    stmt = (
        update(Run)
        .where(
            Run.id == fence.run_id,
            Run.lease_owner == fence.owner,
            Run.lease_until >= fence.now,
        )
        .values(**values)
    )
    return await _rowcount(s, stmt) == 1


async def lease_held(s: AsyncSession, fence: Fence) -> bool:
    stmt = select(Run.id).where(
        Run.id == fence.run_id, Run.lease_owner == fence.owner, Run.lease_until >= fence.now
    )
    return await s.scalar(stmt) is not None


async def resumable_runs(s: AsyncSession, now: datetime) -> list[Run]:
    """Non-terminal runs nobody holds a live lease on (D30 resume sweep)."""
    stmt = _fresh(
        select(Run)
        .where(
            Run.status.in_((RunStatus.CREATED, RunStatus.RUNNING)),
            or_(Run.lease_until.is_(None), Run.lease_until < now),
        )
        .order_by(Run.created_at)
    )
    return list(await s.scalars(stmt))


async def set_final_status(s: AsyncSession, run_id: str, status: RunStatus) -> bool:
    """Post-finalise recompute by a recheck (§5.8): only touches runs that are already final."""
    stmt = (
        update(Run)
        .where(Run.id == run_id, Run.status.in_(TERMINAL_RUN_STATUSES))
        .values(status=status)
    )
    return await _rowcount(s, stmt) == 1


# ---------- legs ----------


async def get_legs(s: AsyncSession, run_id: str) -> list[Leg]:
    return list(await s.scalars(_fresh(select(Leg).where(Leg.run_id == run_id).order_by(Leg.idx))))


async def cas_leg(
    s: AsyncSession, leg_id: str, expected: LegStatus, fence: Fence | None, **values: Any
) -> bool:
    """UPDATE ... WHERE status=:expected [AND lease held]. fence=None only for rechecks (§5.8)."""
    conds = [Leg.id == leg_id, Leg.status == expected]
    if fence is not None:
        conds.append(Leg.run_id == fence.run_id)
        conds.append(
            exists().where(
                and_(
                    Run.id == fence.run_id,
                    Run.lease_owner == fence.owner,
                    Run.lease_until >= fence.now,
                )
            )
        )
    return await _rowcount(s, update(Leg).where(*conds).values(**values)) == 1


async def due_rechecks(
    s: AsyncSession, run_id: str, now: datetime, statuses: Sequence[LegStatus]
) -> list[Leg]:
    stmt = _fresh(
        select(Leg)
        .where(Leg.run_id == run_id, Leg.recheck_at <= now, Leg.status.in_(statuses))
        .order_by(Leg.idx)
    )
    return list(await s.scalars(stmt))


async def runs_with_due_rechecks(s: AsyncSession, now: datetime) -> list[str]:
    stmt = select(Leg.run_id).where(Leg.recheck_at <= now).distinct()
    return list(await s.scalars(stmt))


# ---------- events ----------


async def append_event(
    s: AsyncSession,
    run_id: str,
    type_: str,
    payload: dict[str, Any],
    now: datetime,
    leg_id: str | None = None,
) -> int:
    """Next per-run seq; UNIQUE(run_id, seq) rejects a racing writer."""
    last = await s.scalar(select(func.max(Event.seq)).where(Event.run_id == run_id))
    seq = (last or 0) + 1
    s.add(Event(run_id=run_id, seq=seq, leg_id=leg_id, ts=now, type=type_, payload_json=payload))
    await s.flush()
    return seq


async def get_events(s: AsyncSession, run_id: str) -> list[Event]:
    return list(await s.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.seq)))


# ---------- outbox ----------


async def add_outbox(
    s: AsyncSession, run_id: str, url: str | None, payload: dict[str, Any], now: datetime
) -> int:
    row = Outbox(run_id=run_id, url=url, payload_json=payload, attempts=0, next_attempt_at=now)
    s.add(row)
    await s.flush()
    return row.id


async def due_outbox(s: AsyncSession, now: datetime, limit: int = 20) -> list[Outbox]:
    stmt = _fresh(
        select(Outbox)
        .where(Outbox.status == "PENDING", Outbox.next_attempt_at <= now)
        .order_by(Outbox.id)
        .limit(limit)
    )
    return list(await s.scalars(stmt))


async def mark_outbox(
    s: AsyncSession, outbox_id: int, status: str, next_attempt_at: datetime | None = None
) -> None:
    values: dict[str, Any] = {"status": status, "attempts": Outbox.attempts + 1}
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    await _rowcount(s, update(Outbox).where(Outbox.id == outbox_id).values(**values))


async def get_outbox(s: AsyncSession, run_id: str) -> list[Outbox]:
    return list(await s.scalars(_fresh(select(Outbox).where(Outbox.run_id == run_id))))


# ---------- broker sessions ----------


async def save_broker_session(
    s: AsyncSession,
    cipher: TokenCipher,
    user_id: str,
    broker: str,
    token: str,
    expires_at: datetime | None,
    meta: dict[str, Any],
) -> str:
    sid = str(uuid4())
    s.add(
        BrokerSessionRow(
            id=sid,
            user_id=user_id,
            broker=broker,
            token_enc=cipher.encrypt(token),
            expires_at=expires_at,
            meta_json=meta,
        )
    )
    await s.flush()
    log.info("broker session saved id=%s user=%s broker=%s", sid, user_id, broker)
    return sid


async def load_broker_session(
    s: AsyncSession, cipher: TokenCipher, session_id: str
) -> tuple[BrokerSessionRow, str] | None:
    row = await s.get(BrokerSessionRow, session_id)
    if row is None:
        return None
    return row, cipher.decrypt(row.token_enc)
