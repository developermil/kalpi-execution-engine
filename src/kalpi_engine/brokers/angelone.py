"""Angel One SmartAPI adapter over plain httpx (no SDK). Docs: docs/brokers/angelone.md.

UNVERIFIED against a live account: delivery product code, status vocab, order-book tag field,
MPP percentage, funds semantics, 429 shape, client IP headers, whether holdings qty includes T+1.
"""

import base64
import hmac
import json
import os
import struct
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha1
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

IST = timezone(timedelta(hours=5, minutes=30))
# Master lives on a different host from the API (angelone.md "Instrument master").
MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
P = "/rest/secure/angelbroking"


def totp(secret: str, now: float | None = None) -> str:
    """RFC 6238 SHA1/30s/6 digits (stdlib; pyotp is not a dependency). Secret never logged."""
    key = secret.replace(" ", "").upper()
    key += "=" * (-len(key) % 8)
    counter = int((time.time() if now is None else now) // 30)
    mac = hmac.new(base64.b32decode(key), struct.pack(">Q", counter), sha1).digest()
    off = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[off : off + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


def parse_master(raw: bytes) -> Iterable[InstrumentRef]:
    # Verified 2026-10-01: NSE cash "SBIN-EQ" (instrumenttype ""), BSE "SBIN"; tick in paise.
    for r in json.loads(raw):
        if r.get("instrumenttype") or r.get("exch_seg") not in ("NSE", "BSE"):
            continue
        exch, sym = Exchange(r["exch_seg"]), str(r["symbol"])
        if exch is Exchange.NSE:
            if not sym.endswith("-EQ"):
                continue
            sym = sym[:-3]
        yield InstrumentRef(
            exchange=exch, symbol=sym, token=str(r["token"]), lot_size=int(r.get("lotsize") or 1),
            tick_size=float(r.get("tick_size") or 5.0) / 100,
        )  # fmt: skip


def _expiry(now: datetime) -> datetime:
    """Tokens die daily at 00:00 (post 18242); expire 5 minutes early, IST midnight."""
    nxt = (now.astimezone(IST) + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    return (nxt - timedelta(minutes=5)).astimezone(UTC)


def _state(o: Mapping[str, Any]) -> BrokerOrderState:
    # Vocab per angelone.md section 6 (UNVERIFIED complete list); unknown stays non-terminal.
    S = OrderStatus
    status = {"complete": S.FILLED, "rejected": S.REJECTED, "cancelled": S.CANCELLED}.get(
        str(o.get("status") or o.get("orderstatus") or "").lower(), S.OPEN
    )
    filled = int(o.get("filledshares") or 0)  # UNVERIFIED field name
    if status is S.OPEN and filled > 0:
        status = S.PARTIAL
    avg, oid = o.get("averageprice"), o.get("uniqueorderid") or o["orderid"]
    return BrokerOrderState(
        broker_order_id=str(oid), status=status, filled_qty=filled,
        avg_price=float(avg) if avg else None,
    )  # fmt: skip


class AngelOneBroker(HttpBrokerAdapter):
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="angelone", name="Angel One (SmartAPI)", auth_mode=AuthMode.CREDENTIALS_TOTP,
        credential_fields=("client_code", "pin", "totp_secret"),
        # Lowest of conflicting limits: 20/s (table) vs 10/s vs 9/s; holdings/orderbook 1/s.
        rate_limits=RateLimits(orders_per_sec=9, reads_per_sec=1),
        requires_static_ip=True,  # orders only from registered static IP from 2026-04-01
        daily_2fa=True, tag_max_len=20,  # ordertag max 20 (post 17334)
        experimental=True, live_tested=False, market_order_verified=False,
    )  # fmt: skip
    base_url: ClassVar[str] = "https://apiconnect.angelone.in"
    auth_error_codes: ClassVar[frozenset[str]] = frozenset({"AG8001"})  # Invalid Token

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__()
        self._api_key = api_key if api_key is not None else os.environ.get("ANGELONE_API_KEY", "")
        self.instruments = InstrumentResolver(self._fetch_master, parse_master)

    async def _fetch_master(self) -> bytes:
        return (await self.client.get(MASTER_URL)).content

    async def startup(self) -> None:
        await self.instruments.load()

    def _headers(self, s: BrokerSession | None = None) -> dict[str, str]:
        # Header set from SDK smartConnect.py; IP/MAC values UNVERIFIED placeholders.
        ip, mac = os.environ.get("ANGELONE_PUBLIC_IP", "127.0.0.1"), "00:00:00:00:00:00"
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        h |= {"X-UserType": "USER", "X-SourceID": "WEB", "X-PrivateKey": self._api_key}
        h |= {"X-ClientLocalIP": ip, "X-ClientPublicIP": ip, "X-MACAddress": mac}
        if s is not None:
            h["Authorization"] = f"Bearer {s.access_token.get_secret_value()}"
        return h

    def order_id_from(self, body: Any) -> str | None:
        d = body.get("data") if isinstance(body, dict) else None
        oid = d.get("uniqueorderid") or d.get("orderid") if isinstance(d, dict) else None
        return str(oid) if oid else None

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        def cred(k: str) -> str:
            return str(params.get(k) or os.environ.get(f"ANGELONE_{k.upper()}", ""))

        code, pin, secret = cred("client_code"), cred("pin"), cred("totp_secret")
        otp = str(params["totp"]) if params.get("totp") else totp(secret)
        body = await self.request(  # POST loginByPassword (SDK smartConnect.py)
            "POST", "/rest/auth/angelbroking/user/v1/loginByPassword", headers=self._headers(),
            json={"clientcode": code, "password": pin, "totp": otp},
        )  # fmt: skip
        d = body["data"]
        return BrokerSession(
            broker_id=self.meta.id, access_token=SecretStr(d["jwtToken"]),
            expires_at=_expiry(datetime.now(UTC)),
            extra={"refresh_token": d.get("refreshToken", "")},  # for generateTokens (SDK)
        )  # fmt: skip

    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        body = await self.request("GET", f"{P}/portfolio/v1/getHolding", headers=self._headers(s))
        rows: list[Holding] = []
        for h in body.get("data") or []:
            qty = int(h["quantity"])
            # Conservative (D19): T+1 / collateral may or may not be inside quantity (UNVERIFIED).
            held = int(h.get("t1quantity") or 0) + int(h.get("collateralquantity") or 0)
            rows.append(
                Holding(
                    symbol=str(h["tradingsymbol"]).removesuffix("-EQ"),
                    exchange=Exchange(h.get("exchange") or "NSE"),
                    quantity=qty,
                    sellable_qty=max(0, qty - held),
                    avg_price=h.get("averageprice"),
                )  # fmt: skip
            )
        return rows

    async def get_funds(self, s: BrokerSession) -> Funds | None:
        body = await self.request("GET", f"{P}/user/v1/getRMS", headers=self._headers(s))
        cash = (body.get("data") or {}).get("availablecash")  # semantics UNVERIFIED (post 12766)
        return Funds(available_cash=float(cash)) if cash is not None else None

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        return await self.instruments.resolve(exchange, symbol)

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        ref = await self.resolve_instrument(intent.exchange, intent.symbol)
        market = intent.order_type is OrderType.MARKET
        payload: dict[str, Any] = {
            "variety": "NORMAL",
            "tradingsymbol": f"{ref.symbol}-EQ" if intent.exchange is Exchange.NSE else ref.symbol,
            "symboltoken": ref.token,
            "transactiontype": intent.side.value,
            "exchange": intent.exchange.value,
            "ordertype": intent.order_type.value,
            "producttype": "DELIVERY",  # UNVERIFIED code (README only shows INTRADAY)
            "duration": "DAY",
            # MARKET must carry price "0" (post 15509); non-zero is rejected. Broker converts to
            # MPP from 2026-04-01 (post 18960); no client MPP param is documented (UNVERIFIED).
            "price": "0" if market else str(intent.limit_price),
            "quantity": str(intent.quantity),
            "ordertag": intent.tag,
        }
        body = await self.request(  # POST placeOrder (SDK README)
            "POST", f"{P}/order/v1/placeOrder", placing=True, json=payload, headers=self._headers(s)
        )
        return str(self.order_id_from(body))

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        body = await self.request(  # GET details/{uniqueorderid} (SDK routes)
            "GET", f"{P}/order/v1/details/{broker_order_id}", headers=self._headers(s)
        )
        return _state({"orderid": broker_order_id, **body["data"]})

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        body = await self.request("GET", f"{P}/order/v1/getOrderBook", headers=self._headers(s))
        hits = [o for o in body.get("data") or [] if o.get("ordertag") == tag]  # field UNVERIFIED
        return _state(hits[-1]) if hits else None
