"""Contract fixture for Angel One SmartAPI: every condition is a respx route."""

import json
from typing import Any

import httpx
from tests.contract.harness import Env, PlaceCheck

from kalpi_engine.brokers.angelone import MASTER_URL, AngelOneBroker, totp
from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.domain.models import OrderIntent

BROKER_ID = "angelone"
SIGNS_REQUESTS = True
B = "https://apiconnect.angelone.in"
S = f"{B}/rest/secure/angelbroking"
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890" (RFC 6238)
MASTER = [
    {"token": "2885", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "expiry": "", "strike": "-1",
     "lotsize": "1", "instrumenttype": "", "exch_seg": "NSE", "tick_size": "5.000000"},
    {"token": "99", "symbol": "NIFTY", "name": "NIFTY", "expiry": "", "strike": "-1",
     "lotsize": "1", "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0.000000"},
]  # fmt: skip


def make_adapter() -> BrokerAdapter:
    return AngelOneBroker(api_key="key")


def checksum_vectors() -> list[tuple[str, str]]:
    # RFC 6238 test vector: SHA1, T=59 -> 94287082 -> last 6 digits.
    return [(totp(SECRET, now=59), "287082")]


def _ok(data: Any) -> dict[str, Any]:
    return {"status": True, "message": "SUCCESS", "errorcode": "", "data": data}


async def setup(env: Env) -> None:
    env.router.get(MASTER_URL).respond(200, content=json.dumps(MASTER).encode())


async def login(env: Env) -> dict[str, Any]:
    data = {"jwtToken": "tok-1", "refreshToken": "r", "feedToken": "f"}
    env.router.post(f"{B}/rest/auth/angelbroking/user/v1/loginByPassword").respond(
        200, json=_ok(data)
    )
    return {"client_code": "C1", "pin": "1234", "totp_secret": SECRET}


async def holdings(env: Env, s: BrokerSession) -> dict[str, int]:
    rows = [
        {"tradingsymbol": "TCS-EQ", "exchange": "NSE", "quantity": 10, "t1quantity": 3,
         "collateralquantity": 1, "averageprice": 100.0},
        {"tradingsymbol": "INFY-EQ", "exchange": "NSE", "quantity": 4, "t1quantity": 5,
         "collateralquantity": 0, "averageprice": 50.0},
    ]  # fmt: skip
    env.router.get(f"{S}/portfolio/v1/getHolding").respond(200, json=_ok(rows))
    return {"TCS": 6, "INFY": 0}


async def place_ok(env: Env, s: BrokerSession, intent: OrderIntent) -> PlaceCheck:
    route = env.router.post(f"{S}/order/v1/placeOrder").respond(
        200, json=_ok({"script": "RELIANCE-EQ", "orderid": "A-0", "uniqueorderid": "A-1"})
    )
    return PlaceCheck(
        order_id="A-1",
        sent=lambda: json.loads(route.calls.last.request.content),
        expect={
            "price": "0", "ordertype": "MARKET", "variety": "NORMAL", "duration": "DAY",
            "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885", "exchange": "NSE",
            "transactiontype": intent.side.value, "producttype": "DELIVERY",
            "ordertag": intent.tag, "quantity": "1",
        },
    )  # fmt: skip


async def rate_limited(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{S}/order/v1/placeOrder").respond(429, headers={"Retry-After": "1"})


async def auth_expired(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": False, "message": "Invalid Token", "errorcode": "AG8001", "data": None}
    env.router.get(f"{S}/portfolio/v1/getHolding").respond(403, json=body)


async def reject(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": False, "message": "Ordertag length should not exceed 20 characters",
            "errorcode": "AB4008", "data": None}  # fmt: skip
    env.router.post(f"{S}/order/v1/placeOrder").respond(400, json=body)


async def error_200(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": False, "message": "blocked", "errorcode": "", "data": None}
    env.router.post(f"{S}/order/v1/placeOrder").respond(200, json=body)


async def timeout(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{S}/order/v1/placeOrder").mock(side_effect=httpx.ReadTimeout("slow"))


async def tag_hit(env: Env, s: BrokerSession, tag: str) -> str:
    row = {"orderid": "A-8", "uniqueorderid": "A-9", "status": "complete", "filledshares": "1",
           "ordertag": tag}  # fmt: skip
    env.router.get(f"{S}/order/v1/getOrderBook").respond(200, json=_ok([row]))
    return "A-9"


async def tag_miss(env: Env, s: BrokerSession, tag: str) -> None:
    env.router.get(f"{S}/order/v1/getOrderBook").respond(200, json=_ok([]))


async def resolve(env: Env) -> tuple[str, str]:
    return "RELIANCE", "2885"
