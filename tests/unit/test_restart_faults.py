"""B7 / I8: runs stranded by a crash are resumed by the lifespan sweeps, never resent.

Each seed strands a 12-leg run with a random mix of legs: accepted by the broker but recorded
only as SUBMITTING, SUBMITTING with nothing sent, and still PLANNED.
"""

import asyncio
import random
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from kalpi_engine.brokers.paper import PRICES
from kalpi_engine.brokers.registry import Registry
from kalpi_engine.config import Settings
from kalpi_engine.domain.enums import LegStatus, RunStatus
from kalpi_engine.domain.schemas import ExecuteRequest
from kalpi_engine.main import create_app
from kalpi_engine.planner import plan
from kalpi_engine.service import Runtime
from kalpi_engine.storage import repo
from kalpi_engine.storage.db import Leg, Run, make_engine, make_sessionmaker
from tests.unit.test_api import _orders_by_tag, body, paper_session, registry, settings, terminal

__all__ = ["settings"]  # fixture re-export


async def _strand(
    settings: Settings, reg: Registry, sid: str, rng: random.Random
) -> dict[str, str]:
    """Returns tag -> what the crash left behind ('accepted' | 'unsent' | 'planned')."""
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    loaded = await Runtime(settings, reg, sm).load_session(sid)
    assert loaded is not None
    ins = [{"symbol": sym, "action": "BUY", "quantity": 1} for sym in sorted(PRICES)]
    req = ExecuteRequest.model_validate(body(sid, instructions=ins))
    run_id = str(uuid4())
    intents = plan(req, run_id)
    past = datetime.now(UTC) - timedelta(seconds=5)
    await repo.create_run(
        sm, run_id=run_id, user_id="u1", idempotency_key=run_id, request_hash="h",
        session_id=sid, mode=req.mode, options=req.options.model_dump(mode="json"),
        legs=intents, now=past,
    )  # fmt: skip
    fate: dict[str, str] = {}
    async with sm.begin() as s:
        for it in intents:
            fate[it.tag] = rng.choice(["accepted", "unsent", "planned"])
            if fate[it.tag] == "accepted":
                await loaded.adapter.place_order(loaded.session, it)
            if fate[it.tag] != "planned":
                await s.execute(
                    update(Leg).where(Leg.tag == it.tag).values(status=LegStatus.SUBMITTING)
                )
        await s.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status=RunStatus.RUNNING, lease_owner="dead-worker", lease_until=past)
        )
    await engine.dispose()
    fate["run_id"] = run_id
    return fate


@pytest.mark.parametrize("seed", range(5))
def test_stranded_random_run_resumed_by_lifespan_no_duplicates(
    settings: Settings, seed: int
) -> None:
    reg = registry()
    with TestClient(create_app(settings, reg)) as c:
        sid = paper_session(c)
    fate = asyncio.run(_strand(settings, reg, sid, random.Random(seed)))  # app is down
    run_id = fate.pop("run_id")
    assert {"accepted", "unsent", "planned"} == set(fate.values())  # every crash shape present
    with TestClient(create_app(settings, reg)) as c:  # restart: lifespan resume sweep
        run = terminal(c, run_id)
    assert "run.resumed" in [e["type"] for e in run["events"]]
    by_tag = _orders_by_tag(reg)
    assert max(by_tag.values()) == 1  # zero duplicate orders
    for lg in run["legs"]:
        tag = lg["tag"]
        if fate[tag] == "unsent":  # tag miss -> UNKNOWN, never sent (D30)
            assert lg["status"] == "UNKNOWN" and tag not in by_tag
        else:  # accepted -> adopted by tag; planned -> placed once
            assert lg["status"] == "FILLED" and by_tag[tag] == 1
