"""Request/response schemas for preview/execute (SPEC §2)."""

from typing import Self
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from kalpi_engine.domain.enums import (
    Action,
    Exchange,
    LegStatus,
    Mode,
    OrderType,
    Phase,
    Product,
    RunStatus,
    Side,
)
from kalpi_engine.domain.models import Instruction


class ExecutionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_type: OrderType = OrderType.MARKET
    product: Product = Product.CNC
    exchange: Exchange = Exchange.NSE
    halt_on_sell_failure: bool = False
    poll_timeout_s: int = Field(default=60, gt=0, le=3600)
    webhook_url: AnyHttpUrl | None = None


class ExecuteRequest(BaseModel):
    """Payload-only checks live here; holdings/broker-dependent rules are in planner (V1-V11)."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    mode: Mode
    options: ExecutionOptions = Field(default_factory=ExecutionOptions)
    instructions: list[Instruction] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.options.order_type is OrderType.LIMIT:
            missing = [
                i.symbol
                for i in self.instructions
                if i.order_type is None and i.limit_price is None
            ]
            if missing:
                raise ValueError(f"LIMIT requires limit_price > 0 (V7): {missing}")
        if self.mode is Mode.FIRST_TIME:
            bad = [i.symbol for i in self.instructions if i.action is not Action.BUY]
            if bad:
                raise ValueError(f"FIRST_TIME allows only BUY (V1): {bad}")
        return self

    def order_type_for(self, ins: Instruction) -> OrderType:
        return ins.order_type or self.options.order_type


class Issue(BaseModel):
    """A validation violation (error) or warning."""

    code: str
    message: str
    symbol: str | None = None


class PlannedLeg(BaseModel):
    leg_index: int
    phase: Phase
    symbol: str
    exchange: Exchange
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: float | None = None


class PreviewResponse(BaseModel):
    legs: list[PlannedLeg]
    warnings: list[Issue] = Field(default_factory=list)


class ExecuteAccepted(BaseModel):
    run_id: UUID
    status: RunStatus


class LegView(PlannedLeg):
    status: LegStatus
    tag: str
    broker_order_id: str | None = None
    filled_qty: int = 0
    avg_price: float | None = None
    message: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[Issue] | dict[str, str] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody
