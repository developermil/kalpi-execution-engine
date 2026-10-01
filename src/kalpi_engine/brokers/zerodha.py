"""Zerodha Kite Connect v3 adapter over plain httpx (D25). Docs: docs/brokers/zerodha.md.

UNVERIFIED against a live account: base host, auth header, variety list, margins field path.
"""

import csv
import hashlib
import io
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

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

IST = timezone(timedelta(hours=5, minutes=30))


def checksum(api_key: str, request_token: str, api_secret: str) -> str:
    # POST /session/token checksum = SHA-256(api_key + request_token + api_secret)  [user docs]
    return hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()


def parse_master(raw: bytes) -> Iterable[InstrumentRef]:
    # GET /instruments/NSE: CSV with instrument_token, tradingsymbol, tick_size, lot_size, ...
    for row in csv.DictReader(io.StringIO(raw.decode())):
        yield InstrumentRef(
            exchange=Exchange.NSE,
            symbol=row["tradingsymbol"],
            token=row["instrument_token"],
            lot_size=int(row.get("lot_size") or 1),
            tick_size=float(row.get("tick_size") or 0.05),
        )


def _expiry(now: datetime) -> datetime:
    """access_token expires 06:00 IST next day (user docs); be conservative with 05:55."""
    local = now.astimezone(IST)
    cut = local.replace(hour=5, minute=55, second=0, microsecond=0)
    return (cut if local < cut else cut + timedelta(days=1)).astimezone(UTC)


def _state(o: Mapping[str, Any]) -> BrokerOrderState:
    # Statuses: COMPLETE/REJECTED/CANCELLED terminal; everything else non-terminal (orders docs).
    status = {
        "COMPLETE": OrderStatus.FILLED,
        "REJECTED": OrderStatus.REJECTED,
        "CANCELLED": OrderStatus.CANCELLED,
    }.get(str(o["status"]).upper(), OrderStatus.OPEN)
    filled = int(o.get("filled_quantity") or 0)
    if status is OrderStatus.OPEN and filled > 0:
        status = OrderStatus.PARTIAL  # no native PARTIAL status; derive from filled quantity
    avg = o.get("average_price")
    return BrokerOrderState(
        broker_order_id=str(o["order_id"]),
        status=status,
        filled_qty=filled,
        avg_price=float(avg) if avg else None,
    )


class ZerodhaBroker(HttpBrokerAdapter):
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="zerodha",
        name="Zerodha (Kite Connect)",
        auth_mode=AuthMode.OAUTH_REDIRECT,
        rate_limits=RateLimits(orders_per_sec=10, reads_per_sec=10, orders_per_day=5000),
        requires_static_ip=True,
        daily_2fa=True,
        tag_max_len=20,
        experimental=True,
        live_tested=False,
        market_order_verified=False,
    )
    base_url: ClassVar[str] = "https://api.kite.trade"  # UNVERIFIED host; copied from SDK
    # 403 + TokenException = session expired (exceptions docs)
    auth_error_codes: ClassVar[frozenset[str]] = frozenset({"TokenException"})

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        super().__init__()
        self._api_key = api_key if api_key is not None else os.environ.get("ZERODHA_API_KEY", "")
        self._secret = (
            api_secret if api_secret is not None else os.environ.get("ZERODHA_API_SECRET", "")
        )
        self.instruments = InstrumentResolver(self._fetch_master, parse_master)

    async def _fetch_master(self) -> bytes:
        # UNVERIFIED: whether the dump needs an access token; send the api_key-only header.
        headers = {"X-Kite-Version": "3", "Authorization": f"token {self._api_key}:"}
        return (await self.client.get("/instruments/NSE", headers=headers)).content

    async def startup(self) -> None:
        await self.instruments.load()

    def _auth(self, s: BrokerSession) -> dict[str, str]:
        # `Authorization: token api_key:access_token` (SDK; UNVERIFIED on docs page)
        tok = s.access_token.get_secret_value()
        return {"X-Kite-Version": "3", "Authorization": f"token {self._api_key}:{tok}"}

    def order_id_from(self, body: Any) -> str | None:
        data = body.get("data") if isinstance(body, dict) else None
        return str(data["order_id"]) if isinstance(data, dict) and data.get("order_id") else None

    async def login_url(self, state: str) -> str:
        # Browser login; `redirect_params` is echoed back on the registered redirect URL.
        redirect = quote(urlencode({"state": state}), safe="")
        return (
            f"https://kite.zerodha.com/connect/login?v=3&api_key={quote(self._api_key)}"
            f"&redirect_params={redirect}"
        )

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        token = str(params["request_token"])
        body = await self.request(  # POST /session/token (user docs)
            "POST",
            "/session/token",
            headers={"X-Kite-Version": "3"},
            data={
                "api_key": self._api_key,
                "request_token": token,
                "checksum": checksum(self._api_key, token, self._secret),
            },
        )
        return BrokerSession(
            broker_id=self.meta.id,
            access_token=SecretStr(body["data"]["access_token"]),
            expires_at=_expiry(datetime.now(UTC)),
        )

    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        body = await self.request("GET", "/portfolio/holdings", headers=self._auth(s))  # portfolio
        rows: list[Holding] = []
        for h in body["data"]:
            qty = int(h["quantity"])  # settled only; t1_quantity excluded
            # Most conservative: settled minus used and pledged (collateral) quantity.
            sellable = max(
                0, qty - int(h.get("used_quantity") or 0) - int(h.get("collateral_quantity") or 0)
            )
            rows.append(
                Holding(
                    symbol=h["tradingsymbol"],
                    exchange=Exchange(h.get("exchange", "NSE")),
                    quantity=qty,
                    sellable_qty=sellable,
                    avg_price=h.get("average_price"),
                )
            )
        return rows

    async def get_funds(self, s: BrokerSession) -> Funds | None:
        body = await self.request("GET", "/user/margins/equity", headers=self._auth(s))  # user
        cash = ((body.get("data") or {}).get("available") or {}).get("cash")  # UNVERIFIED path
        return Funds(available_cash=float(cash)) if cash is not None else None

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        return await self.instruments.resolve(exchange, symbol)

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        await self.resolve_instrument(intent.exchange, intent.symbol)
        form: dict[str, Any] = {
            "tradingsymbol": intent.symbol,
            "exchange": intent.exchange.value,
            "transaction_type": intent.side.value,
            "order_type": intent.order_type.value,
            "quantity": intent.quantity,
            "product": intent.product.value,
            "validity": "DAY",
            "tag": intent.tag,
        }
        if intent.order_type is OrderType.MARKET:
            form["market_protection"] = -1  # mandatory on MARKET via API; -1 = automatic
        else:
            form["price"] = intent.limit_price
        body = await self.request(  # POST /orders/regular (orders docs; only variety sent)
            "POST", "/orders/regular", placing=True, data=form, headers=self._auth(s)
        )
        return str(self.order_id_from(body))

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        body = await self.request("GET", f"/orders/{broker_order_id}", headers=self._auth(s))
        return _state(body["data"][-1])  # history of one order; last entry is current

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        body = await self.request("GET", "/orders", headers=self._auth(s))  # day's order book
        hits = [o for o in body["data"] if o.get("tag") == tag]
        return _state(hits[-1]) if hits else None
