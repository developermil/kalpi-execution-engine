"""A5 feasibility spike (throwaway): FIRST_TIME end-to-end in-process on Paper.

Minimal planner + naive sequential executor, but through the real port: registry discovery,
BrokerAdapter methods, domain schemas, deterministic tags and run_status().
"""

import asyncio
import time
import uuid

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.brokers.registry import Registry, discover
from kalpi_engine.domain.enums import LegStatus, OrderStatus, Phase
from kalpi_engine.domain.models import OrderIntent
from kalpi_engine.domain.schemas import ExecuteRequest
from kalpi_engine.domain.status import run_status
from kalpi_engine.domain.tags import make_tag, tag_length

PAYLOAD = {
    "session_id": str(uuid.uuid4()),
    "mode": "FIRST_TIME",
    "instructions": [
        {"symbol": "RELIANCE", "action": "BUY", "quantity": 10},
        {"symbol": "TCS", "action": "BUY", "quantity": 5},
        {"symbol": "INFY", "action": "BUY", "quantity": 12},
        {"symbol": "HDFCBANK", "action": "BUY", "quantity": 8},
        {"symbol": "ITC", "action": "BUY", "quantity": 40},
    ],
}


def plan_first_time(req: ExecuteRequest, run_id: str, tag_len: int) -> list[OrderIntent]:
    return [
        OrderIntent(
            leg_id=f"{run_id}:{i}", phase=Phase.BUY, symbol=ins.symbol,
            exchange=req.options.exchange, side=ins.effective_side, quantity=ins.quantity,
            order_type=req.order_type_for(ins), limit_price=ins.limit_price,
            tag=make_tag(run_id, i, tag_len),
        )
        for i, ins in enumerate(req.instructions)
    ]  # fmt: skip


async def execute(broker: BrokerAdapter, s: BrokerSession, legs: list[OrderIntent]) -> list[str]:
    statuses: list[str] = []
    for leg in legs:
        oid = await broker.place_order(s, leg)
        state = await broker.get_order(s, oid)
        while state.status is OrderStatus.OPEN:
            await asyncio.sleep(0.05)
            state = await broker.get_order(s, oid)
        statuses.append(state.status.value)
        print(
            f"  {leg.tag}  {leg.side:<4} {leg.symbol:<9} x{leg.quantity:<3} "
            f"-> {state.status:<7} filled={state.filled_qty} @ {state.avg_price}  [{oid}]"
        )
    return statuses


async def main() -> None:
    t0 = time.perf_counter()
    broker = Registry(discover()).get("paper")
    session = await broker.create_session({})
    assert await broker.get_holdings(session) == [], "FIRST_TIME needs empty holdings (V1)"
    req = ExecuteRequest.model_validate(PAYLOAD)
    run_id = str(uuid.uuid4())
    legs = plan_first_time(req, run_id, tag_length(broker.meta.tag_max_len))
    print(f"run {run_id} on {broker.meta.id}: {len(legs)} legs")
    statuses = await execute(broker, session, legs)
    status = run_status(LegStatus(s) for s in statuses)
    holdings = {h.symbol: h.quantity for h in await broker.get_holdings(session)}
    print(f"run status: {status}; holdings: {holdings}")
    print(f"filled {statuses.count('FILLED')}/{len(legs)} in {time.perf_counter() - t0:.3f}s")


if __name__ == "__main__":
    asyncio.run(main())
