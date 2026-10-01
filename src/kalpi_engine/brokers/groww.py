"""Groww Trade API adapter over plain httpx. Docs: docs/brokers/groww.md.

UNVERIFIED live: token endpoint fields, auth-expiry codes, MARKET protection (none documented),
partial-fill reporting, instrument CSV refresh cadence.
"""

import csv
import hashlib
import io
import os
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta, timezone
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
from kalpi_engine.domain.errors import BrokerRejected
from kalpi_engine.domain.models import BrokerOrderState, Funds, Holding, OrderIntent

_H = {"Accept": "application/json", "X-API-VERSION": "1.0"}  # /curl docs
IST = timezone(timedelta(hours=5, minutes=30))
MASTER_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"  # /instruments docs


def checksum(secret: str, timestamp: str) -> str:
    # Approval flow checksum = SHA256(secret + epoch-timestamp string) [/curl docs]
    return hashlib.sha256(f"{secret}{timestamp}".encode()).hexdigest()


def parse_master(raw: bytes) -> Iterable[InstrumentRef]:
    # instrument.csv cols: exchange, exchange_token, trading_symbol, segment, lot_size, tick_size
    for row in csv.DictReader(io.StringIO(raw.decode())):
        if row.get("segment") != "CASH" or row.get("exchange") not in ("NSE", "BSE"):
            continue
        yield InstrumentRef(
            exchange=Exchange(row["exchange"]), symbol=row["trading_symbol"],
            token=row["exchange_token"], lot_size=int(row.get("lot_size") or 1),
            tick_size=float(row.get("tick_size") or 0.05),
        )  # fmt: skip


def _expiry(now: datetime) -> datetime:
    """Token expires daily 06:00 IST [/curl docs]; be conservative with 05:55."""
    local = now.astimezone(IST)
    cut = local.replace(hour=5, minute=55, second=0, microsecond=0)
    return (cut if local < cut else cut + timedelta(days=1)).astimezone(UTC)


def _state(o: Mapping[str, Any]) -> BrokerOrderState:
    # /annexures vocab. Unknown values (OPEN, PENDING, new) stay non-terminal (D19), never FILLED.
    raw = str(o.get("order_status", "")).upper()
    filled = int(o.get("filled_quantity") or 0)
    qty = o.get("quantity")
    awaited = raw == "DELIVERY_AWAITED" and qty is not None and filled == int(qty)
    if raw in ("EXECUTED", "COMPLETED") or awaited:
        status = OrderStatus.FILLED
    elif raw in ("REJECTED", "FAILED"):
        status = OrderStatus.REJECTED
    elif raw == "CANCELLED":
        status = OrderStatus.CANCELLED
    else:
        status = OrderStatus.PARTIAL if filled > 0 else OrderStatus.OPEN  # UNVERIFIED partials
    avg = o.get("average_fill_price")
    return BrokerOrderState(
        broker_order_id=str(o["groww_order_id"]), status=status, filled_qty=filled,
        avg_price=float(avg) if avg else None,
    )  # fmt: skip


class GrowwBroker(HttpBrokerAdapter):
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="groww",
        name="Groww Trade API",
        auth_mode=AuthMode.CREDENTIALS_TOTP,
        credential_fields=("totp",),  # or "access_token" pasted from the Groww web UI
        # Orders 10/s, 250/min; non-trading reads 20/s [/curl docs]; lowest = per-minute pace.
        rate_limits=RateLimits(orders_per_sec=250 / 60, reads_per_sec=20),
        requires_static_ip=True,
        daily_2fa=True,
        tag_max_len=20,  # order_reference_id: 8-20 alphanumeric, max 2 hyphens [/orders docs]
        experimental=True,
        live_tested=False,
        market_order_verified=False,  # no MPP/protection param documented (UNVERIFIED)
    )
    base_url: ClassVar[str] = "https://api.groww.in"
    auth_error_codes: ClassVar[frozenset[str]] = frozenset()  # GA### expiry code UNVERIFIED

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        super().__init__()
        self._api_key = api_key or os.environ.get("GROWW_API_KEY", "")
        self._secret = api_secret or os.environ.get("GROWW_API_SECRET", "")
        self.instruments = InstrumentResolver(self._fetch_master, parse_master)

    async def _fetch_master(self) -> bytes:
        return (await self.client.get(MASTER_URL)).content  # public CSV; cadence UNVERIFIED

    async def startup(self) -> None:
        await self.instruments.load()

    def _auth(self, s: BrokerSession) -> dict[str, str]:
        return {"Authorization": f"Bearer {s.access_token.get_secret_value()}", **_H}

    def error_code(self, body: Any) -> str | None:
        # Failure envelope {"status":"FAILURE","error":{"code":"GA###"}} [/curl docs]
        if isinstance(body, dict) and body.get("status") == "FAILURE":
            return str((body.get("error") or {}).get("code") or "FAILURE")
        return super().error_code(body)

    def order_id_from(self, body: Any) -> str | None:
        p = body.get("payload") if isinstance(body, dict) else None
        return str(p["groww_order_id"]) if isinstance(p, dict) and p.get("groww_order_id") else None

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        if params.get("access_token"):  # token generated in the Groww web UI
            token = str(params["access_token"])
        else:
            # POST /v1/token/api/access (/curl docs); body/response field names UNVERIFIED
            ts = str(int(time.time()))
            body: dict[str, Any] = {"key_type": "totp", "totp": str(params.get("totp"))}
            if not params.get("totp"):
                body = {"key_type": "approval", "checksum": checksum(self._secret, ts),
                        "timestamp": ts}  # fmt: skip
            hdr = {"Authorization": f"Bearer {self._api_key}", **_H}
            resp = await self.request("POST", "/v1/token/api/access", headers=hdr, json=body)
            token = str(((resp or {}).get("payload") or resp or {}).get("token", ""))
        return BrokerSession(
            broker_id=self.meta.id, access_token=SecretStr(token),
            expires_at=_expiry(datetime.now(UTC)),
        )  # fmt: skip

    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        body = await self.request("GET", "/v1/holdings/user", headers=self._auth(s))  # /portfolio
        rows: list[Holding] = []
        for h in body["payload"]["holdings"]:
            qty, free = int(h["quantity"]), int(h.get("demat_free_quantity") or 0)  # D19 (inferred)
            rows.append(Holding(
                symbol=h["trading_symbol"], exchange=Exchange.NSE,  # no exchange field (UNVERIFIED)
                quantity=qty, sellable_qty=max(0, min(free, qty)), avg_price=h.get("average_price"),
            ))  # fmt: skip
        return rows

    async def get_funds(self, s: BrokerSession) -> Funds | None:
        body = await self.request("GET", "/v1/margins/detail/user", headers=self._auth(s))
        cash = (body.get("payload") or {}).get("clear_cash")  # /margin docs
        return None if cash is None else Funds(available_cash=float(cash))

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        return await self.instruments.resolve(exchange, symbol)

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        await self.resolve_instrument(intent.exchange, intent.symbol)
        payload: dict[str, Any] = {
            "trading_symbol": intent.symbol,
            "quantity": intent.quantity,
            "validity": "DAY",  # only documented value
            "exchange": intent.exchange.value,
            "segment": "CASH",
            "product": intent.product.value,
            "order_type": intent.order_type.value,
            "transaction_type": intent.side.value,
            "order_reference_id": intent.tag,  # lookup key only; no idempotency (UNVERIFIED)
        }
        if intent.order_type is not OrderType.MARKET:
            payload["price"] = intent.limit_price
        # MARKET: no protection/MPP/slicing param is documented (UNVERIFIED); none sent.
        body = await self.request(  # POST /v1/order/create (/orders docs)
            "POST", "/v1/order/create", placing=True, json=payload, headers=self._auth(s)
        )
        return str(self.order_id_from(body))

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        body = await self.request(  # GET /v1/order/status/{id} (/orders docs)
            "GET", f"/v1/order/status/{broker_order_id}", params={"segment": "CASH"},
            headers=self._auth(s),
        )  # fmt: skip
        return _state({"groww_order_id": broker_order_id, **body["payload"]})

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        try:  # GET /v1/order/status/reference/{ref} (/orders docs); not-found shape UNVERIFIED
            body = await self.request(
                "GET", f"/v1/order/status/reference/{tag}", params={"segment": "CASH"},
                headers=self._auth(s),
            )  # fmt: skip
        except BrokerRejected:
            return None  # FAILURE envelope on unknown reference
        return _state(body["payload"])
