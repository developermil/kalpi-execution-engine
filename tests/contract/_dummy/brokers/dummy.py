"""Test-only 6th broker (C6): one file in brokers/ is all a new broker needs. Not shipped."""

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from pydantic import SecretStr

from kalpi_engine.brokers.base import (
    AuthMode,
    BrokerMeta,
    BrokerSession,
    HttpBrokerAdapter,
    InstrumentRef,
    RateLimits,
)
from kalpi_engine.brokers.instruments import InstrumentResolver
from kalpi_engine.domain.enums import Exchange, OrderStatus, OrderType
from kalpi_engine.domain.models import BrokerOrderState, Funds, Holding, OrderIntent


def checksum(api_key: str, code: str, secret: str) -> str:
    return hashlib.sha256(f"{api_key}{code}{secret}".encode()).hexdigest()


def parse_master(raw: bytes) -> Iterable[InstrumentRef]:
    for line in raw.decode().splitlines()[1:]:
        exch, symbol, token = line.split(",")
        yield InstrumentRef(exchange=Exchange(exch), symbol=symbol, token=token)


def _state(o: Mapping[str, Any]) -> BrokerOrderState:
    status, filled = OrderStatus(o["status"]), o.get("filled", 0)
    return BrokerOrderState(broker_order_id=o["order_id"], status=status, filled_qty=filled)


class DummyBroker(HttpBrokerAdapter):
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="dummy", name="Dummy", auth_mode=AuthMode.OAUTH_REDIRECT,
        rate_limits=RateLimits(orders_per_sec=10, reads_per_sec=10),
    )  # fmt: skip
    base_url: ClassVar[str] = "https://dummy.test"
    auth_error_codes: ClassVar[frozenset[str]] = frozenset({"TOKEN_EXPIRED"})

    def __init__(self) -> None:
        super().__init__()
        self.instruments = InstrumentResolver(self._fetch_master, parse_master)

    async def _fetch_master(self) -> bytes:
        return (await self.client.get("/instruments")).content

    async def _get(self, s: BrokerSession, path: str) -> Any:
        auth = {"Authorization": f"token {s.access_token.get_secret_value()}"}
        return (await self.request("GET", path, headers=auth))["data"]

    def order_id_from(self, body: Any) -> str | None:
        return (body.get("data") or {}).get("order_id") if isinstance(body, dict) else None

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        code = str(params["code"])
        sig = {"api_key": "key", "code": code, "checksum": checksum("key", code, "secret")}
        body = await self.request("POST", "/session", json=sig)
        return BrokerSession(broker_id="dummy", access_token=SecretStr(body["data"]["token"]))

    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        rows = await self._get(s, "/holdings")
        return [Holding(symbol=h["symbol"], exchange=Exchange.NSE, quantity=h["qty"],
                        sellable_qty=h["qty"] - h["t1_qty"]) for h in rows]  # fmt: skip

    async def get_funds(self, s: BrokerSession) -> Funds | None:
        return None

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        return await self.instruments.resolve(exchange, symbol)

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        ref = await self.resolve_instrument(intent.exchange, intent.symbol)
        mp = -1 if intent.order_type is OrderType.MARKET else 0  # MARKET needs protection
        payload = {
            "token": ref.token,
            "side": intent.side.value,
            "qty": intent.quantity,
            "type": intent.order_type.value,
            "tag": intent.tag,
            "market_protection": mp,
        }
        auth = {"Authorization": f"token {s.access_token.get_secret_value()}"}
        body = await self.request("POST", "/orders", placing=True, json=payload, headers=auth)
        return str(self.order_id_from(body))

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        return _state(await self._get(s, f"/orders/{broker_order_id}"))

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        rows = await self._get(s, "/orders")
        return next((_state(o) for o in rows if o.get("tag") == tag), None)
