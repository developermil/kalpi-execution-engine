"""Contract fixture for Fyers API v3 (all payload shapes UNVERIFIED, see docs/brokers/fyers.md)."""

import json
from typing import Any

import httpx
from tests.contract.harness import Env, PlaceCheck

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.brokers.fyers import FyersBroker, app_id_hash
from kalpi_engine.domain.models import OrderIntent

BROKER_ID = "fyers"
SIGNS_REQUESTS = True
B = "https://api-t1.fyers.in"
MASTER = (
    "10100000002885,RELIANCE INDUSTRIES LTD,0,1,0.05,INE002A01018,0915-1530|1815-1915:,"
    "2026-09-30,,NSE:RELIANCE-EQ,10,10,2885,RELIANCE,2885,-1.0,XX,10100000002885,None,1,3.7\n"
    "10100000000001,SOME SME,0,1,0.05,INE000000000,0915-1530|1815-1915:,"
    "2026-09-30,,NSE:SME-SM,10,10,1,SME,1,-1.0,XX,10100000000001,None,1,3.7\n"
)


def make_adapter() -> BrokerAdapter:
    return FyersBroker(app_id="app-200", secret="secret", redirect_uri="http://localhost/cb")


def checksum_vectors() -> list[tuple[str, str]]:
    # sha256("app-200:secret"), computed independently of the adapter.
    expected = "db769dc63d1970a43c8e3eeac0d4a448a33ea3bde7e722dbcd955a35f2b05b79"
    return [(app_id_hash("app-200", "secret"), expected)]


async def setup(env: Env) -> None:
    env.router.get("https://public.fyers.in/sym_details/NSE_CM.csv").respond(
        200, content=MASTER.encode()
    )


async def login(env: Env) -> dict[str, Any]:
    env.router.post(f"{B}/api/v3/validate-authcode").respond(
        200, json={"s": "ok", "code": 200, "access_token": "tok-1"}
    )
    return {"auth_code": "abc"}


async def holdings(env: Env, s: BrokerSession) -> dict[str, int]:
    rows = [
        {"symbol": "NSE:TCS-EQ", "quantity": 10, "remainingQuantity": 7, "costPrice": 100.0},
        {"symbol": "NSE:INFY-EQ", "quantity": 4, "remainingQuantity": 0, "costPrice": 50.0},
    ]  # fmt: skip
    env.router.get(f"{B}/api/v3/holdings").respond(200, json={"s": "ok", "holdings": rows})
    return {"TCS": 7, "INFY": 0}


async def place_ok(env: Env, s: BrokerSession, intent: OrderIntent) -> PlaceCheck:
    route = env.router.post(f"{B}/api/v3/orders/sync").respond(
        200, json={"s": "ok", "code": 1101, "id": "F-1"}
    )

    def sent() -> dict[str, Any]:
        out: dict[str, Any] = json.loads(route.calls.last.request.content)
        return out

    return PlaceCheck(
        order_id="F-1",
        sent=sent,
        expect={
            "symbol": "NSE:RELIANCE-EQ", "qty": 1, "type": 2, "limitPrice": 0, "stopPrice": 0,
            "side": 1 if intent.side.value == "BUY" else -1, "productType": "CNC",
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
            "orderTag": intent.tag,
        },
    )  # fmt: skip


async def rate_limited(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/api/v3/orders/sync").respond(429, headers={"Retry-After": "1"})


async def auth_expired(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"s": "error", "code": -16, "message": "Could not authenticate the user"}
    env.router.get(f"{B}/api/v3/holdings").respond(401, json=body)


async def reject(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"s": "error", "code": -50, "message": "Invalid input"}
    env.router.post(f"{B}/api/v3/orders/sync").respond(400, json=body)


async def error_200(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"s": "error", "code": -99, "message": "blocked"}
    env.router.post(f"{B}/api/v3/orders/sync").respond(200, json=body)


async def timeout(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/api/v3/orders/sync").mock(side_effect=httpx.ReadTimeout("slow"))


async def tag_hit(env: Env, s: BrokerSession, tag: str) -> str:
    row = {"id": "F-9", "status": 2, "filledQty": 1, "orderTag": tag}
    env.router.get(f"{B}/api/v3/orders").respond(200, json={"s": "ok", "orderBook": [row]})
    return "F-9"


async def tag_miss(env: Env, s: BrokerSession, tag: str) -> None:
    env.router.get(f"{B}/api/v3/orders").respond(200, json={"s": "ok", "orderBook": []})


async def resolve(env: Env) -> tuple[str, str]:
    return "RELIANCE", "NSE:RELIANCE-EQ"
