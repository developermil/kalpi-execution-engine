"""Contract fixture for Zerodha Kite Connect: every condition is a respx route."""

from typing import Any
from urllib.parse import parse_qs

import httpx
from tests.contract.harness import Env, PlaceCheck

from kalpi_engine.brokers.base import BrokerAdapter, BrokerSession
from kalpi_engine.brokers.zerodha import ZerodhaBroker, checksum
from kalpi_engine.domain.models import OrderIntent

BROKER_ID = "zerodha"
SIGNS_REQUESTS = True
B = "https://api.kite.trade"
MASTER = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,"
    "lot_size,instrument_type,segment,exchange\n"
    "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.05,1,EQ,NSE,NSE\n"
)


def make_adapter() -> BrokerAdapter:
    return ZerodhaBroker(api_key="key", api_secret="secret")


def checksum_vectors() -> list[tuple[str, str]]:
    # sha256("key" + "abc" + "secret"), computed independently of the adapter.
    expected = "6e42a1b9e2a1993979ccc54a2f8c5eb4b048209b56bb621cbd8a552bae176c94"
    return [(checksum("key", "abc", "secret"), expected)]


async def setup(env: Env) -> None:
    env.router.get(f"{B}/instruments/NSE").respond(200, content=MASTER.encode())


async def login(env: Env) -> dict[str, Any]:
    env.router.post(f"{B}/session/token").respond(
        200, json={"status": "success", "data": {"access_token": "tok-1"}}
    )
    return {"request_token": "abc"}


async def holdings(env: Env, s: BrokerSession) -> dict[str, int]:
    rows = [
        {"tradingsymbol": "TCS", "exchange": "NSE", "quantity": 10, "t1_quantity": 3,
         "used_quantity": 2, "collateral_quantity": 1, "average_price": 100.0},
        {"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 4, "t1_quantity": 0,
         "used_quantity": 5, "collateral_quantity": 0, "average_price": 50.0},
    ]  # fmt: skip
    env.router.get(f"{B}/portfolio/holdings").respond(200, json={"status": "success", "data": rows})
    return {"TCS": 7, "INFY": 0}


async def place_ok(env: Env, s: BrokerSession, intent: OrderIntent) -> PlaceCheck:
    route = env.router.post(f"{B}/orders/regular").respond(
        200, json={"status": "success", "data": {"order_id": "Z-1"}}
    )

    def sent() -> dict[str, Any]:
        q = parse_qs(route.calls.last.request.content.decode())
        form: dict[str, Any] = {k: v[0] for k, v in q.items()}
        form["market_protection"] = int(form["market_protection"])
        form["quantity"] = int(form["quantity"])
        return form

    return PlaceCheck(
        order_id="Z-1",
        sent=sent,
        expect={
            "market_protection": -1, "tradingsymbol": "RELIANCE", "exchange": "NSE",
            "transaction_type": intent.side.value, "order_type": "MARKET", "product": "CNC",
            "tag": intent.tag, "quantity": 1,
        },
    )  # fmt: skip


async def rate_limited(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/orders/regular").respond(429, headers={"Retry-After": "1"})


async def auth_expired(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": "error", "message": "Incorrect api_key or access_token",
            "error_type": "TokenException"}  # fmt: skip
    env.router.get(f"{B}/portfolio/holdings").respond(403, json=body)


async def reject(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": "error", "message": "bad input", "error_type": "InputException"}
    env.router.post(f"{B}/orders/regular").respond(400, json=body)


async def error_200(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    body = {"status": "error", "message": "blocked"}
    env.router.post(f"{B}/orders/regular").respond(200, json=body)


async def timeout(env: Env, s: BrokerSession, intent: OrderIntent) -> None:
    env.router.post(f"{B}/orders/regular").mock(side_effect=httpx.ReadTimeout("slow"))


async def tag_hit(env: Env, s: BrokerSession, tag: str) -> str:
    row = {"order_id": "Z-9", "status": "COMPLETE", "filled_quantity": 1, "tag": tag}
    env.router.get(f"{B}/orders").respond(200, json={"status": "success", "data": [row]})
    return "Z-9"


async def tag_miss(env: Env, s: BrokerSession, tag: str) -> None:
    env.router.get(f"{B}/orders").respond(200, json={"status": "success", "data": []})


async def resolve(env: Env) -> tuple[str, str]:
    return "RELIANCE", "738561"
