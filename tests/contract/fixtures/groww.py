"""Contract fixture for Groww Trade API: every condition is a respx route."""

import hashlib
import json
from typing import Any

import httpx
from tests.contract.harness import Env, PlaceCheck

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.brokers.groww import MASTER_URL, GrowwBroker, checksum
from kalpi_engine.domain.models import OrderIntent

BROKER_ID = "groww"
SIGNS_REQUESTS = True
B = "https://api.groww.in"
MASTER = (
    "exchange,exchange_token,trading_symbol,groww_symbol,instrument_type,segment,lot_size,"
    "tick_size\nNSE,2885,RELIANCE,RELIANCE,EQ,CASH,1,0.05\n"
)


def make_adapter() -> BrokerAdapter:
    return GrowwBroker(api_key="key", api_secret="secret")


def checksum_vectors() -> list[tuple[str, str]]:
    expected = hashlib.sha256(b"secret1700000000").hexdigest()
    return [(checksum("secret", "1700000000"), expected)]


async def setup(env: Env) -> None:
    env.router.get(MASTER_URL).respond(200, content=MASTER.encode())


async def login(env: Env) -> dict[str, Any]:
    env.router.post(f"{B}/v1/token/api/access").respond(
        200, json={"status": "SUCCESS", "payload": {"token": "tok-1"}}
    )
    return {"totp": "123456"}


async def holdings(env: Env, s: BrokerSession) -> dict[str, int]:
    rows = [
        {"trading_symbol": "TCS", "quantity": 10, "demat_free_quantity": 6, "t1_quantity": 3,
         "pledge_quantity": 1, "average_price": 100.0},
        {"trading_symbol": "INFY", "quantity": 4, "demat_free_quantity": 9, "average_price": 50.0},
    ]  # fmt: skip
    env.router.get(f"{B}/v1/holdings/user").respond(
        200, json={"status": "SUCCESS", "payload": {"holdings": rows}}
    )
    return {"TCS": 6, "INFY": 4}  # INFY capped at net quantity


async def place_ok(env: Env, s: BrokerSession, intent: OrderIntent) -> PlaceCheck:
    route = env.router.post(f"{B}/v1/order/create").respond(
        200, json={"status": "SUCCESS", "payload": {"groww_order_id": "G-1"}}
    )

    def sent() -> dict[str, Any]:
        return dict(json.loads(route.calls.last.request.content))

    return PlaceCheck(
        order_id="G-1",
        sent=sent,
        expect={
            "trading_symbol": "RELIANCE", "exchange": "NSE", "segment": "CASH",
            "transaction_type": intent.side.value, "order_type": "MARKET", "product": "CNC",
            "validity": "DAY", "order_reference_id": intent.tag, "quantity": 1,
            "price": None,  # no price and no MPP/protection param is documented for MARKET
        },
    )  # fmt: skip


async def rate_limited(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/v1/order/create").respond(429, headers={"Retry-After": "1"})


async def auth_expired(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.get(f"{B}/v1/holdings/user").respond(
        401, json={"status": "FAILURE", "error": {"code": "GA005", "message": "expired"}}
    )


async def reject(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": "FAILURE", "error": {"code": "GA001", "message": "bad input"}}
    env.router.post(f"{B}/v1/order/create").respond(400, json=body)


async def error_200(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": "FAILURE", "error": {"code": "GA999", "message": "blocked"}}
    env.router.post(f"{B}/v1/order/create").respond(200, json=body)


async def timeout(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/v1/order/create").mock(side_effect=httpx.ReadTimeout("slow"))


async def tag_hit(env: Env, s: BrokerSession, tag: str) -> str:
    row = {"groww_order_id": "G-9", "order_status": "EXECUTED", "filled_quantity": 1,
           "order_reference_id": tag}  # fmt: skip
    env.router.get(f"{B}/v1/order/status/reference/{tag}").respond(
        200, json={"status": "SUCCESS", "payload": row}
    )
    return "G-9"


async def tag_miss(env: Env, s: BrokerSession, tag: str) -> None:
    body = {"status": "FAILURE", "error": {"code": "GA404", "message": "not found"}}
    env.router.get(f"{B}/v1/order/status/reference/{tag}").respond(404, json=body)


async def resolve(env: Env) -> tuple[str, str]:
    return "RELIANCE", "2885"
