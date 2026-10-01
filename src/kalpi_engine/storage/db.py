"""SQLAlchemy 2 async models for SPEC §7. Portable across SQLite and Postgres."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Dialect,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from kalpi_engine.domain.enums import (
    Exchange,
    LegStatus,
    Mode,
    OrderType,
    Phase,
    RunStatus,
    Side,
)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC (SQLite drops tz); always returns aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; pass an aware UTC datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def _enum(cls: type[Any]) -> Enum:
    return Enum(cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    pass


class BrokerSessionRow(Base):
    __tablename__ = "broker_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    broker: Mapped[str] = mapped_column(String(32))
    token_enc: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(36))
    mode: Mapped[Mode] = mapped_column(_enum(Mode))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), index=True)
    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Leg(Base):
    __tablename__ = "legs"
    __table_args__ = (UniqueConstraint("run_id", "idx"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    phase: Mapped[Phase] = mapped_column(_enum(Phase))
    symbol: Mapped[str] = mapped_column(String(40))
    exchange: Mapped[Exchange] = mapped_column(_enum(Exchange))
    side: Mapped[Side] = mapped_column(_enum(Side))
    qty: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[OrderType] = mapped_column(_enum(OrderType))
    limit_price: Mapped[float | None] = mapped_column(Float)
    tag: Mapped[str] = mapped_column(String(20))
    status: Mapped[LegStatus] = mapped_column(_enum(LegStatus))
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_price: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(String)
    recheck_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)


class Event(Base):
    """Append-only audit log."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    leg_id: Mapped[str | None] = mapped_column(String(36))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Outbox(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    url: Mapped[str | None] = mapped_column(String)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")


def make_engine(url: str) -> AsyncEngine:
    u = make_url(url)
    connect_args: dict[str, Any] = {}
    if u.get_backend_name() == "sqlite":
        connect_args["timeout"] = 30  # wait on the write lock instead of failing
    return create_async_engine(url, connect_args=connect_args)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    u = engine.url
    if u.get_backend_name() == "sqlite" and u.database and u.database != ":memory:":
        Path(u.database).parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
