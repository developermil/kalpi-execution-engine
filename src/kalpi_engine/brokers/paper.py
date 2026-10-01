"""In-memory Paper broker with deterministic fills and fault injection (D15/D16).

Like real brokers it does NOT dedupe by tag: placing the same tag twice creates two orders,
so engine tests catch double-sends.
"""

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import SecretStr

from kalpi_engine.brokers.base import (
    AuthMode,
    BrokerAdapter,
    BrokerMeta,
    BrokerSession,
    InstrumentRef,
    RateLimits,
)
from kalpi_engine.domain.enums import Exchange, OrderStatus, OrderType, Side
from kalpi_engine.domain.errors import (
    AmbiguousSubmit,
    AuthExpired,
    BrokerRejected,
    InsufficientFunds,
    InvalidOrder,
    RateLimited,
    TransientError,
    UnknownSymbol,
)
from kalpi_engine.domain.models import BrokerOrderState, Funds, Holding, OrderIntent

PRICES: dict[str, float] = {
    "RELIANCE": 2900.0, "TCS": 3900.0, "INFY": 1500.0, "HDFCBANK": 1650.0,
    "ICICIBANK": 1200.0, "SBIN": 800.0, "ITC": 450.0, "LT": 3600.0,
    "BHARTIARTL": 1500.0, "AXISBANK": 1150.0, "KOTAKBANK": 1800.0, "WIPRO": 520.0,
}  # fmt: skip
DEMO_HOLDINGS: dict[str, int] = {"RELIANCE": 10, "TCS": 5, "INFY": 8, "ITC": 40}
DEFAULT_CASH = 1_000_000.0


@dataclass
class Faults:
    """Knobs for engine tests. Counters are consumed one per place_order / lookup."""

    rate_limit_next: int = 0  # place_order raises RateLimited (nothing created)
    connect_error_next: int = 0  # place_order raises TransientError (nothing created)
    timeout_after_accept_next: int = 0  # order IS created, then AmbiguousSubmit is raised
    timeout_before_accept_next: int = 0  # AmbiguousSubmit raised, order NOT created
    hide_from_tag_lookups: int = 0  # find_order_by_tag misses N times (late fill)
    reject_symbols: set[str] = field(default_factory=set)
    partial_fill: dict[str, int] = field(default_factory=dict)  # symbol -> qty that fills
    fill_after_polls: int = 0  # get_order calls before an order fills (0 = at placement)
    latency_s: float = 0.0
    expire_sessions: bool = False


@dataclass
class _Account:
    cash: float
    qty: dict[str, int]
    sellable: dict[str, int]


@dataclass
class _Order:
    id: str
    account: str
    intent: OrderIntent
    price: float
    status: OrderStatus = OrderStatus.OPEN
    filled: int = 0
    polls: int = 0


class PaperBroker(BrokerAdapter):
    meta: ClassVar[BrokerMeta] = BrokerMeta(
        id="paper",
        name="Paper (simulated)",
        auth_mode=AuthMode.NONE,
        rate_limits=RateLimits(orders_per_sec=10, reads_per_sec=10),
        tag_max_len=20,
        experimental=False,
        live_tested=False,  # simulated; never talks to a real broker
        market_order_verified=True,
    )

    def __init__(self, faults: Faults | None = None) -> None:
        self.faults = faults or Faults()
        self._accounts: dict[str, _Account] = {}
        self._orders: dict[str, _Order] = {}
        self.place_calls = 0

    # --- session / reads ------------------------------------------------------------
    async def create_session(self, params: Mapping[str, Any]) -> BrokerSession:
        """params: optional `holdings` {symbol: qty}, `profile` ('demo' seeds holdings), `cash`."""
        seed: Mapping[str, int] = params.get("holdings") or (
            DEMO_HOLDINGS if params.get("profile") == "demo" else {}
        )
        token = f"paper-{uuid.uuid4().hex}"
        self._accounts[token] = _Account(
            cash=float(params.get("cash", DEFAULT_CASH)), qty=dict(seed), sellable=dict(seed)
        )
        return BrokerSession(broker_id=self.meta.id, access_token=SecretStr(token))

    async def get_holdings(self, s: BrokerSession) -> list[Holding]:
        acct = await self._account(s)
        return [
            Holding(
                symbol=sym, exchange=Exchange.NSE, quantity=q,
                sellable_qty=acct.sellable.get(sym, 0), avg_price=PRICES.get(sym),
            )
            for sym, q in sorted(acct.qty.items())
            if q > 0
        ]  # fmt: skip

    async def get_funds(self, s: BrokerSession) -> Funds:
        return Funds(available_cash=(await self._account(s)).cash)

    async def resolve_instrument(self, exchange: Exchange, symbol: str) -> InstrumentRef:
        if symbol not in PRICES:
            raise UnknownSymbol(f"{exchange}:{symbol}")
        return InstrumentRef(exchange=exchange, symbol=symbol, token=f"PAPER-{symbol}")

    # --- orders -----------------------------------------------------------------------
    async def place_order(self, s: BrokerSession, intent: OrderIntent) -> str:
        acct = await self._account(s)
        self.place_calls += 1
        f = self.faults
        if f.connect_error_next:
            f.connect_error_next -= 1
            raise TransientError("paper: injected connect error")
        if f.rate_limit_next:
            f.rate_limit_next -= 1
            raise RateLimited("paper: injected 429", retry_after=0.0)
        if f.timeout_before_accept_next:
            f.timeout_before_accept_next -= 1
            raise AmbiguousSubmit("paper: injected timeout, order never reached the book")
        if intent.symbol in f.reject_symbols:
            raise BrokerRejected(f"paper: injected reject for {intent.symbol}")
        await self.resolve_instrument(intent.exchange, intent.symbol)
        price = PRICES[intent.symbol]
        block = intent.quantity * (intent.limit_price or price)
        if intent.side is Side.SELL:
            if acct.sellable.get(intent.symbol, 0) < intent.quantity:
                raise BrokerRejected(f"paper: insufficient sellable qty for {intent.symbol}")
            acct.sellable[intent.symbol] -= intent.quantity
        else:
            if acct.cash < block:
                raise InsufficientFunds(f"paper: need {block:.2f}, have {acct.cash:.2f}")
            acct.cash -= block
        order = _Order(
            id=f"P{len(self._orders) + 1:08d}",
            account=s.access_token.get_secret_value(),
            intent=intent,
            price=price,
        )
        self._orders[order.id] = order
        if f.fill_after_polls == 0:
            self._try_fill(order)
        if f.timeout_after_accept_next:
            f.timeout_after_accept_next -= 1
            raise AmbiguousSubmit("paper: injected timeout after accept")
        return order.id

    async def get_order(self, s: BrokerSession, broker_order_id: str) -> BrokerOrderState:
        order = await self._order(s, broker_order_id)
        order.polls += 1
        if order.polls >= self.faults.fill_after_polls:
            self._try_fill(order)
        return self._state(order)

    async def find_order_by_tag(self, s: BrokerSession, tag: str) -> BrokerOrderState | None:
        token = await self._account_token(s)
        if self.faults.hide_from_tag_lookups:
            self.faults.hide_from_tag_lookups -= 1
            return None
        for order in self._orders.values():
            if order.account == token and order.intent.tag == tag:
                return self._state(order)
        return None

    async def cancel_order(self, s: BrokerSession, broker_order_id: str) -> None:
        order = await self._order(s, broker_order_id)
        if order.status in (OrderStatus.OPEN, OrderStatus.PARTIAL):
            order.status = OrderStatus.CANCELLED
            self._release(order, order.intent.quantity - order.filled)

    # --- internals ----------------------------------------------------------------------
    async def _account_token(self, s: BrokerSession) -> str:
        if self.faults.latency_s:
            await asyncio.sleep(self.faults.latency_s)
        token = s.access_token.get_secret_value()
        if self.faults.expire_sessions or token not in self._accounts:
            raise AuthExpired("paper: session expired or unknown")
        return token

    async def _account(self, s: BrokerSession) -> _Account:
        return self._accounts[await self._account_token(s)]

    async def _order(self, s: BrokerSession, broker_order_id: str) -> _Order:
        token = await self._account_token(s)
        order = self._orders.get(broker_order_id)
        if order is None or order.account != token:
            raise InvalidOrder(f"paper: unknown order {broker_order_id}", code="ORDER_NOT_FOUND")
        return order

    def _try_fill(self, order: _Order) -> None:
        if order.status is not OrderStatus.OPEN or order.filled:
            return
        it = order.intent
        if it.order_type is OrderType.LIMIT and it.limit_price is not None:
            crosses = it.limit_price >= order.price if it.side is Side.BUY else (
                it.limit_price <= order.price)  # fmt: skip
            if not crosses:
                return
        qty = min(self.faults.partial_fill.get(it.symbol, it.quantity), it.quantity)
        acct = self._accounts[order.account]
        order.filled = qty
        if it.side is Side.BUY:
            blocked = qty * (it.limit_price or order.price)
            acct.cash += blocked - qty * order.price  # refund price improvement
            acct.qty[it.symbol] = acct.qty.get(it.symbol, 0) + qty
            acct.sellable[it.symbol] = acct.sellable.get(it.symbol, 0) + qty
        else:
            acct.qty[it.symbol] -= qty
            acct.cash += qty * order.price
        order.status = OrderStatus.FILLED if qty == it.quantity else OrderStatus.PARTIAL

    def _release(self, order: _Order, unfilled: int) -> None:
        acct = self._accounts[order.account]
        it = order.intent
        if it.side is Side.SELL:
            acct.sellable[it.symbol] += unfilled
        else:
            acct.cash += unfilled * (it.limit_price or order.price)

    @staticmethod
    def _state(order: _Order) -> BrokerOrderState:
        return BrokerOrderState(
            broker_order_id=order.id,
            status=order.status,
            filled_qty=order.filled,
            avg_price=order.price if order.filled else None,
        )
