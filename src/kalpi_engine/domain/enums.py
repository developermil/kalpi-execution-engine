from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    REBALANCE = "REBALANCE"


class Mode(StrEnum):
    FIRST_TIME = "FIRST_TIME"
    REBALANCE = "REBALANCE"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class Product(StrEnum):
    CNC = "CNC"


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"


class Phase(StrEnum):
    SELL = "SELL"
    BUY = "BUY"


class OrderStatus(StrEnum):
    """Broker-side order state, normalised by each adapter."""

    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_FAILURES = "COMPLETED_WITH_FAILURES"
    FAILED = "FAILED"


class LegStatus(StrEnum):
    PLANNED = "PLANNED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FAILURES, RunStatus.FAILED}
)
