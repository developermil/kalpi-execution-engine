"""Run finalisation (SPEC §5 step 8): final status, run.finished event and the outbox row,
all in one lease-fenced transaction, so a run never ends without its notification.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.domain.enums import RunStatus
from kalpi_engine.domain.status import run_status
from kalpi_engine.notify.payload import build_payload
from kalpi_engine.storage import repo
from kalpi_engine.storage.repo import Fence


class _Abort(Exception):
    pass


async def write_final(
    sm: async_sessionmaker[AsyncSession],
    fence: Fence,
    *,
    broker_id: str,
    default_webhook_url: str | None,
    now: datetime,
) -> RunStatus | None:
    """Returns the final status, or None if the lease was lost (nothing written)."""
    run_id = fence.run_id
    try:
        async with sm.begin() as s:
            legs = await repo.get_legs(s, run_id)
            status = run_status(lg.status for lg in legs)
            if not await repo.update_run(s, fence, status=status, finished_at=now):
                raise _Abort  # roll back: another worker owns the run
            counts: dict[str, int] = {}
            for lg in legs:
                counts[lg.status.value] = counts.get(lg.status.value, 0) + 1
            seq = await repo.append_event(
                s, run_id, "run.finished", {"status": status.value, "legs": counts}, now
            )
            run = await repo.get_run(s, run_id)
            assert run is not None
            payload = build_payload(
                run, legs, broker=broker_id, event="execution.completed", seq=seq
            )
            url = run.options_json.get("webhook_url") or default_webhook_url or None
            await repo.add_outbox(s, run_id, url, payload, now)
    except _Abort:
        return None
    return status
