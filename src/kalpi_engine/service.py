"""Service layer between the API and the engine: broker sessions, executor factory, background
launch, and the resume / recheck sweeps that run for the app's lifetime (D30, SPEC §5.8-9).
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.brokers.registry import Registry
from kalpi_engine.config import Settings
from kalpi_engine.domain.enums import RunStatus
from kalpi_engine.execution.executor import ExecConfig, Executor
from kalpi_engine.execution.limits import LimiterRegistry, MonotonicClock
from kalpi_engine.execution.reconcile import Reconciler
from kalpi_engine.execution.resume import ResumeSweeper
from kalpi_engine.planner.validate import IST
from kalpi_engine.storage import repo
from kalpi_engine.storage.crypto import TokenCipher
from kalpi_engine.storage.db import BrokerSessionRow, Run

log = logging.getLogger(__name__)

SESSION_ROLLOVER_IST = time(6, 0)  # Indian broker tokens die overnight


def utcnow() -> datetime:
    return datetime.now(UTC)


def default_expiry(now: datetime) -> datetime:
    """Next 06:00 IST: used when an adapter does not report an expiry."""
    ist = now.astimezone(IST)
    cut = datetime.combine(ist.date(), SESSION_ROLLOVER_IST, tzinfo=IST)
    return (cut if cut > ist else cut + timedelta(days=1)).astimezone(UTC)


@dataclass(frozen=True)
class LoadedSession:
    row: BrokerSessionRow
    adapter: BrokerAdapter
    session: BrokerSession


class Runtime:
    def __init__(
        self, settings: Settings, registry: Registry, sm: async_sessionmaker[AsyncSession]
    ) -> None:
        self.settings, self.registry, self.sm = settings, registry, sm
        self.clock = MonotonicClock()
        self.wall = utcnow
        self.limiters = LimiterRegistry(self.clock)
        self.owner = f"w-{uuid4().hex[:12]}"
        self._cipher: TokenCipher | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def cipher(self) -> TokenCipher:
        if self._cipher is None:
            self._cipher = TokenCipher(self.settings.fernet_key.get_secret_value())
        return self._cipher

    # ---------- broker sessions ----------

    async def save_session(self, user_id: str, bs: BrokerSession) -> tuple[str, datetime]:
        """Token + extras are encrypted together; meta_json holds nothing secret."""
        expires_at = bs.expires_at or default_expiry(self.wall())
        blob = json.dumps({"access_token": bs.access_token.get_secret_value(), "extra": bs.extra})
        async with self.sm.begin() as s:
            sid = await repo.save_broker_session(
                s, self.cipher, user_id, bs.broker_id, blob, expires_at, {}
            )
        return sid, expires_at

    async def load_session(
        self, session_id: str, user_id: str | None = None
    ) -> LoadedSession | None:
        async with self.sm() as s:
            found = await repo.load_broker_session(s, self.cipher, session_id)
        if found is None:
            return None
        row, blob = found
        if (
            user_id is not None and row.user_id != user_id
        ) or row.broker not in self.registry.ids():
            return None
        data = json.loads(blob)
        bs = BrokerSession(
            broker_id=row.broker,
            access_token=SecretStr(data["access_token"]),
            expires_at=row.expires_at,
            extra=data.get("extra") or {},
        )
        return LoadedSession(row, self.registry.get(row.broker), bs)

    # ---------- execution ----------

    def exec_config(self) -> ExecConfig:
        st = self.settings
        return ExecConfig(
            poll_interval_s=st.poll_interval_s,
            max_concurrency=st.max_concurrency,
            default_webhook_url=st.default_webhook_url or None,
            lease_ttl_s=st.lease_ttl_s,
            owner=self.owner,
        )

    async def make_executor(self, run: Run) -> Executor | None:
        loaded = await self.load_session(run.session_id)
        if loaded is None:
            log.warning("run %s: broker session %s not found", run.id, run.session_id)
            return None
        return Executor(
            self.sm,
            loaded.adapter,
            loaded.session,
            self.limiters,
            clock=self.clock,
            config=self.exec_config(),
            wall=self.wall,
        )

    async def make_reconciler(self, run: Run) -> Reconciler | None:
        loaded = await self.load_session(run.session_id)
        if loaded is None:
            return None
        return Reconciler(
            self.sm,
            loaded.adapter,
            loaded.session,
            self.limiters,
            clock=self.clock,
            wall=self.wall,
            default_webhook_url=self.settings.default_webhook_url or None,
        )

    async def launch(self, run: Run) -> None:
        """Start the run in the background; the lease decides if we actually execute it."""
        ex = await self.make_executor(run)
        if ex is None:
            return
        self._track(asyncio.create_task(ex.run(run.id), name=f"run-{run.id}"))

    def _track(self, task: asyncio.Task[RunStatus | None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------- sweeps ----------

    async def recheck_once(self) -> int:
        async with self.sm() as s:
            run_ids = await repo.runs_with_due_rechecks(s, self.wall())
        changed = 0
        for run_id in run_ids:
            async with self.sm() as s:
                run = await repo.get_run(s, run_id)
            rec = None if run is None else await self.make_reconciler(run)
            if rec is not None:
                changed += await rec.sweep(run_id)
        return changed

    async def recheck_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.recheck_once()
            except Exception:
                log.exception("recheck sweep failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.settings.recheck_interval_s)

    def resume_sweeper(self) -> ResumeSweeper:
        return ResumeSweeper(
            self.sm,
            self.make_executor,
            clock=self.clock,
            wall=self.wall,
            interval_s=self.settings.lease_ttl_s,
        )

    async def shutdown(self) -> None:
        """Cancel in-flight runs; their leases expire and the next process resumes them."""
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
