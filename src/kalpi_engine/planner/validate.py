"""Holdings/broker-dependent validation V1-V11 (SPEC §2). Pure: callers pre-load all inputs.

Payload-only rules (V1 all-BUY, V3 positive int, V6, V7) are enforced by domain/schemas.py.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from kalpi_engine.domain.enums import Action, Exchange, Mode, OrderType, Side
from kalpi_engine.domain.models import Holding
from kalpi_engine.domain.schemas import ExecuteRequest, Issue

IST = timezone(timedelta(hours=5, minutes=30))  # no DST in India
MARKET_OPEN, MARKET_CLOSE = time(9, 15), time(15, 30)

_STATUS = {"SESSION_EXPIRED": 401, "HOLDINGS_EXIST": 409}


@dataclass(frozen=True)
class ValidationContext:
    holdings: Sequence[Holding]
    is_known: Callable[[Exchange, str], bool]
    market_order_verified: bool
    now: datetime
    session_found: bool = True
    session_expires_at: datetime | None = None
    max_qty_per_order: int = 100_000


@dataclass
class ValidationResult:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def http_status(errors: Sequence[Issue]) -> int:
    """Most significant status wins: 401 (no session) > 409 (holdings exist) > 422."""
    found = {_STATUS.get(e.code, 422) for e in errors}
    return next(s for s in (401, 409, 422) if s in found)


def _in_market_hours(now: datetime) -> bool:
    ist = now.astimezone(IST)
    return ist.weekday() < 5 and MARKET_OPEN <= ist.time() <= MARKET_CLOSE


def validate(req: ExecuteRequest, ctx: ValidationContext) -> ValidationResult:
    """Collect ALL violations (errors) and warnings; never short-circuit."""
    res = ValidationResult()
    err, warn = res.errors.append, res.warnings.append

    if not ctx.session_found or (
        ctx.session_expires_at is not None and ctx.session_expires_at <= ctx.now
    ):
        err(Issue(code="SESSION_EXPIRED", message="session missing or expired; log in again"))

    sellable: defaultdict[str, int] = defaultdict(int)
    held: set[str] = set()
    for h in ctx.holdings:
        sellable[h.symbol] += h.sellable_qty
        if h.quantity > 0:
            held.add(h.symbol)

    if req.mode is Mode.FIRST_TIME and held:
        err(
            Issue(
                code="HOLDINGS_EXIST", message=f"FIRST_TIME needs no holdings; found {sorted(held)}"
            )
        )

    counts = Counter(i.symbol for i in req.instructions)
    for sym, n in counts.items():
        if n > 1:
            err(Issue(code="DUPLICATE_SYMBOL", message=f"{sym} appears {n} times", symbol=sym))

    exchange = req.options.exchange
    for i in req.instructions:
        if i.quantity > ctx.max_qty_per_order:
            err(
                Issue(
                    code="QUANTITY_TOO_LARGE",
                    message=f"quantity {i.quantity} > max {ctx.max_qty_per_order}",
                    symbol=i.symbol,
                )
            )
        if i.effective_side is Side.SELL and i.quantity > sellable[i.symbol]:
            err(
                Issue(
                    code="INSUFFICIENT_HOLDINGS",
                    message=f"sell {i.quantity} but sellable_qty is {sellable[i.symbol]}",
                    symbol=i.symbol,
                )
            )
        if i.action is Action.BUY and i.symbol in held:
            warn(
                Issue(code="ALREADY_HELD", message="BUY of a symbol already held", symbol=i.symbol)
            )
        if not ctx.is_known(exchange, i.symbol):
            err(Issue(code="UNKNOWN_SYMBOL", message=f"not found on {exchange}", symbol=i.symbol))
        if not ctx.market_order_verified and req.order_type_for(i) is OrderType.MARKET:
            warn(
                Issue(
                    code="MARKET_ORDER_UNVERIFIED",
                    message="MARKET orders not verified on this broker; consider LIMIT",
                    symbol=i.symbol,
                )
            )

    if not _in_market_hours(ctx.now):
        warn(
            Issue(
                code="OUTSIDE_MARKET_HOURS",
                message="outside 09:15-15:30 IST Mon-Fri; the broker may reject or queue",
            )
        )
    return res
