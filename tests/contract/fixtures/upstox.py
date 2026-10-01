"""Contract fixture for Upstox: every condition is a respx route."""

import gzip
import json
from typing import Any

import httpx
from tests.contract.harness import Env, PlaceCheck

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.brokers.upstox import MASTER_URL, UpstoxBroker
from kalpi_engine.domain.models import OrderIntent

BROKER_ID = "upstox"
SIGNS_REQUESTS = False
B = "https://api.upstox.com"
HFT = "https://api-hft.upstox.com"
KEY = "NSE_EQ|INE002A01018"
_ROWS = [
    {"segment": "NSE_EQ", "trading_symbol": "RELIANCE", "instrument_key": KEY, "lot_size": 1},
    {"segment": "BSE_EQ", "trading_symbol": "OTHER", "instrument_key": "BSE_EQ|X"},
]
MASTER = gzip.compress(json.dumps(_ROWS).encode())


def make_adapter() -> BrokerAdapter:
    return UpstoxBroker(api_key="key", api_secret="secret", redirect_uri="http://cb")


async def setup(env: Env) -> None:
    env.router.get(MASTER_URL).respond(200, content=MASTER)


async def login(env: Env) -> dict[str, Any]:
    env.router.post(f"{B}/v2/login/authorization/token").respond(200, json={"access_token": "t1"})
    return {"code": "abc"}


async def holdings(env: Env, s: BrokerSession) -> dict[str, int]:
    rows = [
        {"trading_symbol": "TCS", "quantity": 10, "t1_quantity": 3, "collateral_quantity": 1,
         "average_price": 100.0, "instrument_token": "NSE_EQ|X"},
        {"trading_symbol": "INFY", "quantity": 4, "t1_quantity": 5, "collateral_quantity": 0,
         "average_price": 50.0, "instrument_token": "NSE_EQ|Y"},
    ]  # fmt: skip
    env.router.get(f"{B}/v2/portfolio/long-term-holdings").respond(
        200, json={"status": "success", "data": rows}
    )
    return {"TCS": 6, "INFY": 0}


async def place_ok(env: Env, s: BrokerSession, intent: OrderIntent) -> PlaceCheck:
    route = env.router.post(f"{HFT}/v3/order/place").respond(
        200, json={"status": "success", "data": {"order_ids": ["U-1"]}}
    )
    return PlaceCheck(
        order_id="U-1",
        sent=lambda: json.loads(route.calls.last.request.content),
        expect={
            "market_protection": -1, "slice": False, "order_type": "MARKET", "price": 0,
            "instrument_token": KEY, "transaction_type": intent.side.value, "product": "D",
            "validity": "DAY", "tag": intent.tag, "quantity": 1,
        },
    )  # fmt: skip


async def rate_limited(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{HFT}/v3/order/place").respond(429, headers={"Retry-After": "1"})


async def auth_expired(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": "error", "errors": [{"errorCode": "UDAPI100050", "message": "bad token"}]}
    env.router.get(f"{B}/v2/portfolio/long-term-holdings").respond(401, json=body)


async def reject(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": "error", "errors": [{"errorCode": "UDAPI100500", "message": "bad input"}]}
    env.router.post(f"{HFT}/v3/order/place").respond(400, json=body)


async def error_200(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{HFT}/v3/order/place").respond(200, json={"status": "error", "message": "x"})


async def timeout(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{HFT}/v3/order/place").mock(side_effect=httpx.ReadTimeout("slow"))


async def tag_hit(env: Env, s: BrokerSession, tag: str) -> str:
    row = {"order_id": "U-9", "status": "complete", "filled_quantity": 1, "tag": tag}
    body = {"status": "success", "data": [row]}
    env.router.get(f"{B}/v2/order/retrieve-all").respond(200, json=body)
    return "U-9"


async def tag_miss(env: Env, s: BrokerSession, tag: str) -> None:
    body = {"status": "success", "data": []}
    env.router.get(f"{B}/v2/order/retrieve-all").respond(200, json=body)


async def resolve(env: Env) -> tuple[str, str]:
    return "RELIANCE", KEY
