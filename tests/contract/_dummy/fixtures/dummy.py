"""Contract fixture for the dummy HTTP broker: every condition is a respx route."""

import importlib
import json
from typing import Any

import httpx
from tests.contract.harness import Env, PlaceCheck

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.domain.models import OrderIntent

BROKER_ID = "dummy"
SIGNS_REQUESTS = True
B = "https://dummy.test"


def _mod() -> Any:
    # Imported lazily: kalpi_engine.brokers.dummy exists only while the test extends __path__.
    return importlib.import_module("kalpi_engine.brokers.dummy")


def make_adapter() -> BrokerAdapter:
    adapter: BrokerAdapter = _mod().DummyBroker()
    return adapter


def checksum_vectors() -> list[tuple[str, str]]:
    # sha256("key" + "abc" + "secret"), computed independently of the adapter.
    expected = "6e42a1b9e2a1993979ccc54a2f8c5eb4b048209b56bb621cbd8a552bae176c94"
    return [(_mod().checksum("key", "abc", "secret"), expected)]


async def setup(env: Env) -> None:
    master = b"exch,symbol,token\nNSE,RELIANCE,2885\n"
    env.router.get(f"{B}/instruments").respond(200, content=master)


async def login(env: Env) -> dict[str, Any]:
    env.router.post(f"{B}/session").respond(200, json={"data": {"token": "tok-1"}})
    return {"code": "abc"}


async def holdings(env: Env, s: BrokerSession) -> dict[str, int]:
    rows = [{"symbol": "TCS", "qty": 5, "t1_qty": 2}]
    env.router.get(f"{B}/holdings").respond(200, json={"data": rows})
    return {"TCS": 3}


async def place_ok(env: Env, s: BrokerSession, intent: OrderIntent) -> PlaceCheck:
    route = env.router.post(f"{B}/orders").respond(200, json={"data": {"order_id": "D-1"}})
    return PlaceCheck(
        order_id="D-1",
        sent=lambda: json.loads(route.calls.last.request.content),
        expect={"token": "2885", "market_protection": -1, "tag": intent.tag, "qty": 1},
    )


async def rate_limited(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/orders").respond(429, headers={"Retry-After": "1"})


async def auth_expired(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": "error", "code": "TOKEN_EXPIRED"}
    env.router.get(f"{B}/holdings").respond(200, json=body)


async def reject(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/orders").respond(400, json={"error_type": "InputException"})


async def error_200(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/orders").respond(200, json={"status": "error", "code": "RMS_BLOCK"})


async def timeout(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/orders").mock(side_effect=httpx.ReadTimeout("slow"))


async def tag_hit(env: Env, s: BrokerSession, tag: str) -> str:
    row = {"order_id": "D-9", "status": "FILLED", "filled": 1, "tag": tag}
    env.router.get(f"{B}/orders").respond(200, json={"data": [row]})
    return "D-9"


async def tag_miss(env: Env, s: BrokerSession, tag: str) -> None:
    env.router.get(f"{B}/orders").respond(200, json={"data": []})


async def resolve(env: Env) -> tuple[str, str]:
    return "RELIANCE", "2885"
