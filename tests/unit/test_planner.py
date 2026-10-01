"""Planner: validation V1-V11 (SPEC §2) and sells-then-buys planning (SPEC §5 step 2, D7)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kalpi_engine.domain.enums import Exchange, OrderType, Phase, Side
from kalpi_engine.domain.models import Holding
from kalpi_engine.domain.schemas import ExecuteRequest
from kalpi_engine.domain.tags import make_tag
from kalpi_engine.planner import ValidationContext, http_status, plan, validate

# Wednesday 2026-09-30 11:00 IST (05:30 UTC): inside market hours.
MARKET_OPEN = datetime(2026, 9, 30, 5, 30, tzinfo=UTC)
KNOWN = {"RELIANCE", "TCS", "INFY", "HDFCBANK", "ITC", "SBIN"}


def req(mode: str, *instructions: dict[str, Any], **options: Any) -> ExecuteRequest:
    return ExecuteRequest.model_validate(
        {
            "session_id": str(uuid4()),
            "mode": mode,
            "options": options,
            "instructions": list(instructions),
        }
    )


def ins(symbol: str, action: str, qty: int, **kw: Any) -> dict[str, Any]:
    return {"symbol": symbol, "action": action, "quantity": qty, **kw}


def hold(symbol: str, qty: int, sellable: int | None = None) -> Holding:
    return Holding(
        symbol=symbol,
        exchange=Exchange.NSE,
        quantity=qty,
        sellable_qty=qty if sellable is None else sellable,
    )


def ctx(*holdings: Holding, **kw: Any) -> ValidationContext:
    base: dict[str, Any] = {
        "holdings": list(holdings),
        "is_known": lambda ex, sym: sym in KNOWN,
        "market_order_verified": True,
        "now": MARKET_OPEN,
    }
    return ValidationContext(**(base | kw))


def codes(issues: list[Any]) -> list[str]:
    return [i.code for i in issues]


# ---------- happy paths per mode ----------


def test_first_time_clean() -> None:
    r = validate(req("FIRST_TIME", ins("RELIANCE", "BUY", 10), ins("TCS", "BUY", 2)), ctx())
    assert r.ok and r.errors == [] and r.warnings == []


def test_rebalance_sell_buy_and_adjust_clean() -> None:
    body = req(
        "REBALANCE",
        ins("TCS", "SELL", 5),
        ins("ITC", "BUY", 10),
        ins("INFY", "REBALANCE", 3, side="BUY"),
        ins("SBIN", "REBALANCE", 2, side="SELL"),
    )
    r = validate(body, ctx(hold("TCS", 5), hold("INFY", 1), hold("SBIN", 4)))
    assert r.ok and r.warnings == []


# ---------- V1 ----------


def test_v1_first_time_with_holdings_rejected_409() -> None:
    r = validate(req("FIRST_TIME", ins("RELIANCE", "BUY", 1)), ctx(hold("ITC", 3)))
    assert codes(r.errors) == ["HOLDINGS_EXIST"]
    assert http_status(r.errors) == 409


def test_v1_first_time_zero_qty_holding_is_empty() -> None:
    r = validate(req("FIRST_TIME", ins("RELIANCE", "BUY", 1)), ctx(hold("ITC", 0)))
    assert r.ok


def test_v1_first_time_with_sell_rejected_by_schema() -> None:
    with pytest.raises(ValidationError, match="V1"):
        req("FIRST_TIME", ins("RELIANCE", "SELL", 1))


# ---------- V2 ----------


def test_v2_duplicate_symbols() -> None:
    r = validate(
        req("REBALANCE", ins("ITC", "BUY", 1), ins("itc", "BUY", 2), ins("TCS", "BUY", 1)),
        ctx(),
    )
    assert codes(r.errors) == ["DUPLICATE_SYMBOL"]
    assert r.errors[0].symbol == "ITC"
    assert http_status(r.errors) == 422


def test_v2_duplicate_reported_once_per_symbol() -> None:
    body = req("REBALANCE", ins("ITC", "BUY", 1), ins("ITC", "BUY", 2), ins("ITC", "BUY", 3))
    assert codes(validate(body, ctx()).errors) == ["DUPLICATE_SYMBOL"]


# ---------- V3 ----------


def test_v3_quantity_above_max() -> None:
    r = validate(req("FIRST_TIME", ins("ITC", "BUY", 101)), ctx(max_qty_per_order=100))
    assert codes(r.errors) == ["QUANTITY_TOO_LARGE"]


def test_v3_quantity_at_max_ok() -> None:
    assert validate(req("FIRST_TIME", ins("ITC", "BUY", 100)), ctx(max_qty_per_order=100)).ok


@pytest.mark.parametrize("qty", [0, -1, 1.5, "3"])
def test_v3_quantity_must_be_positive_int(qty: Any) -> None:
    with pytest.raises(ValidationError):
        req("FIRST_TIME", ins("ITC", "BUY", qty))


# ---------- V4 ----------


def test_v4_oversell_when_sellable_below_quantity() -> None:
    r = validate(req("REBALANCE", ins("TCS", "SELL", 5)), ctx(hold("TCS", 10, sellable=3)))
    assert codes(r.errors) == ["INSUFFICIENT_HOLDINGS"]
    assert "3" in r.errors[0].message


def test_v4_sell_not_held() -> None:
    r = validate(req("REBALANCE", ins("TCS", "SELL", 1)), ctx())
    assert codes(r.errors) == ["INSUFFICIENT_HOLDINGS"]


def test_v4_rebalance_sell_oversell() -> None:
    r = validate(req("REBALANCE", ins("SBIN", "REBALANCE", 5, side="SELL")), ctx(hold("SBIN", 4)))
    assert codes(r.errors) == ["INSUFFICIENT_HOLDINGS"]


def test_v4_sell_exactly_sellable_ok() -> None:
    assert validate(req("REBALANCE", ins("TCS", "SELL", 3)), ctx(hold("TCS", 10, 3))).ok


def test_v4_sellable_summed_across_exchanges() -> None:
    bse = Holding(symbol="TCS", exchange=Exchange.BSE, quantity=2, sellable_qty=2)
    assert validate(req("REBALANCE", ins("TCS", "SELL", 5)), ctx(hold("TCS", 3), bse)).ok


# ---------- V5 ----------


def test_v5_buy_of_held_symbol_is_warning() -> None:
    r = validate(req("REBALANCE", ins("TCS", "BUY", 1)), ctx(hold("TCS", 2)))
    assert r.ok and codes(r.warnings) == ["ALREADY_HELD"]


def test_v5_rebalance_buy_of_held_symbol_no_warning() -> None:
    r = validate(req("REBALANCE", ins("TCS", "REBALANCE", 1, side="BUY")), ctx(hold("TCS", 2)))
    assert r.ok and r.warnings == []


# ---------- V6 / V7 (payload rules, enforced by the schema) ----------


def test_v6_rebalance_without_side_rejected() -> None:
    with pytest.raises(ValidationError, match="V6"):
        req("REBALANCE", ins("TCS", "REBALANCE", 1))


def test_v7_limit_without_price_rejected() -> None:
    with pytest.raises(ValidationError, match="V7"):
        req("FIRST_TIME", ins("TCS", "BUY", 1, order_type="LIMIT"))


def test_v7_default_limit_without_price_rejected() -> None:
    with pytest.raises(ValidationError, match="V7"):
        req("FIRST_TIME", ins("TCS", "BUY", 1), order_type="LIMIT")


@pytest.mark.parametrize("price", [0, -5.0])
def test_v7_limit_price_must_be_positive(price: float) -> None:
    with pytest.raises(ValidationError):
        req("FIRST_TIME", ins("TCS", "BUY", 1, order_type="LIMIT", limit_price=price))


# ---------- V8 ----------


def test_v8_unknown_symbol() -> None:
    r = validate(req("FIRST_TIME", ins("NOPE", "BUY", 1)), ctx())
    assert codes(r.errors) == ["UNKNOWN_SYMBOL"]
    assert r.errors[0].symbol == "NOPE"


def test_v8_resolver_uses_request_exchange() -> None:
    seen: list[tuple[Exchange, str]] = []

    def known(ex: Exchange, sym: str) -> bool:
        seen.append((ex, sym))
        return True

    validate(req("FIRST_TIME", ins("ITC", "BUY", 1), exchange="BSE"), ctx(is_known=known))
    assert seen == [(Exchange.BSE, "ITC")]


# ---------- V9 ----------


def test_v9_session_missing() -> None:
    r = validate(req("FIRST_TIME", ins("ITC", "BUY", 1)), ctx(session_found=False))
    assert codes(r.errors) == ["SESSION_EXPIRED"]
    assert http_status(r.errors) == 401


def test_v9_session_expired() -> None:
    expired = MARKET_OPEN - timedelta(seconds=1)
    r = validate(req("FIRST_TIME", ins("ITC", "BUY", 1)), ctx(session_expires_at=expired))
    assert codes(r.errors) == ["SESSION_EXPIRED"]


def test_v9_session_valid() -> None:
    later = MARKET_OPEN + timedelta(hours=1)
    assert validate(req("FIRST_TIME", ins("ITC", "BUY", 1)), ctx(session_expires_at=later)).ok


# ---------- V10 ----------


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 9, 30, 3, 44, tzinfo=UTC),  # 09:14 IST
        datetime(2026, 9, 30, 10, 1, tzinfo=UTC),  # 15:31 IST
        datetime(2026, 10, 3, 5, 30, tzinfo=UTC),  # Saturday 11:00 IST
        datetime(2026, 10, 4, 5, 30, tzinfo=UTC),  # Sunday 11:00 IST
    ],
)
def test_v10_outside_market_hours_is_warning(now: datetime) -> None:
    r = validate(req("FIRST_TIME", ins("ITC", "BUY", 1)), ctx(now=now))
    assert r.ok and codes(r.warnings) == ["OUTSIDE_MARKET_HOURS"]


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 9, 30, 3, 45, tzinfo=UTC),  # 09:15 IST
        datetime(2026, 9, 30, 10, 0, tzinfo=UTC),  # 15:30 IST
    ],
)
def test_v10_market_hours_boundaries_inclusive(now: datetime) -> None:
    assert validate(req("FIRST_TIME", ins("ITC", "BUY", 1)), ctx(now=now)).warnings == []


# ---------- V11 ----------


def test_v11_market_on_unverified_broker_is_warning_not_error() -> None:
    r = validate(req("FIRST_TIME", ins("ITC", "BUY", 1)), ctx(market_order_verified=False))
    assert r.ok and codes(r.warnings) == ["MARKET_ORDER_UNVERIFIED"]
    assert "LIMIT" in r.warnings[0].message


def test_v11_limit_on_unverified_broker_no_warning() -> None:
    body = req("FIRST_TIME", ins("ITC", "BUY", 1, order_type="LIMIT", limit_price=400.0))
    assert validate(body, ctx(market_order_verified=False)).warnings == []


# ---------- all violations collected ----------


def test_all_violations_collected_together() -> None:
    body = req(
        "REBALANCE",
        ins("TCS", "SELL", 9),
        ins("NOPE", "BUY", 1),
        ins("ITC", "BUY", 999),
        ins("ITC", "BUY", 1),
    )
    r = validate(body, ctx(hold("TCS", 1), max_qty_per_order=100, session_found=False))
    assert set(codes(r.errors)) == {
        "DUPLICATE_SYMBOL",
        "QUANTITY_TOO_LARGE",
        "INSUFFICIENT_HOLDINGS",
        "UNKNOWN_SYMBOL",
        "SESSION_EXPIRED",
    }
    assert http_status(r.errors) == 401  # auth first: nothing else matters without a session


def test_http_status_holdings_beats_422() -> None:
    r = validate(req("FIRST_TIME", ins("NOPE", "BUY", 1)), ctx(hold("ITC", 1)))
    assert http_status(r.errors) == 409


# ---------- plan ----------


def test_plan_sells_then_buys_preserving_order() -> None:
    body = req(
        "REBALANCE",
        ins("ITC", "BUY", 10),
        ins("TCS", "SELL", 5),
        ins("INFY", "REBALANCE", 3, side="BUY"),
        ins("SBIN", "REBALANCE", 2, side="SELL"),
    )
    legs = plan(body, run_id="run-1")
    assert [(lg.phase, lg.symbol, lg.side) for lg in legs] == [
        (Phase.SELL, "TCS", Side.SELL),
        (Phase.SELL, "SBIN", Side.SELL),
        (Phase.BUY, "ITC", Side.BUY),
        (Phase.BUY, "INFY", Side.BUY),
    ]
    assert [lg.leg_id for lg in legs] == ["0", "1", "2", "3"]
    assert [lg.tag for lg in legs] == [make_tag("run-1", i) for i in range(4)]


def test_plan_first_time_all_buy_phase() -> None:
    legs = plan(req("FIRST_TIME", ins("ITC", "BUY", 1), ins("TCS", "BUY", 2)), run_id="r")
    assert {lg.phase for lg in legs} == {Phase.BUY}
    assert [lg.quantity for lg in legs] == [1, 2]


def test_plan_order_type_limit_price_and_exchange() -> None:
    body = req(
        "FIRST_TIME",
        ins("ITC", "BUY", 1),
        ins("HDFCBANK", "BUY", 4, order_type="LIMIT", limit_price=1650.0),
        ins("TCS", "BUY", 1, order_type="MARKET", limit_price=10.0),
        exchange="BSE",
    )
    legs = plan(body, run_id="r")
    assert [(lg.order_type, lg.limit_price) for lg in legs] == [
        (OrderType.MARKET, None),
        (OrderType.LIMIT, 1650.0),
        (OrderType.MARKET, None),
    ]
    assert {lg.exchange for lg in legs} == {Exchange.BSE}


def test_plan_default_limit_uses_instruction_price() -> None:
    body = req("FIRST_TIME", ins("ITC", "BUY", 1, limit_price=401.5), order_type="LIMIT")
    (leg,) = plan(body, run_id="r")
    assert (leg.order_type, leg.limit_price) == (OrderType.LIMIT, 401.5)


def test_plan_tag_length_follows_broker() -> None:
    (leg,) = plan(req("FIRST_TIME", ins("ITC", "BUY", 1)), run_id="r", tag_len=10)
    assert len(leg.tag) == 10
