"""Turn a validated request into ordered OrderIntents: SELL phase, then BUY phase (D7)."""

from kalpi_engine.domain.enums import OrderType, Phase, Side
from kalpi_engine.domain.models import Instruction, OrderIntent
from kalpi_engine.domain.schemas import ExecuteRequest
from kalpi_engine.domain.tags import TAG_DEFAULT_LEN, make_tag


def plan(req: ExecuteRequest, run_id: str, tag_len: int = TAG_DEFAULT_LEN) -> list[OrderIntent]:
    """Request order is kept within each phase; leg_id is the global leg index."""
    ordered: list[Instruction] = [i for i in req.instructions if i.effective_side is Side.SELL]
    ordered += [i for i in req.instructions if i.effective_side is Side.BUY]
    legs = []
    for idx, i in enumerate(ordered):
        order_type = req.order_type_for(i)
        legs.append(
            OrderIntent(
                leg_id=str(idx),
                phase=Phase(i.effective_side),
                symbol=i.symbol,
                exchange=req.options.exchange,
                side=i.effective_side,
                quantity=i.quantity,
                order_type=order_type,
                limit_price=i.limit_price if order_type is OrderType.LIMIT else None,
                product=req.options.product,
                tag=make_tag(run_id, idx, tag_len),
            )
        )
    return legs
