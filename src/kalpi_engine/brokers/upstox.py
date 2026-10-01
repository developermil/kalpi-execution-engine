"""Upstox v2/v3 adapter over plain httpx. Docs: docs/brokers/upstox.md (experimental).

UNVERIFIED against a live account: token-endpoint content type (form assumed), holdings `quantity`
semantics, order-details by tag, error envelope, market_protection default, X-Algo-Name header.
"""

import gzip
import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, ClassVar
from urllib.parse import urlencode

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
HFT = "https://api-hft.upstox.com"  # place host differs from the rest (docs: Place order)
MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
_C = OrderStatus.CANCELLED
_DONE = {"complete": OrderStatus.FILLED, "rejected": OrderStatus.REJECTED, "cancelled": _C,
         "cancelled after market order": _C}  # fmt: skip


def parse_master(raw: bytes) -> Iterable[InstrumentRef]:
    # /instruments/: gzip JSON; segment NSE_EQ, trading_symbol, instrument_key `NSE_EQ|<ISIN>`.
    for row in json.loads(gzip.decompress(raw)):
        if row.get("segment") == "NSE_EQ" and row.get("instrument_key"):
            yield InstrumentRef(
                exchange=Exchange.NSE,
                symbol=row["trading_symbol"],
                token=row["instrument_key"],
                lot_size=int(row.get("lot_size") or 1),
                tick_size=float(row.get("tick_size") or 0.05),
            )


def _state(o: Mapping[str, Any]) -> BrokerOrderState:
    # Status strings are lowercase (/appendix/order-status/). Unknown -> non-terminal OPEN (D19).
    status = _DONE.get(str(o.get("status", "")).lower(), OrderStatus.OPEN)
    filled = int(o.get("filled_quantity") or 0)
    if status is OrderStatus.OPEN and filled > 0 and int(o.get("pending_quantity") or 0) > 0:
        status = OrderStatus.PARTIAL  # doc has no PARTIAL status; derived
    avg = o.get("average_price")
    return BrokerOrderState(
        broker_order_id=str(o["order_id"]),
        status=status,
        filled_qty=filled,
        avg_price=float(avg) if avg else None,
        message=o.get("status_message"),
    )


class UpstoxBroker(HttpBrokerAdapter):
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="upstox",
        name="Upstox",
        auth_mode=AuthMode.OAUTH_REDIRECT,
        # Lowest of conflicting limits: 10/s regular (50/s only for registered algos); reads 50/s.
        rate_limits=RateLimits(orders_per_sec=10, reads_per_sec=50, orders_per_day=None),
        requires_static_ip=True,  # mandatory for order place/modify/cancel from 2026-04-01
        daily_2fa=True,
        tag_max_len=40,
        experimental=True,
        live_tested=False,
        market_order_verified=False,
    )
    base_url: ClassVar[str] = "https://api.upstox.com"
    auth_error_codes: ClassVar[frozenset[str]] = frozenset({"UDAPI100050"})  # invalid token

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        redirect_uri: str | None = None,
    ) -> None:
        super().__init__()
        e = os.environ.get
        self._key = api_key if api_key is not None else e("UPSTOX_API_KEY", "")
        self._secret = api_secret if api_secret is not None else e("UPSTOX_API_SECRET", "")
        self._redirect = redirect_uri or e("UPSTOX_REDIRECT_URI", "")
        self.instruments = InstrumentResolver(self._fetch_master, parse_master)

    async def _fetch_master(self) -> bytes:
        return (await self.client.get(MASTER_URL)).content

    async def startup(self) -> None:
        await self.instruments.load()

    def error_code(self, body: Any) -> str | None:
        # Envelope {"status":"error","errors":[{"errorCode":"UDAPI..."}]} is UNVERIFIED.
        errs = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            if errs[0].get("errorCode"):
                return str(errs[0]["errorCode"])
        return super().error_code(body)

    def order_id_from(self, body: Any) -> str | None:
        data = body.get("data") if isinstance(body, dict) else None
        ids = data.get("order_ids") if isinstance(data, dict) else None
        return str(ids[0]) if ids else None

    def _auth(self, s: BrokerSession) -> dict[str, str]:
        tok = s.access_token.get_secret_value()
        return {"Authorization": f"Bearer {tok}", "Accept": "application/json"}

    async def login_url(self, state: str) -> str:
        q = urlencode({"client_id": self._key, "redirect_uri": self._redirect,
                       "response_type": "code", "state": state})  # fmt: skip
        return f"{self.base_url}/v2/login/authorization/dialog?{q}"  # /authentication/

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        body = await self.request(  # POST /v2/login/authorization/token (/get-token/)
            "POST",
            "/v2/login/authorization/token",
            data={"code": str(params["code"]), "client_id": self._key,
                  "client_secret": self._secret, "redirect_uri": self._redirect,
                  "grant_type": "authorization_code"},  # fmt: skip
        )
        local = datetime.now(UTC).astimezone(IST)
        cut = local.replace(hour=3, minute=25, second=0, microsecond=0)  # token dies 03:30 IST
        exp = (cut if local < cut else cut + timedelta(days=1)).astimezone(UTC)
        tok = SecretStr(body["access_token"])
        return BrokerSession(broker_id=self.meta.id, access_token=tok, expires_at=exp)

    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        body = await self.request(  # GET /v2/portfolio/long-term-holdings (/get-holdings/)
            "GET", "/v2/portfolio/long-term-holdings", headers=self._auth(s)
        )
        rows: list[Holding] = []
        for h in body["data"]:
            qty = int(h["quantity"])
            # UNVERIFIED whether quantity includes t1/collateral: subtract both, floor at 0.
            held = int(h.get("t1_quantity") or 0) + int(h.get("collateral_quantity") or 0)
            rows.append(
                Holding(
                    symbol=h["trading_symbol"],
                    exchange=Exchange.NSE,
                    quantity=qty,
                    sellable_qty=max(0, qty - held),
                    avg_price=h.get("average_price"),
                )
            )
        return rows

    async def get_funds(self, s: BrokerSession) -> Funds | None:
        body = await self.request(  # GET /v2/user/get-funds-and-margin (/get-user-fund-margin/)
            "GET", "/v2/user/get-funds-and-margin", headers=self._auth(s)
        )
        cash = ((body.get("data") or {}).get("equity") or {}).get("available_margin")
        return Funds(available_cash=float(cash)) if cash is not None else None

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        return await self.instruments.resolve(exchange, symbol)

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        ref = await self.resolve_instrument(intent.exchange, intent.symbol)
        market = intent.order_type is OrderType.MARKET
        payload: dict[str, Any] = {
            "quantity": intent.quantity, "product": "D", "validity": "DAY",
            "price": 0 if market else intent.limit_price, "tag": intent.tag,
            "instrument_token": ref.token, "order_type": "MARKET" if market else "LIMIT",
            "transaction_type": intent.side.value, "disclosed_quantity": 0,
            "trigger_price": 0, "is_amo": False,
            "slice": False,  # one order id per leg; slicing would break id tracking
        }  # fmt: skip
        if market:
            payload["market_protection"] = -1  # -1 auto; behaviour when omitted UNVERIFIED
        body = await self.request(  # POST api-hft /v3/order/place (/v3/place-order/)
            "POST", f"{HFT}/v3/order/place", placing=True, json=payload, headers=self._auth(s)
        )
        return str(self.order_id_from(body))

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        body = await self.request(  # GET /v2/order/details (/get-order-details/)
            "GET", "/v2/order/details", params={"order_id": broker_order_id}, headers=self._auth(s)
        )
        data = body["data"]
        return _state(data[-1] if isinstance(data, list) else data)

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        body = await self.request(  # GET /v2/order/retrieve-all, today only (/get-order-book/)
            "GET", "/v2/order/retrieve-all", headers=self._auth(s)
        )
        hits = [o for o in body["data"] if o.get("tag") == tag]
        return _state(hits[-1]) if hits else None
