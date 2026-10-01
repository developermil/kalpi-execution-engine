import pytest

from kalpi_engine.brokers.base import BrokerSession
from kalpi_engine.brokers.paper import DEFAULT_CASH, PRICES, Faults, PaperBroker
from kalpi_engine.domain.enums import Exchange, OrderStatus, OrderType, Phase, Side
from kalpi_engine.domain.errors import (
    AmbiguousSubmit,
    AuthExpired,
    BrokerRejected,
    InsufficientFunds,
    RateLimited,
    TransientError,
    UnknownSymbol,
)
from kalpi_engine.domain.models import OrderIntent


def intent(
    symbol: str = "INFY",
    side: Side = Side.BUY,
    qty: int = 2,
    tag: str = "KTAG0001",
    order_type: OrderType = OrderType.MARKET,
    limit: float | None = None,
) -> OrderIntent:
    return OrderIntent(
        leg_id="leg-1", phase=Phase(side.value), symbol=symbol, exchange=Exchange.NSE,
        side=side, quantity=qty, order_type=order_type, limit_price=limit, tag=tag,
    )  # fmt: skip


async def setup(
    faults: Faults | None = None, **params: object
) -> tuple[PaperBroker, BrokerSession]:
    b = PaperBroker(faults)
    return b, await b.create_session(params)


async def test_market_buy_fills_and_updates_holdings_and_cash() -> None:
    b, s = await setup()
    oid = await b.place_order(s, intent(qty=2))
    st = await b.get_order(s, oid)
    assert (st.status, st.filled_qty, st.avg_price) == (OrderStatus.FILLED, 2, PRICES["INFY"])
    assert [(h.symbol, h.quantity) for h in await b.get_holdings(s)] == [("INFY", 2)]
    assert (await b.get_funds(s)).available_cash == DEFAULT_CASH - 2 * PRICES["INFY"]


async def test_sell_respects_sellable_qty() -> None:
    b, s = await setup(holdings={"TCS": 5})
    await b.place_order(s, intent("TCS", Side.SELL, 3, tag="KSELL001"))
    with pytest.raises(BrokerRejected):
        await b.place_order(s, intent("TCS", Side.SELL, 3, tag="KSELL002"))
    (h,) = await b.get_holdings(s)
    assert (h.quantity, h.sellable_qty) == (2, 2)


async def test_find_order_by_tag_and_no_dedupe() -> None:
    b, s = await setup()
    first = await b.place_order(s, intent(tag="KDUP0001"))
    second = await b.place_order(s, intent(tag="KDUP0001"))
    assert first != second  # real brokers don't dedupe tags; neither does Paper
    found = await b.find_order_by_tag(s, "KDUP0001")
    assert found is not None and found.broker_order_id == first
    assert await b.find_order_by_tag(s, "KMISSING") is None


async def test_tags_are_scoped_to_the_session() -> None:
    b, s1 = await setup()
    s2 = await b.create_session({})
    await b.place_order(s1, intent(tag="KSCOPE01"))
    assert await b.find_order_by_tag(s2, "KSCOPE01") is None


async def test_retry_safe_faults_create_no_order() -> None:
    b, s = await setup(Faults(connect_error_next=1, rate_limit_next=1))
    with pytest.raises(TransientError):
        await b.place_order(s, intent(tag="KRETRY01"))
    with pytest.raises(RateLimited):
        await b.place_order(s, intent(tag="KRETRY01"))
    assert await b.find_order_by_tag(s, "KRETRY01") is None
    assert await b.place_order(s, intent(tag="KRETRY01"))


async def test_timeout_after_accept_then_late_tag_hit() -> None:
    b, s = await setup(Faults(timeout_after_accept_next=1, hide_from_tag_lookups=1))
    with pytest.raises(AmbiguousSubmit):
        await b.place_order(s, intent(tag="KAMBIG01"))
    assert await b.find_order_by_tag(s, "KAMBIG01") is None  # first lookup misses
    late = await b.find_order_by_tag(s, "KAMBIG01")
    assert late is not None and late.status is OrderStatus.FILLED


async def test_reject_partial_and_delayed_fill() -> None:
    f = Faults(reject_symbols={"SBIN"}, partial_fill={"ITC": 3}, fill_after_polls=2)
    b, s = await setup(f)
    with pytest.raises(BrokerRejected):
        await b.place_order(s, intent("SBIN"))
    oid = await b.place_order(s, intent("ITC", qty=10, tag="KPART001"))
    assert (await b.get_order(s, oid)).status is OrderStatus.OPEN
    st = await b.get_order(s, oid)
    assert (st.status, st.filled_qty) == (OrderStatus.PARTIAL, 3)


async def test_limit_not_crossing_stays_open_then_cancel_releases_cash() -> None:
    b, s = await setup()
    oid = await b.place_order(s, intent(order_type=OrderType.LIMIT, limit=1.0, qty=4))
    assert (await b.get_order(s, oid)).status is OrderStatus.OPEN
    await b.cancel_order(s, oid)
    assert (await b.get_order(s, oid)).status is OrderStatus.CANCELLED
    assert (await b.get_funds(s)).available_cash == DEFAULT_CASH


async def test_unknown_symbol_funds_and_auth() -> None:
    b, s = await setup(cash=100.0)
    with pytest.raises(UnknownSymbol):
        await b.place_order(s, intent("NOPE"))
    with pytest.raises(InsufficientFunds):
        await b.place_order(s, intent("RELIANCE"))
    b.faults.expire_sessions = True
    with pytest.raises(AuthExpired):
        await b.get_holdings(s)


async def test_demo_profile_seeds_holdings() -> None:
    b, s = await setup(profile="demo")
    assert {h.symbol for h in await b.get_holdings(s)} == {"RELIANCE", "TCS", "INFY", "ITC"}
    b2, s2 = await setup()
    assert await b2.get_holdings(s2) == []
    assert s.access_token.get_secret_value() not in repr(s)
