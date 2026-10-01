import re
import uuid

import pytest
from pydantic import ValidationError

from kalpi_engine.domain.enums import LegStatus as L
from kalpi_engine.domain.enums import RunStatus as R
from kalpi_engine.domain.enums import Side
from kalpi_engine.domain.errors import AmbiguousSubmit, InvalidOrder, KalpiError, RateLimited
from kalpi_engine.domain.models import Holding, Instruction
from kalpi_engine.domain.schemas import ExecuteRequest
from kalpi_engine.domain.status import run_status, sell_failed
from kalpi_engine.domain.tags import make_tag, tag_length

RUN = "6f1c2c8e-1d0a-4c55-9a51-0d8b8f1f6a10"


# --- tags ---------------------------------------------------------------


@pytest.mark.parametrize("length", [8, 12, 16, 20])
def test_tag_alphanumeric_bounded_deterministic(length: int) -> None:
    tag = make_tag(RUN, 3, length)
    assert len(tag) == length <= 20
    assert re.fullmatch(r"[A-Z0-9]+", tag)
    assert tag == make_tag(RUN, 3, length)


def test_tag_differs_per_leg_and_run() -> None:
    tags = {make_tag(RUN, i) for i in range(50)} | {make_tag(str(uuid.uuid4()), 0)}
    assert len(tags) == 51


@pytest.mark.parametrize("bad", [7, 21, 0])
def test_tag_length_out_of_range_rejected(bad: int) -> None:
    with pytest.raises(ValueError):
        make_tag(RUN, 0, bad)


def test_tag_length_from_broker_meta() -> None:
    assert tag_length(20) == 16
    assert tag_length(10) == 10
    with pytest.raises(ValueError):
        tag_length(6)


# --- instruction / request validation ------------------------------------


def _req(mode: str = "REBALANCE", options: dict[str, object] | None = None, **ins: object) -> dict:
    body: dict[str, object] = {
        "session_id": RUN,
        "mode": mode,
        "instructions": [{"symbol": "INFY", "action": "BUY", "quantity": 1, **ins}],
    }
    if options:
        body["options"] = options
    return body


@pytest.mark.parametrize(
    "ins",
    [
        {"action": "REBALANCE"},  # missing side (V6)
        {"quantity": 0},
        {"quantity": -3},
        {"quantity": 1.5},
        {"quantity": "3"},
        {"order_type": "LIMIT"},  # no price (V7)
        {"order_type": "LIMIT", "limit_price": 0},
        {"action": "SELL", "side": "BUY"},  # contradiction
        {"symbol": "  "},
    ],
)
def test_invalid_instruction_rejected(ins: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExecuteRequest.model_validate(_req(**ins))


def test_valid_request_and_defaults() -> None:
    req = ExecuteRequest.model_validate(
        _req(action="REBALANCE", side="SELL", symbol=" infy ", quantity=3)
    )
    ins = req.instructions[0]
    assert ins.symbol == "INFY" and ins.effective_side is Side.SELL
    assert req.order_type_for(ins) == "MARKET" and req.options.product == "CNC"


def test_limit_from_options_requires_price() -> None:
    with pytest.raises(ValidationError):
        ExecuteRequest.model_validate(_req(options={"order_type": "LIMIT"}))
    ok = ExecuteRequest.model_validate(_req(options={"order_type": "LIMIT"}, limit_price=10.5))
    assert ok.order_type_for(ok.instructions[0]) == "LIMIT"


def test_first_time_only_buys_and_nonempty() -> None:
    with pytest.raises(ValidationError):
        ExecuteRequest.model_validate(_req(mode="FIRST_TIME", action="SELL"))
    with pytest.raises(ValidationError):
        ExecuteRequest.model_validate({"session_id": RUN, "mode": "REBALANCE", "instructions": []})


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Instruction.model_validate({"symbol": "X", "action": "BUY", "quantity": 1, "qty": 2})


def test_holding_has_sellable_qty() -> None:
    h = Holding(symbol="tcs", exchange="NSE", quantity=10, sellable_qty=7)  # type: ignore[arg-type]
    assert h.symbol == "TCS" and h.sellable_qty == 7


# --- run status (SPEC §3 table, D28) -------------------------------------


@pytest.mark.parametrize(
    ("legs", "expected"),
    [
        # every leg FILLED
        ([L.FILLED], R.COMPLETED),
        ([L.FILLED, L.FILLED, L.FILLED], R.COMPLETED),
        # nothing executed, nothing in doubt
        ([L.REJECTED], R.FAILED),
        ([L.REJECTED, L.CANCELLED, L.FAILED, L.SKIPPED], R.FAILED),
        ([L.SKIPPED, L.SKIPPED], R.FAILED),
        # anything else
        ([L.PARTIAL], R.COMPLETED_WITH_FAILURES),
        ([L.OPEN], R.COMPLETED_WITH_FAILURES),
        ([L.UNKNOWN], R.COMPLETED_WITH_FAILURES),
        ([L.UNKNOWN, L.REJECTED], R.COMPLETED_WITH_FAILURES),
        ([L.OPEN, L.SKIPPED], R.COMPLETED_WITH_FAILURES),
        ([L.FILLED, L.REJECTED], R.COMPLETED_WITH_FAILURES),
        ([L.FILLED, L.SKIPPED], R.COMPLETED_WITH_FAILURES),
        ([L.FILLED, L.PARTIAL], R.COMPLETED_WITH_FAILURES),
    ],
)
def test_run_status_table(legs: list[L], expected: R) -> None:
    assert run_status(legs) is expected


@pytest.mark.parametrize("pending", [L.PLANNED, L.SUBMITTING, L.SUBMITTED])
def test_run_status_non_terminal_never_failed(pending: L) -> None:
    """D36: an order may exist for a non-terminal leg, so the run must not report FAILED."""
    assert run_status([pending]) is R.COMPLETED_WITH_FAILURES
    assert run_status([pending, L.REJECTED, L.SKIPPED]) is R.COMPLETED_WITH_FAILURES


def test_run_status_requires_legs() -> None:
    with pytest.raises(ValueError):
        run_status([])


# --- sell barrier (D21) ---------------------------------------------------


@pytest.mark.parametrize("status", list(L))
def test_sell_failed_only_false_for_filled(status: L) -> None:
    assert sell_failed(status) is (status is not L.FILLED)


# --- errors ----------------------------------------------------------------


def test_error_taxonomy() -> None:
    assert RateLimited("slow", retry_after=2.0).retry_after == 2.0
    assert InvalidOrder("bad", code="TICK_SIZE").code == "TICK_SIZE"
    assert InvalidOrder("bad").code == "INVALID_ORDER"
    assert isinstance(AmbiguousSubmit(), KalpiError)
