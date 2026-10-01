"""Outbox delivery worker (SPEC §6): at-least-once, backoff 1,2,4,8,16s, then dead-letter.

Delivery never touches run status; the result stays retrievable via GET /v1/executions/{id}.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalpi_engine.notify.sign import SIGNATURE_HEADER, sign
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Outbox

log = logging.getLogger(__name__)

BACKOFF_S = (1, 2, 4, 8, 16)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Notifier:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        *,
        secret: str,
        wall: Callable[[], datetime] = _utcnow,
        timeout_s: float = 5.0,
    ) -> None:
        self.sm, self.client, self.secret, self.wall = sm, client, secret, wall
        self.timeout_s = timeout_s

    async def deliver_due(self) -> int:
        """Try every due row once; returns how many were delivered."""
        async with self.sm() as s:
            due = await repo.due_outbox(s, self.wall())
        delivered = 0
        for row in due:
            ok = await self._send(row)
            async with self.sm.begin() as s:
                if ok:
                    await repo.mark_outbox(s, row.id, "SENT")
                    delivered += 1
                elif row.attempts + 1 > len(BACKOFF_S):
                    await repo.mark_outbox(s, row.id, "DEAD")
                    log.error("webhook dead-lettered outbox=%s run=%s", row.id, row.run_id)
                else:
                    nxt = self.wall() + timedelta(seconds=BACKOFF_S[row.attempts])
                    await repo.mark_outbox(s, row.id, "PENDING", next_attempt_at=nxt)
        return delivered

    async def _send(self, row: Outbox) -> bool:
        p = row.payload_json
        if not row.url:
            log.info(
                "notification (console) %s run=%s status=%s seq=%s summary=%s",
                p.get("event"),
                row.run_id,
                p.get("status"),
                p.get("seq"),
                p.get("summary"),
            )
            return True
        body = json.dumps(p, separators=(",", ":"), sort_keys=True).encode()
        headers = {"content-type": "application/json", SIGNATURE_HEADER: sign(self.secret, body)}
        try:
            resp = await self.client.post(
                row.url, content=body, headers=headers, timeout=self.timeout_s
            )
        except httpx.HTTPError as e:
            log.warning(
                "webhook outbox=%s attempt %d failed: %s",
                row.id,
                row.attempts + 1,
                type(e).__name__,
            )
            return False
        if resp.is_success:
            return True
        log.warning(
            "webhook outbox=%s attempt %d got HTTP %d", row.id, row.attempts + 1, resp.status_code
        )
        return False

    async def run_forever(self, stop: asyncio.Event, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            try:
                await self.deliver_due()
            except Exception:
                log.exception("notifier sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), interval_s)
            except TimeoutError:
                pass
