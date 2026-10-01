"""Fyers API v3 adapter over plain httpx (D25). Docs: docs/brokers/fyers.md.

EXPERIMENTAL, never live-tested. UNVERIFIED: order/holdings/funds paths, enum codes, field names,
auth header. D19 defaults: unknown status is OPEN.
"""

import csv
import hashlib
import io
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, ClassVar
from urllib.parse import quote

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
from kalpi_engine.domain.enums import Exchange, OrderStatus, OrderType, Side
from kalpi_engine.domain.errors import BrokerRejected
from kalpi_engine.domain.models import BrokerOrderState, Funds, Holding, OrderIntent

IST = timezone(timedelta(hours=5, minutes=30))
MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"  # inspected 2026-10-01 (docs)


def app_id_hash(app_id: str, secret: str) -> str:
    # validate-authcode appIdHash = SHA256("app_id:secret")  [community thread, docs item 1]
    return hashlib.sha256(f"{app_id}:{secret}".encode()).hexdigest()


def parse_master(raw: bytes) -> Iterable[InstrumentRef]:
    # Headerless 21-col CSV (docs, verified by inspection): [3]lot [4]tick [9]order ticker
    # [13]short symbol. Require exact `NSE:<short>-EQ` in col 10, else UNKNOWN_SYMBOL.
    for r in csv.reader(io.StringIO(raw.decode())):
        if len(r) >= 14 and r[9] == f"NSE:{r[13]}-EQ":
            yield InstrumentRef(
                exchange=Exchange.NSE, symbol=r[13], token=r[9],
                lot_size=int(r[3] or 1), tick_size=float(r[4] or 0.05),
            )  # fmt: skip


def _expiry(now: datetime) -> datetime:
    """Tokens expire daily (docs); UNVERIFIED hour, so be conservative: next IST midnight - 5m."""
    local = now.astimezone(IST)
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (nxt - timedelta(minutes=5)).astimezone(UTC)


def _state(o: Mapping[str, Any]) -> BrokerOrderState:
    # Status codes UNVERIFIED: none mapped terminal; leg stays OPEN, UNKNOWN at timeout (D19).
    filled = int(o.get("filledQty") or 0)
    avg = o.get("tradedPrice")
    return BrokerOrderState(
        broker_order_id=str(o["id"]),
        status=OrderStatus.PARTIAL if filled > 0 else OrderStatus.OPEN,
        filled_qty=filled,
        avg_price=float(avg) if avg else None,
    )


class FyersBroker(HttpBrokerAdapter):
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="fyers",
        name="Fyers (API v3)",
        auth_mode=AuthMode.OAUTH_REDIRECT,
        # 10/s, 200/min (docs item 8); lowest of conflicting daily caps (10k vs 100k) -> 10000
        rate_limits=RateLimits(orders_per_sec=10, reads_per_sec=3, orders_per_day=10000),
        requires_static_ip=True,
        daily_2fa=True,
        tag_max_len=16,  # UNVERIFIED real max; shorter than any peer (D19)
        experimental=True,
        live_tested=False,
        market_order_verified=False,
    )
    base_url: ClassVar[str] = "https://api-t1.fyers.in"  # fyers-skills (docs item 1)
    auth_error_codes: ClassVar[frozenset[str]] = frozenset({"-16"})  # "Could not authenticate"

    def __init__(
        self, app_id: str | None = None, secret: str | None = None, redirect_uri: str | None = None
    ) -> None:
        super().__init__()
        env = os.environ
        self._app_id = app_id or env.get("FYERS_APP_ID", "")
        self._secret = secret or env.get("FYERS_SECRET_ID", "")
        self._redirect = redirect_uri or env.get("FYERS_REDIRECT_URI", "")
        self.instruments = InstrumentResolver(self._fetch_master, parse_master)

    async def _fetch_master(self) -> bytes:
        return (await self.client.get(MASTER_URL)).content  # public CSV, no auth

    async def startup(self) -> None:
        await self.instruments.load()
    def error_code(self, body: Any) -> str | None:
        # Error shape {"s":"error","code":-16,"message":...} (docs item 12); HTTP status UNVERIFIED
        if isinstance(body, dict) and body.get("s") == "error":
            return str(body.get("code") or body.get("message") or "error")
        return None

    def order_id_from(self, body: Any) -> str | None:
        oid = body.get("id") if isinstance(body, dict) else None  # UNVERIFIED response field
        return str(oid) if oid else None

    def _auth(self, s: BrokerSession) -> dict[str, str]:
        # `Authorization: app_id:access_token` (UNVERIFIED, docs item 1)
        return {"Authorization": f"{self._app_id}:{s.access_token.get_secret_value()}"}

    async def login_url(self, state: str) -> str:
        # UNVERIFIED path/params (docs item 1)
        return (
            f"{self.base_url}/api/v3/generate-authcode?client_id={quote(self._app_id)}"
            f"&redirect_uri={quote(self._redirect, safe='')}&response_type=code"
            f"&state={quote(state)}"
        )

    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        body = await self.request(  # POST validate-authcode (community, docs item 1)
            "POST",
            "/api/v3/validate-authcode",
            json={
                "grant_type": "authorization_code",
                "appIdHash": app_id_hash(self._app_id, self._secret),
                "code": str(params.get("auth_code") or params["code"]),
            },
        )
        return BrokerSession(
            broker_id=self.meta.id,
            access_token=SecretStr(body["access_token"]),
            expires_at=_expiry(datetime.now(UTC)),
        )

    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        body = await self.request("GET", "/api/v3/holdings", headers=self._auth(s))  # UNVERIFIED
        rows = []
        for h in body["holdings"]:  # UNVERIFIED container + field names
            # Only parse names we know; if none identifies quantity, raise (nothing is sent).
            cands = [int(h[k]) for k in ("quantity", "remainingQuantity") if h.get(k) is not None]
            if not cands:
                raise BrokerRejected(f"holdings: quantity not identified for {h.get('symbol')}")
            qty = min(cands)  # smallest candidate; T1 is never counted
            sym = str(h["symbol"]).split(":")[-1].removesuffix("-EQ")
            rows.append(
                Holding(
                    symbol=sym, exchange=Exchange.NSE, quantity=qty, sellable_qty=qty,
                    avg_price=h.get("costPrice"),
                )  # fmt: skip
            )
        return rows

    async def get_funds(self, s: BrokerSession) -> Funds | None:
        body = await self.request("GET", "/api/v3/funds", headers=self._auth(s))  # UNVERIFIED
        for e in body.get("fund_limit") or []:  # titles per docs item 3
            if e.get("title") == "Clear Balance" and e.get("equityAmount") is not None:
                return Funds(available_cash=float(e["equityAmount"]))  # which entry: UNVERIFIED
        return None

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        return await self.instruments.resolve(exchange, symbol)

    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        ref = await self.resolve_instrument(intent.exchange, intent.symbol)
        market = intent.order_type is OrderType.MARKET
        payload: dict[str, Any] = {
            "symbol": ref.token,
            "qty": intent.quantity,
            "type": 2 if market else 1,  # enum codes UNVERIFIED (1=limit, 2=market)
            "side": 1 if intent.side is Side.BUY else -1,  # UNVERIFIED
            "productType": intent.product.value,  # CNC/MARGIN/INTRADAY (docs item 4)
            "limitPrice": 0 if market else intent.limit_price,
            "stopPrice": 0, "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
            "orderTag": intent.tag,
        }
        # MARKET auto-converts to MPP (docs item 10); no slicing/protection param known: UNVERIFIED.
        body = await self.request(  # POST order path UNVERIFIED (docs item 4)
            "POST", "/api/v3/orders/sync", placing=True, json=payload, headers=self._auth(s)
        )
        return str(self.order_id_from(body))

    async def _book(self, s: BrokerSession) -> list[Mapping[str, Any]]:
        # GET /orders = full order book; no multi-id filter in v3, filter client-side (docs item 6)
        body = await self.request("GET", "/api/v3/orders", headers=self._auth(s))
        return list(body.get("orderBook") or [])  # UNVERIFIED container name

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        for o in await self._book(s):
            if str(o.get("id")) == broker_order_id:
                return _state(o)
        return BrokerOrderState(broker_order_id=broker_order_id, status=OrderStatus.OPEN)

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        hits = [o for o in await self._book(s) if o.get("orderTag") == tag]  # echo UNVERIFIED
        return _state(hits[-1]) if hits else None
