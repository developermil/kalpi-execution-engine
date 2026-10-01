"""Contract fixture for Paper: no HTTP, so each condition is arranged with a Faults knob."""

from typing import Any

from tests.contract import harness
from tests.contract.harness import Env, PlaceCheck

from kalpi_engine.brokers.base import BrokerSession
from kalpi_engine.brokers.paper import DEMO_HOLDINGS, PaperBroker
from kalpi_engine.domain.enums import Side
from kalpi_engine.domain.models import OrderIntent

BROKER_ID = "paper"
SIGNS_REQUESTS = False


def make_adapter() -> PaperBroker:
    return PaperBroker()


def _paper(env: Env) -> PaperBroker:
    assert isinstance(env.adapter, PaperBroker)
    return env.adapter


async def login(env: Env) -> dict[str, Any]:
    return {"profile": "demo"}


async def holdings(env: Env, s: BrokerSession) -> dict[str, int]:
    return dict(DEMO_HOLDINGS)


async def place_ok(env: Env, s: BrokerSession, intent: OrderIntent) -> PlaceCheck:
    paper = _paper(env)
    order_id = f"P{len(paper._orders) + 1:08d}"
    return PlaceCheck(
        order_id=order_id,
        sent=lambda: paper._orders[order_id].intent.model_dump(mode="json"),
        expect={"symbol": intent.symbol, "side": "BUY", "quantity": 1, "tag": intent.tag},
    )


async def rate_limited(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    _paper(env).faults.rate_limit_next = 1


async def auth_expired(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    _paper(env).faults.expire_sessions = True


async def reject(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    _paper(env).faults.reject_symbols.add(intent.symbol)


async def error_200(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    # Paper has no HTTP body; its "accepted call, error result" is a business reject.
    _paper(env).faults.reject_symbols.add(intent.symbol)


async def timeout(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    _paper(env).faults.timeout_after_accept_next = 1


async def tag_hit(env: Env, s: BrokerSession, tag: str) -> str:
    return await env.adapter.place_order(s, harness.intent(tag=tag, side=Side.BUY))


async def tag_miss(env: Env, s: BrokerSession, tag: str) -> None:
    return None


async def resolve(env: Env) -> tuple[str, str]:
    return "RELIANCE", "PAPER-RELIANCE"
