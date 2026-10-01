"""A minimal HTTP broker used only to prove onboarding needs zero shared edits (C0).

Not shipped: the test puts this directory on `kalpi_engine.brokers.__path__` temporarily.
It is also the smallest worked example of HttpBrokerAdapter + InstrumentResolver.
"""

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


class DummyBroker(HttpBrokerAdapter):
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="dummy",
        name="Dummy (contract proof)",
        auth_mode=AuthMode.OAUTH_REDIRECT,
        rate_limits=RateLimits(orders_per_sec=10, reads_per_sec=10),
    )
    base_url: ClassVar[str] = "https://dummy.test"
    auth_error_codes: ClassVar[frozenset[str]] = frozenset({"TOKEN_EXPIRED"})
    API_KEY: ClassVar[str] = "key"
    SECRET: ClassVar[str] = "secret"

    def __init__(self) -> None:
        super().__init__()
        self.instruments = InstrumentResolver(self._fetch_master, parse_master)

    async def _fetch_master(self) -> bytes:
        return (await self.client.get("/instruments")).content

    async def startup(self) -> None:
        await self.instruments.load()

    def _auth(self, s: BrokerSession) -> dict[str, str]:
        return {"Authorization": f"token {s.access_token.get_secret_value()}"}

    def order_id_from(self, body: Any) -> str | None:
        return (body.get("data") or {}).get("order_id") if isinstance(body, dict) else None

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        code = str(params["code"])
        sig = checksum(self.API_KEY, code, self.SECRET)
        body = await self.request(
            "POST", "/session", json={"api_key": self.API_KEY, "code": code, "checksum": sig}
        )
        return BrokerSession(broker_id="dummy", access_token=SecretStr(body["data"]["token"]))

    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        body = await self.request("GET", "/holdings", headers=self._auth(s))
        return [
            Holding(
                symbol=h["symbol"],
                exchange=Exchange.NSE,
                quantity=h["qty"],
                sellable_qty=h["qty"] - h["t1_qty"],
            )
            for h in body["data"]
        ]

    async def get_funds(self, s: BrokerSession) -> Funds | None:
        return None

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        return await self.instruments.resolve(exchange, symbol)

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        ref = await self.resolve_instrument(intent.exchange, intent.symbol)
        payload = {
            "token": ref.token,
            "side": intent.side.value,
            "qty": intent.quantity,
            "type": intent.order_type.value,
            "tag": intent.tag,
            "market_protection": -1 if intent.order_type is OrderType.MARKET else 0,
        }
        body = await self.request(
            "POST", "/orders", placing=True, json=payload, headers=self._auth(s)
        )
        return str(self.order_id_from(body))

    @staticmethod
    def _state(o: Mapping[str, Any]) -> BrokerOrderState:
        return BrokerOrderState(
            broker_order_id=o["order_id"],
            status=OrderStatus(o["status"]),
            filled_qty=o.get("filled", 0),
        )

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        body = await self.request("GET", f"/orders/{broker_order_id}", headers=self._auth(s))
        return self._state(body["data"])

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        body = await self.request("GET", "/orders", headers=self._auth(s))
        hits = [o for o in body["data"] if o.get("tag") == tag]
        return self._state(hits[0]) if hits else None
