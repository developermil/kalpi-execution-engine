from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from kalpi_engine.domain.enums import (
    Action,
    Exchange,
    OrderStatus,
    OrderType,
    Phase,
    Product,
    Side,
)

Symbol = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=40),
]
PositiveQty = Annotated[int, Field(strict=True, gt=0)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Instruction(_Frozen):
    symbol: Symbol
    action: Action
    side: Side | None = None
    quantity: PositiveQty
    order_type: OrderType | None = None
    limit_price: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.action is Action.REBALANCE and self.side is None:
            raise ValueError("REBALANCE requires side (V6)")
        if self.action is not Action.REBALANCE and self.side not in (None, Side(self.action)):
            raise ValueError(f"side {self.side} contradicts action {self.action}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT requires limit_price > 0 (V7)")
        return self

    @property
    def effective_side(self) -> Side:
        return self.side if self.side is not None else Side(self.action)


class OrderIntent(_Frozen):
    leg_id: str
    phase: Phase
    symbol: Symbol
    exchange: Exchange
    side: Side
    quantity: PositiveQty
    order_type: OrderType
    limit_price: float | None = Field(default=None, gt=0)
    product: Product = Product.CNC
    tag: str


class Holding(_Frozen):
    symbol: Symbol
    exchange: Exchange
    quantity: int = Field(ge=0)
    sellable_qty: int = Field(ge=0, description="Conservative qty computed by the adapter (D26)")
    avg_price: float | None = None


class Funds(_Frozen):
    available_cash: float


class BrokerOrderState(_Frozen):
    broker_order_id: str
    status: OrderStatus
    filled_qty: int = Field(default=0, ge=0)
    avg_price: float | None = None
    message: str | None = None
